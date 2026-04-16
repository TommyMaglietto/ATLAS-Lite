#!/usr/bin/env python3
"""
ATLAS Lite Trailing Stop Monitor

Monitors live prices for all active trailing stops. When price breaches
the floor, executes a market sell for the full position. Handles both
crypto (24/7) and equity (market hours only) stops.

Usage:
    python scripts/trailing_stop_monitor.py [--quiet]

Reads:  state/trailing_stops.json, config/strategy_params.json
Writes: state/trailing_stops.json (atomic), logs/trades.jsonl (append)
API:    Alpaca (paper mode only)
"""

import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from atomic_write import atomic_write_json, atomic_read_json, normalize_crypto_symbol, file_lock
from resilience import acquire_pid_lock, configure_client_timeouts, validate_min_qty

# ---------------------------------------------------------------------------
# Quiet mode: suppress verbose output when nothing actionable happens
# ---------------------------------------------------------------------------
QUIET = "--quiet" in sys.argv

STATE_FILE = PROJECT_ROOT / "state" / "trailing_stops.json"
RISK_STATE_FILE = PROJECT_ROOT / "state" / "risk_state.json"
TRADES_LOG = PROJECT_ROOT / "logs" / "trades.jsonl"
PARAMS_FILE = PROJECT_ROOT / "config" / "strategy_params.json"

# ---------------------------------------------------------------------------
# Alpaca client setup (PAPER ONLY)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER = True  # NEVER change this

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest, StopLimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest, StockLatestQuoteRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
import numpy as np
import pandas as pd

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
crypto_data = CryptoHistoricalDataClient()  # No keys needed
stock_data = StockHistoricalDataClient(API_KEY, SECRET_KEY)
configure_client_timeouts(trading_client)


# ---------------------------------------------------------------------------
# Load strategy parameters (so self-improvement can tune these)
# ---------------------------------------------------------------------------
def load_strategy_params() -> dict:
    """Load trailing stop params from strategy_params.json."""
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            params = json.load(f)
        return params.get("trailing_stop", {})
    except Exception as e:
        print(f"  WARNING: Could not load strategy_params.json: {e}")
        return {}


def load_risk_params() -> dict:
    """Load risk parameters for cash reserve checks."""
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            params = json.load(f)
        return params.get("risk", {})
    except Exception as e:
        print(f"  WARNING: Could not load risk params: {e}")
        return {}


STRATEGY_PARAMS = load_strategy_params()
RISK_PARAMS = load_risk_params()


def load_15m_params():
    """Load 15-min specific params for trailing stop management."""
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            params = json.load(f)
        cs = params.get("crypto_strategy_15m", {})
        return {
            "trail_pct": cs.get("shared", {}).get("trail_pct", 2.0),
            "full_close_after_tiers": 2,  # Faster cycling: 2 tiers instead of 3
            "trail_decay_per_day": cs.get("shared", {}).get("trail_decay_per_day", 1.0),
        }
    except Exception:
        return {"trail_pct": 2.0, "full_close_after_tiers": 2, "trail_decay_per_day": 1.0}


PARAMS_15M = load_15m_params()

# Profit-taking config (from strategy_params.json or defaults)
PROFIT_TAKING_ENABLED = STRATEGY_PARAMS.get("profit_taking_enabled", True)
PROFIT_TIERS = STRATEGY_PARAMS.get("profit_tiers", [
    {"gain_pct": 5, "sell_pct": 10},
    {"gain_pct": 10, "sell_pct": 15},
    {"gain_pct": 20, "sell_pct": 20},
    {"gain_pct": 35, "sell_pct": 25},
])

# Ladder buy config (from strategy_params.json or defaults)
LADDER_BUY_ENABLED = STRATEGY_PARAMS.get("ladder_buy_enabled", False)
LADDER_LEVELS = STRATEGY_PARAMS.get("ladder_levels", [
    {"drop_pct": 15, "shares": 10},
    {"drop_pct": 20, "shares": 20},
    {"drop_pct": 30, "shares": 50},
])

# Risk config
CASH_RESERVE_PCT = RISK_PARAMS.get("cash_reserve_pct", 20.0)
MAX_DRAWDOWN_PCT = RISK_PARAMS.get("max_drawdown_pct", 15.0)
EMERGENCY_RECOVERY_BUFFER_PCT = RISK_PARAMS.get("emergency_recovery_buffer_pct", 3.0)
MAX_STOPS_PER_RUN = RISK_PARAMS.get("max_stops_per_run", 3)  # Circuit breaker limit


# ---------------------------------------------------------------------------
# Adaptive slippage model (Phase 6)
# ---------------------------------------------------------------------------

def get_slippage_pct(symbol):
    """Read per-asset slippage from the model, or use default."""
    model_file = PROJECT_ROOT / "state" / "slippage_model.json"
    model = atomic_read_json(str(model_file))
    if model and symbol in model.get("assets", {}):
        return model["assets"][symbol].get("ema_slippage_pct", 0.10) / 100
    return (model or {}).get("default_slippage_pct", 0.10) / 100


def compute_exit_indicators(symbol, crypto=True):
    """Fetch latest 1H bar and compute indicators at exit time.
    Function-level import to avoid circular deps and module-level side effects."""
    try:
        from crypto_strategy import compute_indicators, load_params
        params = load_params()
        ip = params.get("shared", params)

        if crypto:
            req = CryptoBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Hour,
                start=datetime.now(timezone.utc) - timedelta(hours=100),
                end=datetime.now(timezone.utc),
            )
            bars = crypto_data.get_crypto_bars(req)
        else:
            from alpaca.data.requests import StockBarsRequest
            req = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Hour,
                start=datetime.now(timezone.utc) - timedelta(hours=100),
                end=datetime.now(timezone.utc),
            )
            bars = stock_data.get_stock_bars(req)

        symbol_bars = list(bars[symbol])
        rows = [{"timestamp": b.timestamp, "open": float(b.open), "high": float(b.high),
                 "low": float(b.low), "close": float(b.close), "volume": float(b.volume)}
                for b in symbol_bars]
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
        if len(df) < 60:
            return None

        df = compute_indicators(df, ip)
        latest = df.iloc[-1]
        return {
            "rsi": round(float(latest.get("rsi", 0)), 2) if not np.isnan(latest.get("rsi", np.nan)) else None,
            "bb_position": round(float(latest.get("bb_position", 0)), 4) if not np.isnan(latest.get("bb_position", np.nan)) else None,
            "adx": round(float(latest.get("adx", 0)), 2) if not np.isnan(latest.get("adx", np.nan)) else None,
            "vwap": round(float(latest.get("vwap", 0)), 4) if not np.isnan(latest.get("vwap", np.nan)) else None,
            "ema9": round(float(latest.get("ema9", 0)), 4) if not np.isnan(latest.get("ema9", np.nan)) else None,
        }
    except Exception as e:
        if not QUIET:
            print(f"  Warning: Could not compute exit indicators for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Symbol normalization helpers
# ---------------------------------------------------------------------------
def to_alpaca_position_symbol(symbol: str) -> str:
    """Convert slash format to Alpaca position format: 'BTC/USD' -> 'BTCUSD'."""
    return symbol.replace("/", "")


def to_slash_symbol(symbol: str) -> str:
    """Convert flat format to slash format if it looks like crypto: 'BTCUSD' -> 'BTC/USD'.
    Equities are returned unchanged.
    Delegates to the shared normalize_crypto_symbol() in atomic_write.py."""
    return normalize_crypto_symbol(symbol)


def is_crypto(stop: dict) -> bool:
    return stop.get("asset_class", "").lower() == "crypto"


# ---------------------------------------------------------------------------
# Market hours check (equity only)
# ---------------------------------------------------------------------------
def market_is_open() -> bool:
    """Return True if the US stock market is currently open."""
    try:
        clock = trading_client.get_clock()
        return clock.is_open
    except Exception as e:
        print(f"  WARNING: Could not check market clock: {e}")
        return False


# ---------------------------------------------------------------------------
# Live position reconciliation
# ---------------------------------------------------------------------------
def reconcile_quantities(active_stops: list) -> list:
    """Update each stop's qty to match Alpaca's actual position quantity.

    The trailing_stops.json file can have stale quantities (e.g., only the
    first buy, missing DCA additions). Alpaca is the source of truth.
    """
    try:
        positions = trading_client.get_all_positions()
    except Exception as e:
        print(f"  WARNING: Could not fetch positions for reconciliation: {e}")
        return active_stops

    # Build lookup: normalize all position symbols to both formats
    pos_by_symbol = {}
    for pos in positions:
        raw_sym = pos.symbol  # Alpaca uses 'BTCUSD' for crypto, 'AAPL' for equity
        pos_by_symbol[raw_sym] = pos
        slash_sym = to_slash_symbol(raw_sym)
        if slash_sym != raw_sym:
            pos_by_symbol[slash_sym] = pos

    for stop in active_stops:
        if stop.get("status") != "ACTIVE":
            continue

        sym = stop["symbol"]
        flat_sym = to_alpaca_position_symbol(sym)

        pos = pos_by_symbol.get(sym) or pos_by_symbol.get(flat_sym)
        if pos is None:
            if not QUIET:
                print(f"  WARNING: No Alpaca position for {sym} -- marking qty=0 for cleanup")
            stop["qty"] = 0
            continue

        # Count how many stops share this Alpaca position (e.g. 1H + 15M)
        sibling_count = sum(
            1 for s in active_stops
            if s.get("status") == "ACTIVE" and s.get("symbol") == sym and s is not stop
        )

        if sibling_count == 0:
            # Single stop for this symbol -- reconcile directly
            alpaca_qty = abs(float(pos.qty))
            local_qty = float(stop["qty"])

            if abs(alpaca_qty - local_qty) > 1e-9:
                if not QUIET:
                    print(f"  RECONCILE: {sym} qty updated {local_qty} -> {alpaca_qty}")
                stop["qty"] = alpaca_qty
        # else: multiple stops share the position -- skip per-stop reconciliation
        # (validated in aggregate below)

    # After reconciliation, verify total local qty matches Alpaca per symbol
    by_symbol_total = {}
    for stop in active_stops:
        if stop.get("status") != "ACTIVE":
            continue
        sym = stop.get("symbol", "")
        by_symbol_total[sym] = by_symbol_total.get(sym, 0) + float(stop.get("qty", 0))

    for sym, total_local in by_symbol_total.items():
        flat_sym = to_alpaca_position_symbol(sym)
        pos = pos_by_symbol.get(sym) or pos_by_symbol.get(flat_sym)
        if pos:
            alpaca_qty = abs(float(pos.qty))
            if abs(total_local - alpaca_qty) > 0.001:
                if not QUIET:
                    print(f"  WARNING: {sym} total local qty {total_local:.6f} != Alpaca qty {alpaca_qty:.6f}")

    return active_stops


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------
def get_current_price(stop: dict) -> float | None:
    """Fetch latest mid-price for a stop's symbol. Returns None on failure."""
    symbol = stop["symbol"]
    try:
        if is_crypto(stop):
            req = CryptoLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = crypto_data.get_crypto_latest_quote(req)
            quote = quotes.get(symbol)
            if quote is None:
                print(f"  WARNING: No crypto quote returned for {symbol}")
                return None
            bid = float(quote.bid_price) if quote.bid_price else 0.0
            ask = float(quote.ask_price) if quote.ask_price else 0.0
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return ask or bid or None
        else:
            req = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = stock_data.get_stock_latest_quote(req)
            quote = quotes.get(symbol)
            if quote is None:
                print(f"  WARNING: No stock quote returned for {symbol}")
                return None
            bid = float(quote.bid_price) if quote.bid_price else 0.0
            ask = float(quote.ask_price) if quote.ask_price else 0.0
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return ask or bid or None
    except Exception as e:
        print(f"  ERROR: Failed to get price for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Sell execution
# ---------------------------------------------------------------------------
def execute_sell(stop: dict) -> dict | None:
    """Close the full position via Alpaca's close_position API.

    Using close_position(symbol) instead of MarketOrderRequest(qty=...) avoids
    the float-precision "insufficient balance" errors we hit on SHIB: our stored
    qty has 6 decimals, Alpaca's balance has 9, so stored qty can exceed actual
    available by a nanoscopic amount. close_position liquidates the full live
    balance server-side, regardless of what we think we hold.

    Falls back to MarketOrderRequest if close_position fails (position closed
    elsewhere, etc.).
    """
    symbol = stop["symbol"]
    qty = float(stop["qty"])
    # Alpaca accepts both "BTC/USD" and "BTCUSD" for crypto position lookups
    # but normalizes to flat. Use whatever get_open_position would return.
    alpaca_symbol = to_alpaca_position_symbol(symbol) if is_crypto(stop) else symbol

    try:
        order = trading_client.close_position(alpaca_symbol)
        print(f"  SELL ORDER PLACED (close_position): {symbol} order_id={order.id}")
        return order
    except Exception as e:
        # Position may not exist, or close_position failed for another reason —
        # fall back to a qty-bounded market order.
        print(f"  close_position({symbol}) failed: {e} — falling back to MarketOrderRequest")
        try:
            order_req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
            order = trading_client.submit_order(order_req)
            print(f"  SELL ORDER PLACED (fallback): {symbol} qty={qty} order_id={order.id}")
            return order
        except Exception as e2:
            print(f"  ERROR: Failed to place sell order for {symbol}: {e2}")
            return None


# ---------------------------------------------------------------------------
# Partial sell execution (for profit-taking)
# ---------------------------------------------------------------------------
def execute_partial_sell(stop: dict, sell_qty: float) -> dict | None:
    """Place a market sell order for a partial position.

    Server-side stop-limit orders reserve the full position qty on Alpaca,
    leaving 'available' at 0. To execute a partial sell (profit-take) we must:
      1. Cancel the server-side stop (frees qty)
      2. Place the partial sell
      3. Re-place the server stop with (original_qty - sold_qty)
    If the partial sell fails, we still re-place the server stop so the
    position stays protected.
    """
    symbol = stop["symbol"]
    crypto = is_crypto(stop)
    time_in_force = TimeInForce.GTC if crypto else TimeInForce.DAY

    # Step 1: temporarily lift the server stop so Alpaca frees the reserved qty
    had_server_stop = bool(stop.get("trailing_stop_order_id"))
    if had_server_stop:
        cancel_server_stop(stop, trading_client)

    # Step 2: place the partial sell
    order = None
    try:
        order_req = MarketOrderRequest(
            symbol=symbol,
            qty=sell_qty,
            side=OrderSide.SELL,
            time_in_force=time_in_force,
        )
        order = trading_client.submit_order(order_req)
        print(f"  PARTIAL SELL ORDER PLACED: {symbol} qty={sell_qty} order_id={order.id}")
    except Exception as e:
        print(f"  ERROR: Failed to place partial sell for {symbol} qty={sell_qty}: {e}")

    # Step 3: re-place server stop with remaining qty (even if sell failed)
    if had_server_stop:
        remaining_qty = float(stop.get("qty", 0)) - (sell_qty if order else 0)
        if remaining_qty > 0:
            # Build a temporary dict with the reduced qty for the server stop
            stop_for_replace = dict(stop)
            stop_for_replace["qty"] = remaining_qty
            new_order_id = place_or_update_server_stop(stop_for_replace, trading_client)
            if new_order_id:
                stop["trailing_stop_order_id"] = new_order_id
        return None


# ---------------------------------------------------------------------------
# Ladder buy execution
# ---------------------------------------------------------------------------
def execute_ladder_buy(stop: dict, buy_qty: float) -> dict | None:
    """Place a market buy order for a ladder buy. Returns the order or None."""
    symbol = stop["symbol"]

    # Equity only — use DAY time in force
    time_in_force = TimeInForce.DAY

    try:
        order_req = MarketOrderRequest(
            symbol=symbol,
            qty=buy_qty,
            side=OrderSide.BUY,
            time_in_force=time_in_force,
        )
        order = trading_client.submit_order(order_req)
        print(f"  LADDER BUY ORDER PLACED: {symbol} qty={buy_qty} order_id={order.id}")
        return order
    except Exception as e:
        print(f"  ERROR: Failed to place ladder buy for {symbol} qty={buy_qty}: {e}")
        return None


# ---------------------------------------------------------------------------
# Cash reserve check
# ---------------------------------------------------------------------------
def get_available_cash() -> tuple[float, float, bool]:
    """Return (cash, equity, can_trade) checking the 20% reserve constraint."""
    try:
        account = trading_client.get_account()
        cash = float(account.cash)
        equity = float(account.equity)
        reserve_needed = equity * (CASH_RESERVE_PCT / 100)
        available = cash - reserve_needed
        return cash, equity, available > 0
    except Exception as e:
        print(f"  WARNING: Could not fetch account info for cash check: {e}")
        return 0.0, 0.0, False


# ---------------------------------------------------------------------------
# Alpaca position lookup
# ---------------------------------------------------------------------------
def has_alpaca_position(symbol: str) -> bool:
    """Return True if Alpaca has an open position for this symbol."""
    flat_sym = to_alpaca_position_symbol(symbol)
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            if pos.symbol == symbol or pos.symbol == flat_sym:
                return True
    except Exception as e:
        print(f"  WARNING: Could not fetch positions for {symbol}: {e}")
    return False


# ---------------------------------------------------------------------------
# Server-side stop orders (Fix 6)
# ---------------------------------------------------------------------------
def place_or_update_server_stop(stop, trading_client):
    """Place or update a server-side stop order on Alpaca.
    Returns the new order ID, or None on failure."""
    symbol = stop.get("symbol", "")
    qty = float(stop.get("qty", 0))
    floor_price = float(stop.get("floor_price", 0))

    if qty <= 0 or floor_price <= 0:
        return None

    # Cancel existing server-side stop if present
    old_order_id = stop.get("trailing_stop_order_id")
    if old_order_id:
        try:
            trading_client.cancel_order_by_id(old_order_id)
        except Exception:
            pass  # Order may already be filled/cancelled

    # Place new stop order
    # NOTE: Alpaca crypto only supports `stop_limit` (plain `stop` raises 40010001).
    # We use StopLimitOrderRequest for crypto with a 2% slippage band below stop_price,
    # and plain StopOrderRequest for equities.
    try:
        is_crypto_sym = "/" in symbol
        tif = TimeInForce.GTC

        # Alpaca needs flat symbol for crypto orders (BTCUSD not BTC/USD)
        order_symbol = to_alpaca_position_symbol(symbol) if is_crypto_sym else symbol

        if is_crypto_sym:
            # Crypto: stop_limit with 2% slippage cushion so the order actually fills
            # during fast drops. limit_price floors what we'll accept; if price blows
            # through, the limit rests and the 5-min poll-based sell backstops.
            rounded_stop = round(floor_price, 6)
            rounded_limit = round(floor_price * 0.98, 6)
            order_req = StopLimitOrderRequest(
                symbol=order_symbol,
                qty=qty,
                side=OrderSide.SELL,
                stop_price=rounded_stop,
                limit_price=rounded_limit,
                time_in_force=tif,
            )
        else:
            # Equity: plain stop order (market order triggered at stop_price)
            rounded_stop = round(floor_price, 2)
            order_req = StopOrderRequest(
                symbol=order_symbol,
                qty=qty,
                side=OrderSide.SELL,
                stop_price=rounded_stop,
                time_in_force=tif,
            )
        order = trading_client.submit_order(order_req)
        return str(order.id)
    except Exception as e:
        if not QUIET:
            print(f"  WARNING: Could not place server stop for {symbol}: {e}")
        return None


def cancel_server_stop(stop, trading_client):
    """Cancel the server-side stop order for a stop entry. Clears the order ID."""
    server_order = stop.get("trailing_stop_order_id")
    if server_order:
        try:
            trading_client.cancel_order_by_id(server_order)
        except Exception:
            pass  # Order may already be filled/cancelled
        stop["trailing_stop_order_id"] = None


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------
def log_trade(record: dict) -> None:
    """Append a trade record to logs/trades.jsonl with file locking."""
    TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    try:
        with file_lock(str(TRADES_LOG)):
            with open(TRADES_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # Fallback: write without lock rather than lose the trade record
        try:
            with open(TRADES_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            print(f"  ERROR: Failed to write trade log: {e}")


# ---------------------------------------------------------------------------
# Pending fill handler
# ---------------------------------------------------------------------------
def check_pending_fills(active_stops: list) -> list:
    """For stops with status PENDING_FILL, check if the entry order has filled.
    If filled, promote to ACTIVE and set entry_price/qty from the fill."""
    updated = []
    for stop in active_stops:
        if stop.get("status") != "PENDING_FILL":
            updated.append(stop)
            continue

        order_id = stop.get("entry_order_id")
        if not order_id:
            print(f"  WARNING: PENDING_FILL stop for {stop['symbol']} has no entry_order_id")
            updated.append(stop)
            continue

        try:
            order = trading_client.get_order_by_id(order_id)
        except Exception as e:
            print(f"  WARNING: Could not fetch order {order_id} for {stop['symbol']}: {e}")
            updated.append(stop)
            continue

        status = order.status
        symbol = stop["symbol"]

        if status == OrderStatus.FILLED:
            filled_qty = float(order.filled_qty) if order.filled_qty else float(stop["qty"])
            filled_price = float(order.filled_avg_price) if order.filled_avg_price else float(stop["entry_price"])
            trail_pct = float(stop.get("trail_pct", 5.0))
            loss_pct = float(stop.get("loss_pct", 5.0))

            # Compute initial floor from the actual fill price
            initial_floor = round(filled_price * (1 - loss_pct / 100), 2)

            stop["status"] = "ACTIVE"
            stop["qty"] = filled_qty
            stop["entry_price"] = filled_price
            stop["highest_price"] = filled_price
            stop["floor_price"] = initial_floor
            stop["last_checked"] = datetime.now(timezone.utc).isoformat()

            print(f"  FILLED: {symbol} qty={filled_qty} @ ${filled_price:.2f} -> ACTIVE (floor=${initial_floor:.2f})")

            # Log the fill event
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": "TRAILING_STOP",
                "action": "BUY_FILL",
                "symbol": symbol,
                "asset_class": stop.get("asset_class", "equity"),
                "qty": filled_qty,
                "price": filled_price,
                "order_type": "limit",
                "order_id": order_id,
                "status": "filled",
                "cost_basis": round(filled_qty * filled_price, 2),
                "pnl": 0,
            })
        elif status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            print(f"  CANCELLED/EXPIRED: {symbol} order {order_id} status={status} — removing stop")
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": "TRAILING_STOP",
                "action": "ORDER_CANCELLED",
                "symbol": symbol,
                "asset_class": stop.get("asset_class", "equity"),
                "qty": float(stop["qty"]),
                "price": 0,
                "order_type": "limit",
                "order_id": order_id,
                "status": str(status),
                "pnl": 0,
            })
            # Do not add to updated list — effectively removes the stop
            continue
        else:
            # Check for stale PENDING_FILL orders (>48 hours)
            opened_at_str = stop.get("opened_at")
            if opened_at_str:
                try:
                    opened_at = datetime.fromisoformat(opened_at_str)
                    age = datetime.now(timezone.utc) - opened_at
                    if age > timedelta(hours=48):
                        print(f"  WARNING: PENDING_FILL timeout for {symbol} — order {order_id} "
                              f"is {age.total_seconds() / 3600:.1f}h old, cancelling")
                        try:
                            trading_client.cancel_order_by_id(order_id)
                            print(f"  CANCELLED stale order {order_id} for {symbol}")
                        except Exception as cancel_err:
                            print(f"  WARNING: Failed to cancel stale order {order_id}: {cancel_err}")
                        log_trade({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "strategy": "TRAILING_STOP",
                            "action": "PENDING_FILL_TIMEOUT",
                            "symbol": symbol,
                            "asset_class": stop.get("asset_class", "equity"),
                            "qty": float(stop["qty"]),
                            "price": 0,
                            "order_type": "limit",
                            "order_id": order_id,
                            "status": "timeout_cancelled",
                            "age_hours": round(age.total_seconds() / 3600, 1),
                            "pnl": 0,
                        })
                        # Remove from active stops — do not add to updated list
                        continue
                except (ValueError, TypeError) as parse_err:
                    print(f"  WARNING: Could not parse opened_at for {symbol}: {parse_err}")

            if not QUIET:
                print(f"  PENDING: {symbol} order {order_id} status={status}")

        updated.append(stop)

    return updated


# ---------------------------------------------------------------------------
# Risk state management (drawdown tracking + emergency mode)
# ---------------------------------------------------------------------------
def load_risk_state() -> dict:
    """Load risk state from file, or create default if missing."""
    state = atomic_read_json(str(RISK_STATE_FILE))
    if state is not None:
        return state
    # File doesn't exist yet — bootstrap with current equity
    try:
        account = trading_client.get_account()
        current_equity = float(account.equity)
    except Exception as e:
        print(f"  WARNING: Could not fetch account equity for risk state init: {e}")
        current_equity = 100000.0  # Safe fallback for paper account
    default_state = {
        "peak_equity": current_equity,
        "peak_date": datetime.now(timezone.utc).isoformat(),
        "emergency_mode": False,
        "emergency_triggered_at": None,
        "current_drawdown_pct": 0.0,
        "circuit_breaker_hit": False,
        "circuit_breaker_at": None,
    }
    atomic_write_json(str(RISK_STATE_FILE), default_state)
    if not QUIET:
        print(f"  Created risk_state.json (peak_equity=${current_equity:,.2f})")
    return default_state


def save_risk_state(risk_state: dict) -> bool:
    """Atomically save risk state."""
    return atomic_write_json(str(RISK_STATE_FILE), risk_state)


def check_drawdown_and_emergency(active_stops: list, closed_stops: list) -> tuple[bool, list, list]:
    """Check portfolio drawdown against max limit. Handle emergency mode.

    Returns:
        (emergency_triggered, updated_active_stops, updated_closed_stops)
        If emergency_triggered is True, the caller should skip normal stop processing.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Fetch current equity from Alpaca
    try:
        account = trading_client.get_account()
        current_equity = float(account.equity)
    except Exception as e:
        print(f"  WARNING: Could not fetch account equity for drawdown check: {e}")
        return False, active_stops, closed_stops

    # Load risk state
    risk_state = load_risk_state()
    peak_equity = float(risk_state.get("peak_equity", current_equity))
    was_emergency = risk_state.get("emergency_mode", False)

    # Update peak if equity has risen
    if current_equity > peak_equity:
        risk_state["peak_equity"] = current_equity
        risk_state["peak_date"] = now_iso
        peak_equity = current_equity

    # Calculate drawdown
    if peak_equity > 0:
        drawdown_pct = ((peak_equity - current_equity) / peak_equity) * 100
    else:
        drawdown_pct = 0.0
    drawdown_pct = round(drawdown_pct, 2)
    risk_state["current_drawdown_pct"] = drawdown_pct

    if not QUIET:
        print(f"  Equity: ${current_equity:,.2f} | Peak: ${peak_equity:,.2f} | Drawdown: -{drawdown_pct:.2f}%")

    # --- Emergency recovery check ---
    recovery_threshold = MAX_DRAWDOWN_PCT - EMERGENCY_RECOVERY_BUFFER_PCT
    if was_emergency and drawdown_pct < recovery_threshold:
        risk_state["emergency_mode"] = False
        risk_state["emergency_triggered_at"] = None
        save_risk_state(risk_state)
        print(f"  Emergency mode cleared, drawdown recovered to -{drawdown_pct:.2f}%")
        return False, active_stops, closed_stops

    # --- Emergency mode still active but not recovered ---
    if was_emergency and drawdown_pct >= recovery_threshold:
        save_risk_state(risk_state)
        print(f"  EMERGENCY MODE STILL ACTIVE: drawdown -{drawdown_pct:.2f}% (need <-{recovery_threshold:.1f}% to clear)")
        print(f"  Skipping all new entries (profit-taking, ladder buys blocked)")
        # Don't trigger emergency sells again, but keep blocking new entries
        return True, active_stops, closed_stops

    # --- Check for new breach ---
    if drawdown_pct >= MAX_DRAWDOWN_PCT:
        risk_state["emergency_mode"] = True
        risk_state["emergency_triggered_at"] = now_iso
        save_risk_state(risk_state)

        print(f"  MAX DRAWDOWN BREACHED: -{drawdown_pct:.2f}% (limit: {MAX_DRAWDOWN_PCT}%)")
        print(f"  EMERGENCY MODE: Halting all new entries, closing weakest positions")

        # Find positions with unrealized PnL for triage
        positions_with_pnl = []
        for stop in active_stops:
            if stop.get("status") != "ACTIVE":
                continue
            entry_price = float(stop.get("entry_price", 0))
            qty = float(stop.get("qty", 0))
            current_price = get_current_price(stop)
            if current_price is None:
                continue
            unrealized_pnl = (current_price - entry_price) * qty
            positions_with_pnl.append((unrealized_pnl, stop, current_price))

        # Sort ascending — worst PnL first
        positions_with_pnl.sort(key=lambda x: x[0])

        # Close the 2 worst positions
        emergency_sells = positions_with_pnl[:2]
        sold_symbols = set()

        for unrealized_pnl, stop, sell_price in emergency_sells:
            symbol = stop["symbol"]
            qty = float(stop["qty"])
            entry_price = float(stop["entry_price"])

            print(f"  EMERGENCY SELL: {symbol} (unrealized PnL: ${unrealized_pnl:+,.2f})")
            # Cancel server-side stop to prevent double-sell
            cancel_server_stop(stop, trading_client)
            order = execute_sell(stop)
            if order is None:
                print(f"  ERROR: Emergency sell failed for {symbol} — will retry next run")
                continue

            trade_value = sell_price * qty
            slippage_cost = round(trade_value * get_slippage_pct(symbol), 2)
            pnl = round((sell_price - entry_price) * qty - slippage_cost, 2)
            pnl_pct = round(((sell_price - entry_price) / entry_price) * 100, 2) if entry_price else 0

            log_trade({
                "timestamp": now_iso,
                "strategy": "TRAILING_STOP",
                "action": "EMERGENCY_SELL",
                "symbol": symbol,
                "asset_class": stop.get("asset_class", "equity"),
                "qty": qty,
                "price": sell_price,
                "order_type": "market",
                "order_id": str(order.id),
                "status": "filled",
                "entry_price": entry_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "slippage_estimate": slippage_cost,
                "signal_type": "max_drawdown_emergency",
                "drawdown_pct": drawdown_pct,
                "max_drawdown_limit": MAX_DRAWDOWN_PCT,
                "exit_reason": "emergency_drawdown",
                "exit_type": "max_drawdown_breach",
            })

            # Move to closed
            closed_record = deepcopy(stop)
            closed_record["status"] = "EMERGENCY_CLOSED"
            closed_record["closed_at"] = now_iso
            closed_record["sell_price"] = sell_price
            closed_record["sell_order_id"] = str(order.id)
            closed_record["realized_pnl"] = pnl
            closed_record["realized_pnl_pct"] = pnl_pct
            closed_stops.append(closed_record)
            sold_symbols.add(symbol)

            print(f"  SOLD (EMERGENCY): {symbol} qty={qty} @ ~${sell_price:.4f} | PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)")

        # Rebuild active_stops without the sold positions
        still_active = []
        for stop in active_stops:
            if stop["symbol"] in sold_symbols and stop.get("status") == "ACTIVE":
                continue  # This position was emergency-sold
            still_active.append(stop)

        return True, still_active, closed_stops

    # No emergency — save updated risk state and continue normally
    # Clear circuit breaker flag from previous runs
    risk_state["circuit_breaker_hit"] = False
    risk_state["circuit_breaker_at"] = None
    save_risk_state(risk_state)
    return False, active_stops, closed_stops


# ---------------------------------------------------------------------------
# Core trailing stop logic
# ---------------------------------------------------------------------------
def process_active_stops(active_stops: list, closed_stops: list, equity_market_open: bool,
                         emergency_mode: bool = False, schema_version: str = "1.0.0"):
    """Check prices and update trailing logic for each ACTIVE stop.

    Order of operations per stop:
      1. Trail-up logic (raise floor on new highs)
      2. Profit-taking tiers (partial sells on the way up) — skipped in emergency mode
      3. Ladder buys (add shares on significant dips, equity only) — skipped in emergency mode
      4. Floor breach check (full sell if trailing stop triggers) — subject to circuit breaker

    Returns (updated_active, updated_closed, sell_count, profit_takes, ladder_buys).
    """
    still_active = []
    sells_executed = 0
    stops_triggered_this_run = 0
    circuit_breaker_tripped = False
    profit_takes_executed = 0
    ladder_buys_executed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for stop in active_stops:
        if stop.get("status") != "ACTIVE":
            still_active.append(stop)
            continue

        symbol = stop["symbol"]
        crypto = is_crypto(stop)

        # Equity stops can only fire during market hours
        if not crypto and not equity_market_open:
            if not QUIET:
                print(f"  {symbol}: equity market closed — skipping (floor=${stop['floor_price']:.4f})")
            still_active.append(stop)
            continue

        # Fetch live price
        current_price = get_current_price(stop)
        if current_price is None:
            if not QUIET:
                print(f"  {symbol}: could not get price — skipping")
            still_active.append(stop)
            continue

        highest_price = float(stop["highest_price"])
        floor_price = float(stop["floor_price"])
        timeframe = stop.get("timeframe", "1H")

        # --- Server-side stop: check if it filled between polls ---
        if STRATEGY_PARAMS.get("server_side_stops", False):
            server_order = stop.get("trailing_stop_order_id")
            if server_order:
                try:
                    srv_order = trading_client.get_order_by_id(server_order)
                    if str(srv_order.status) in ("filled", "partially_filled"):
                        if not QUIET:
                            print(f"  {symbol}: Server stop FILLED (order {server_order[:8]}...)")
                        # Mark stop as closed -- the server handled it
                        fill_price = float(srv_order.filled_avg_price) if srv_order.filled_avg_price else current_price
                        fill_qty = float(srv_order.filled_qty) if srv_order.filled_qty else float(stop["qty"])
                        srv_pnl = round((fill_price - float(stop["entry_price"])) * fill_qty, 2)
                        srv_pnl_pct = round(((fill_price - float(stop["entry_price"])) / float(stop["entry_price"])) * 100, 2) if float(stop["entry_price"]) else 0
                        log_trade({
                            "timestamp": now_iso,
                            "strategy": "TRAILING_STOP",
                            "action": "SELL",
                            "symbol": symbol,
                            "asset_class": stop.get("asset_class", "equity"),
                            "qty": fill_qty,
                            "price": fill_price,
                            "order_type": "stop",
                            "order_id": server_order,
                            "status": "filled",
                            "entry_price": float(stop["entry_price"]),
                            "pnl": srv_pnl,
                            "pnl_pct": srv_pnl_pct,
                            "signal_type": "server_stop_filled",
                            "exit_reason": "server_stop_filled",
                            "exit_type": "server_side_stop",
                            "timeframe": timeframe,
                        })
                        closed_record = deepcopy(stop)
                        closed_record["status"] = "STOPPED_OUT_SERVER"
                        closed_record["closed_at"] = now_iso
                        closed_record["sell_price"] = fill_price
                        closed_record["sell_order_id"] = server_order
                        closed_record["realized_pnl"] = srv_pnl
                        closed_record["realized_pnl_pct"] = srv_pnl_pct
                        closed_record["qty"] = 0
                        closed_stops.append(closed_record)
                        sells_executed += 1
                        print(f"  SOLD (SERVER STOP): {symbol} qty={fill_qty} @ ${fill_price:.4f} | PnL=${srv_pnl:+.2f} ({srv_pnl_pct:+.2f}%)")
                        continue  # Skip all further processing for this stop
                except Exception:
                    pass  # Order lookup failed, proceed with normal check

        # --- Server-side stop: place initial stop if not yet placed ---
        if STRATEGY_PARAMS.get("server_side_stops", False) and not stop.get("trailing_stop_order_id"):
            order_id = place_or_update_server_stop(stop, trading_client)
            if order_id:
                stop["trailing_stop_order_id"] = order_id
                if not QUIET:
                    print(f"  {symbol}: Server stop placed at ${floor_price:.4f} (order {order_id[:8]}...)")

        # Use timeframe-specific trail_pct and full_close threshold
        if timeframe == "15M":
            trail_pct = PARAMS_15M.get("trail_pct", 2.0)
            full_close_threshold_for_stop = PARAMS_15M.get("full_close_after_tiers", 2)
            # Sync the stop's trail_pct with 15M config
            if float(stop.get("trail_pct", 5.0)) != trail_pct:
                if not QUIET:
                    print(f"  {symbol}: [15M] trail_pct synced {stop.get('trail_pct')} -> {trail_pct}")
                stop["trail_pct"] = trail_pct
        else:
            # ATR-based dynamic trail: each stop has its own trail_pct set at creation
            # Do NOT sync from config — respect the per-asset ATR-computed trail
            trail_pct = float(stop.get("trail_pct", 5.0))
            full_close_threshold_for_stop = STRATEGY_PARAMS.get("full_close_after_tiers", 3)

        entry_price = float(stop["entry_price"])
        qty = float(stop["qty"])

        # --- Trail-up logic ---
        if current_price > highest_price:
            stop["highest_price"] = current_price
            new_floor = round(current_price * (1 - trail_pct / 100), 6)
            if new_floor > floor_price:
                old_floor = floor_price
                stop["floor_price"] = new_floor
                floor_price = new_floor
                # Update server-side stop order at new floor
                if STRATEGY_PARAMS.get("server_side_stops", False):
                    new_order_id = place_or_update_server_stop(stop, trading_client)
                    if new_order_id:
                        stop["trailing_stop_order_id"] = new_order_id
                if not QUIET:
                    print(f"  {symbol}: NEW HIGH ${current_price:.4f} — floor raised ${old_floor:.4f} -> ${new_floor:.4f}")
            else:
                if not QUIET:
                    print(f"  {symbol}: new high ${current_price:.4f} but floor unchanged at ${floor_price:.4f}")
        else:
            pct_above_floor = ((current_price - floor_price) / floor_price) * 100 if floor_price else 0
            if not QUIET:
                print(f"  {symbol}: ${current_price:.4f} (floor=${floor_price:.4f}, {pct_above_floor:+.2f}% above floor)")

        # --- Profit-taking tiers (BEFORE floor breach check) ---
        # Skipped during emergency mode — no new entries or profit-taking
        if PROFIT_TAKING_ENABLED and not emergency_mode and has_alpaca_position(symbol):
            gain_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price else 0
            tiers_hit = stop.get("profit_tiers_hit", [])
            # Snapshot original qty so each tier's % is computed from the
            # position size at the START of this run, not after prior tier sells
            original_qty_for_tiers = qty

            for tier in PROFIT_TIERS:
                tier_gain = tier["gain_pct"]
                tier_sell_pct = tier["sell_pct"]

                # Skip tiers already triggered
                if tier_gain in tiers_hit:
                    continue

                if gain_pct >= tier_gain:
                    sell_qty = original_qty_for_tiers * tier_sell_pct / 100

                    # Round to int for equities, keep fractional for crypto
                    if not crypto:
                        sell_qty = int(sell_qty)
                    else:
                        sell_qty = round(sell_qty, 8)

                    if sell_qty <= 0:
                        if not QUIET:
                            print(f"  PROFIT TAKE SKIP: {symbol} +{gain_pct:.1f}% — sell_qty rounds to 0")
                        continue

                    # Safety: never sell more than we actually hold
                    if sell_qty > qty:
                        print(f"  PROFIT TAKE CLAMP: {symbol} sell_qty {sell_qty} > held qty {qty}, clamping")
                        sell_qty = qty

                    # Safety: don't leave dust positions behind
                    dust_threshold = 0.001 if crypto else 1
                    remaining_after_sell = qty - sell_qty
                    if 0 < remaining_after_sell < dust_threshold:
                        print(f"  PROFIT TAKE SKIP: {symbol} — would leave dust position ({remaining_after_sell})")
                        continue

                    # Minimum quantity validation
                    pt_asset_cls = "crypto" if crypto else "equity"
                    pt_qty_ok, pt_min_qty = validate_min_qty(symbol, sell_qty, asset_class=pt_asset_cls)
                    if not pt_qty_ok:
                        print(f"  PROFIT TAKE SKIP: {symbol} sell_qty={sell_qty} below minimum ({pt_min_qty})")
                        continue

                    order = execute_partial_sell(stop, sell_qty)
                    if order is None:
                        print(f"  ERROR: Profit-take sell failed for {symbol} — skipping tier {tier_gain}%")
                        continue

                    # Calculate PnL for this partial sell
                    sell_price = current_price  # Market order, approximate
                    trade_value = sell_price * sell_qty
                    slippage_cost = round(trade_value * get_slippage_pct(symbol), 2)
                    pnl = round((sell_price - entry_price) * sell_qty - slippage_cost, 2)

                    # Log the profit-take trade
                    log_trade({
                        "timestamp": now_iso,
                        "strategy": "TRAILING_STOP",
                        "action": "PROFIT_TAKE",
                        "symbol": symbol,
                        "asset_class": stop.get("asset_class", "equity" if not crypto else "crypto"),
                        "qty": sell_qty,
                        "price": sell_price,
                        "order_type": "market",
                        "order_id": str(order.id),
                        "status": "filled",
                        "entry_price": entry_price,
                        "pnl": pnl,
                        "pnl_pct": round(gain_pct, 2),
                        "slippage_estimate": slippage_cost,
                        "signal_type": "profit_tier",
                        "entry_signal_type": stop.get("entry_signal_type", "unknown"),
                        "tier_gain_pct": tier_gain,
                        "tier_sell_pct": tier_sell_pct,
                        "exit_reason": "profit_take",
                        "exit_type": f"tier_{tier['gain_pct']}pct",
                        "exit_indicators": compute_exit_indicators(symbol, crypto=crypto),
                        "timeframe": stop.get("timeframe", "1H"),
                    })

                    # Update stop qty and mark tier as hit
                    qty -= sell_qty
                    stop["qty"] = qty
                    tiers_hit.append(tier_gain)
                    stop["profit_tiers_hit"] = tiers_hit
                    profit_takes_executed += 1

                    # Crash-safety: persist tier immediately so a restart
                    # won't re-trigger the same profit-take sell.  The stop
                    # object is a reference inside active_stops, so the list
                    # already reflects the mutation.
                    _interim_state = {
                        "schema_version": schema_version,
                        "active_stops": active_stops,
                        "closed_stops": closed_stops,
                    }
                    if not atomic_write_json(str(STATE_FILE), _interim_state):
                        print(f"  WARNING: Intermediate state save failed after profit-take for {symbol}")

                    print(f"  PROFIT TAKE: {symbol} +{gain_pct:.1f}% — selling {tier_sell_pct}% ({sell_qty} units)")

            # --- Full close after N tiers (learning mode: complete the round-trip) ---
            full_close_threshold = full_close_threshold_for_stop
            if full_close_threshold > 0 and len(tiers_hit) >= full_close_threshold and qty > 0:
                remaining_qty = round(qty, 8) if crypto else int(qty)
                # Minimum quantity validation
                fc_asset_cls = "crypto" if crypto else "equity"
                fc_qty_ok, fc_min_qty = validate_min_qty(symbol, remaining_qty, asset_class=fc_asset_cls)
                if fc_qty_ok and remaining_qty > 0:
                    # Cancel server-side stop to prevent double-sell
                    cancel_server_stop(stop, trading_client)
                    fc_order = execute_sell(stop)
                    if fc_order is not None:
                        fc_sell_price = current_price
                        fc_trade_value = fc_sell_price * remaining_qty
                        fc_slippage_cost = round(fc_trade_value * get_slippage_pct(symbol), 2)
                        fc_pnl = round((fc_sell_price - entry_price) * remaining_qty - fc_slippage_cost, 2)
                        fc_pnl_pct = round(((fc_sell_price - entry_price) / entry_price) * 100, 2) if entry_price else 0

                        log_trade({
                            "timestamp": now_iso,
                            "strategy": "TRAILING_STOP",
                            "action": "FULL_CLOSE",
                            "symbol": symbol,
                            "asset_class": stop.get("asset_class", "equity" if not crypto else "crypto"),
                            "qty": remaining_qty,
                            "price": fc_sell_price,
                            "order_type": "market",
                            "order_id": str(fc_order.id),
                            "status": "filled",
                            "entry_price": entry_price,
                            "pnl": fc_pnl,
                            "pnl_pct": fc_pnl_pct,
                            "slippage_estimate": fc_slippage_cost,
                            "signal_type": "tier_threshold_complete",
                            "entry_signal_type": stop.get("entry_signal_type", "unknown"),
                            "tiers_hit": list(tiers_hit),
                            "full_close_threshold": full_close_threshold,
                            "exit_reason": "tier_threshold_complete",
                            "exit_type": f"full_close_after_{full_close_threshold}_tiers",
                            "exit_indicators": compute_exit_indicators(symbol, crypto=crypto),
                            "timeframe": stop.get("timeframe", "1H"),
                        })

                        # Move to closed stops
                        closed_record = deepcopy(stop)
                        closed_record["status"] = "TIER_COMPLETE"
                        closed_record["closed_at"] = now_iso
                        closed_record["sell_price"] = fc_sell_price
                        closed_record["sell_order_id"] = str(fc_order.id)
                        closed_record["realized_pnl"] = fc_pnl
                        closed_record["realized_pnl_pct"] = fc_pnl_pct
                        closed_stops.append(closed_record)

                        sells_executed += 1
                        qty = 0
                        stop["qty"] = 0
                        print(f"  FULL CLOSE: {symbol} — all {full_close_threshold} tiers hit, "
                              f"sold remaining {remaining_qty} | PnL=${fc_pnl:+.2f} ({fc_pnl_pct:+.2f}%)")
                    else:
                        print(f"  ERROR: Full close sell failed for {symbol} — keeping active")
                else:
                    if not QUIET:
                        print(f"  FULL CLOSE SKIP: {symbol} remaining qty={remaining_qty} below minimum ({fc_min_qty})")

        # --- Ladder buys (equity only, skip crypto — crypto DCA handles this) ---
        # Skipped during emergency mode — no new entries
        if LADDER_BUY_ENABLED and not emergency_mode and not crypto and equity_market_open:
            drop_pct = ((entry_price - current_price) / entry_price) * 100 if entry_price else 0
            levels_hit = stop.get("ladder_levels_hit", [])

            if drop_pct > 0:
                for level in LADDER_LEVELS:
                    level_drop = level["drop_pct"]
                    level_shares = level["shares"]

                    # Skip levels already triggered
                    if level_drop in levels_hit:
                        continue

                    if drop_pct >= level_drop:
                        # Check cash reserve before buying
                        cash, equity_val, can_trade = get_available_cash()
                        cost_estimate = current_price * level_shares
                        reserve_needed = equity_val * (CASH_RESERVE_PCT / 100)
                        available_cash = cash - reserve_needed

                        if not can_trade or available_cash < cost_estimate:
                            print(f"  LADDER BUY SKIP: {symbol} -{drop_pct:.1f}% — insufficient cash "
                                  f"(available=${available_cash:.2f}, need=${cost_estimate:.2f}, "
                                  f"reserve={CASH_RESERVE_PCT}%)")
                            continue

                        order = execute_ladder_buy(stop, level_shares)
                        if order is None:
                            print(f"  ERROR: Ladder buy failed for {symbol} — skipping level {level_drop}%")
                            continue

                        # Recalculate weighted average entry price
                        old_cost = entry_price * qty
                        new_cost = current_price * level_shares
                        new_qty = qty + level_shares
                        new_entry = round((old_cost + new_cost) / new_qty, 6)

                        # Log the ladder buy trade
                        log_trade({
                            "timestamp": now_iso,
                            "strategy": "TRAILING_STOP",
                            "action": "LADDER_BUY",
                            "symbol": symbol,
                            "asset_class": stop.get("asset_class", "equity"),
                            "qty": level_shares,
                            "price": current_price,
                            "order_type": "market",
                            "order_id": str(order.id),
                            "status": "filled",
                            "old_entry_price": entry_price,
                            "new_entry_price": new_entry,
                            "old_qty": qty,
                            "new_qty": new_qty,
                            "signal_type": "ladder_buy",
                            "level_drop_pct": level_drop,
                            "entry_reason": "ladder_buy",
                            "entry_type": f"drop_{level['drop_pct']}pct",
                            "cost_basis": round(new_cost, 2),
                            "pnl": 0,
                        })

                        # Update stop state
                        qty = new_qty
                        entry_price = new_entry
                        stop["qty"] = qty
                        stop["entry_price"] = new_entry
                        levels_hit.append(level_drop)
                        stop["ladder_levels_hit"] = levels_hit
                        ladder_buys_executed += 1

                        # Recalculate floor based on new weighted average entry price (not current_price)
                        # NEVER lower the floor -- max() guard prevents ladder buys from removing protection
                        loss_pct = float(stop.get("loss_pct", 5.0))
                        new_floor = round(new_entry * (1 - loss_pct / 100), 6)
                        new_floor = max(new_floor, stop.get("floor_price", 0))
                        stop["floor_price"] = new_floor
                        floor_price = new_floor
                        print(f"  LADDER BUY: {symbol} -{drop_pct:.1f}% — buying {level_shares} shares "
                              f"(new avg=${new_entry:.2f}, floor=${new_floor:.4f})")

        # --- Time-decay: tighten trail for positions that plateau ---
        trail_decay = STRATEGY_PARAMS.get("trail_decay_per_day", 0)
        if trail_decay > 0:
            # Track when the position last made a new high
            last_high_at = stop.get("last_new_high_at")
            if current_price > highest_price:
                stop["last_new_high_at"] = now_iso  # Updated new high
            elif last_high_at:
                try:
                    high_dt = datetime.fromisoformat(last_high_at)
                    if high_dt.tzinfo is None:
                        high_dt = high_dt.replace(tzinfo=timezone.utc)
                    days_stale = (datetime.now(timezone.utc) - high_dt).total_seconds() / 86400
                    decay_amount = trail_decay * days_stale
                    effective_trail = max(1.5, trail_pct - decay_amount)  # Never tighter than 1.5%
                    if effective_trail < trail_pct:
                        new_floor = highest_price * (1 - effective_trail / 100)
                        if new_floor > floor_price:
                            old_floor = floor_price
                            stop["floor_price"] = round(new_floor, 6)
                            floor_price = new_floor
                            # Update server-side stop order at new decayed floor
                            if STRATEGY_PARAMS.get("server_side_stops", False):
                                decay_order_id = place_or_update_server_stop(stop, trading_client)
                                if decay_order_id:
                                    stop["trailing_stop_order_id"] = decay_order_id
                            if not QUIET:
                                print(f"  {symbol}: TIME-DECAY floor raised ${old_floor:.4f} -> ${new_floor:.4f} "
                                      f"(trail {trail_pct:.1f}% -> {effective_trail:.1f}%, stale {days_stale:.1f}d)")
                except Exception:
                    pass
            else:
                # First time: initialize the timestamp
                stop["last_new_high_at"] = now_iso

        # --- Floor breach check ---
        # Guard against corrupt state: a floor <= 0 combined with price ~0 will
        # fire the breach every cycle and spin-loop on sub-penny assets (SHIB's
        # "0.0000 <= 0.0000" bug). If we see this, drop the stop entirely — it
        # was created from incomplete data and has no protective value.
        if floor_price <= 0 or current_price <= 0:
            if not QUIET:
                print(f"  CORRUPT STOP: {symbol} floor=${floor_price} price=${current_price} — dropping entry")
            closed_record = deepcopy(stop)
            closed_record["status"] = "DROPPED_CORRUPT"
            closed_record["closed_at"] = now_iso
            closed_record["qty"] = 0
            closed_stops.append(closed_record)
            continue  # Skip remaining logic for this stop

        if current_price <= floor_price:
            print(f"  *** FLOOR BREACHED *** {symbol}: ${current_price:.4f} <= ${floor_price:.4f}")

            # --- Circuit breaker: limit simultaneous stop triggers ---
            stops_triggered_this_run += 1
            if stops_triggered_this_run >= MAX_STOPS_PER_RUN:
                print(f"  CIRCUIT BREAKER: {stops_triggered_this_run} stops triggered in single run, pausing remaining")
                log_trade({
                    "timestamp": now_iso,
                    "strategy": "TRAILING_STOP",
                    "action": "CIRCUIT_BREAKER",
                    "symbol": symbol,
                    "asset_class": stop.get("asset_class", "equity"),
                    "qty": qty,
                    "price": current_price,
                    "order_type": "none",
                    "order_id": "",
                    "status": "paused",
                    "entry_price": entry_price,
                    "pnl": 0,
                    "signal_type": "circuit_breaker_halt",
                    "stops_triggered": stops_triggered_this_run,
                    "max_stops_per_run": MAX_STOPS_PER_RUN,
                    "floor_price": floor_price,
                })
                # Record circuit breaker in risk state
                risk_state = load_risk_state()
                risk_state["circuit_breaker_hit"] = True
                risk_state["circuit_breaker_at"] = now_iso
                save_risk_state(risk_state)
                circuit_breaker_tripped = True
                # Keep this stop active — it will be processed next run
                still_active.append(stop)
                break  # Stop processing remaining stops

            # Cancel server-side stop to prevent double-sell
            cancel_server_stop(stop, trading_client)

            # Minimum quantity validation
            asset_cls = "crypto" if is_crypto(stop) else "equity"
            qty_ok, min_qty = validate_min_qty(symbol, qty, asset_class=asset_cls)
            if not qty_ok:
                print(f"  SKIP SELL: {symbol} qty={qty} below minimum ({min_qty}) — keeping active")
                still_active.append(stop)
                continue

            order = execute_sell(stop)
            if order is None:
                print(f"  ERROR: Sell failed for {symbol} — keeping stop active for retry")
                still_active.append(stop)
                continue

            # Calculate realized PnL
            sell_price = current_price  # Market order, approximate
            trade_value = sell_price * qty
            slippage_cost = round(trade_value * get_slippage_pct(symbol), 2)
            pnl = round((sell_price - entry_price) * qty - slippage_cost, 2)
            pnl_pct = round(((sell_price - entry_price) / entry_price) * 100, 2) if entry_price else 0

            # Log the sell
            log_trade({
                "timestamp": now_iso,
                "strategy": "TRAILING_STOP",
                "action": "SELL",
                "symbol": symbol,
                "asset_class": stop.get("asset_class", "equity"),
                "qty": qty,
                "price": sell_price,
                "order_type": "market",
                "order_id": str(order.id),
                "status": "filled",
                "entry_price": entry_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "slippage_estimate": slippage_cost,
                "signal_type": "trailing_stop_triggered",
                "entry_signal_type": stop.get("entry_signal_type", "unknown"),
                "floor_price": floor_price,
                "highest_price": float(stop["highest_price"]),
                "trail_pct": trail_pct,
                "exit_reason": "trailing_stop_triggered",
                "exit_type": "floor_breach",
                "exit_indicators": compute_exit_indicators(symbol, crypto=crypto),
                "timeframe": stop.get("timeframe", "1H"),
            })

            # Move to closed
            closed_record = deepcopy(stop)
            closed_record["status"] = "STOPPED_OUT"
            closed_record["closed_at"] = now_iso
            closed_record["sell_price"] = sell_price
            closed_record["sell_order_id"] = str(order.id)
            closed_record["realized_pnl"] = pnl
            closed_record["realized_pnl_pct"] = pnl_pct
            closed_stops.append(closed_record)

            sells_executed += 1
            print(f"  SOLD: {symbol} qty={qty} @ ~${sell_price:.4f} | PnL=${pnl:+.2f} ({pnl_pct:+.2f}%)")
        else:
            stop["last_checked"] = now_iso
            still_active.append(stop)

    # If circuit breaker tripped, any remaining unprocessed stops stay active
    if circuit_breaker_tripped:
        # The break exits the for loop — remaining stops in active_stops that
        # weren't yet iterated need to be preserved. They are still in the
        # original list but not yet added to still_active. We handle this by
        # collecting the symbols already processed and adding the rest.
        processed_symbols = {s["symbol"] for s in still_active} | {s["symbol"] for s in closed_stops if s.get("closed_at") == now_iso}
        for stop in active_stops:
            if stop["symbol"] not in processed_symbols:
                still_active.append(stop)
                processed_symbols.add(stop["symbol"])

    # --- Cleanup: move zero-qty zombie stops to closed ---
    cleaned_active = []
    for stop in still_active:
        qty = float(stop.get("qty", 0))
        crypto = is_crypto(stop)
        dust_threshold = 0.001 if crypto else 1
        if qty <= 0 or qty < dust_threshold:
            symbol = stop.get("symbol", "???")
            closed_record = deepcopy(stop)
            closed_record["status"] = "CLEANED_ZERO_QTY"
            closed_record["closed_at"] = now_iso
            closed_record["realized_pnl"] = 0
            closed_record["realized_pnl_pct"] = 0
            closed_stops.append(closed_record)
            print(f"  Cleaned up zero-qty stop for {symbol}")
        else:
            cleaned_active.append(stop)

    return cleaned_active, closed_stops, sells_executed, profit_takes_executed, ladder_buys_executed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    now = datetime.now(timezone.utc)
    if not QUIET:
        print("=" * 70)
        print(f"ATLAS Lite Trailing Stop Monitor")
        print(f"Run at: {now.isoformat()}")
        print(f"Paper mode: {PAPER}")
        print("=" * 70)

    if not acquire_pid_lock():
        return 1

    # --- Load state ---
    state = atomic_read_json(str(STATE_FILE))
    if state is None:
        print(f"FATAL: Could not read {STATE_FILE}")
        return 1

    active_stops = state.get("active_stops", [])
    closed_stops = state.get("closed_stops", [])
    if not QUIET:
        print(f"\nLoaded {len(active_stops)} active stops, {len(closed_stops)} closed stops")

    if not active_stops:
        if QUIET:
            print("[QUIET] 0 positions checked, 0 actions taken")
        else:
            print("No active stops to monitor. Exiting.")
        return 0

    # --- Check equity market hours once ---
    equity_market_open = market_is_open()
    if not QUIET:
        print(f"Equity market open: {equity_market_open}")

    # --- Step 1: Handle PENDING_FILL stops ---
    pending_count = sum(1 for s in active_stops if s.get("status") == "PENDING_FILL")
    if pending_count > 0:
        if not QUIET:
            print(f"\n--- Checking {pending_count} PENDING_FILL orders ---")
        active_stops = check_pending_fills(active_stops)

    # --- Step 2: Reconcile quantities with Alpaca ---
    active_count = sum(1 for s in active_stops if s.get("status") == "ACTIVE")
    if active_count > 0:
        if not QUIET:
            print(f"\n--- Reconciling quantities for {active_count} ACTIVE stops ---")
        active_stops = reconcile_quantities(active_stops)

    # --- Step 3: Drawdown check + emergency mode ---
    if not QUIET:
        print(f"\n--- Checking portfolio drawdown (max: {MAX_DRAWDOWN_PCT}%) ---")
    emergency_mode, active_stops, closed_stops = check_drawdown_and_emergency(
        active_stops, closed_stops
    )

    if emergency_mode:
        # Emergency mode: save state and return early — damage control only
        print(f"\n--- EMERGENCY MODE ACTIVE — skipping normal stop processing ---")
        updated_state = {
            "schema_version": state.get("schema_version", "1.0.0"),
            "active_stops": active_stops,
            "closed_stops": closed_stops,
        }
        if not atomic_write_json(str(STATE_FILE), updated_state):
            print(f"\nFATAL: Could not save state to {STATE_FILE}")
            return 1
        final_active = len([s for s in active_stops if s.get("status") == "ACTIVE"])
        print(f"\n{'=' * 70}")
        print(f"SUMMARY (EMERGENCY MODE)")
        print(f"  Active stops:      {final_active}")
        print(f"  Total closed:      {len(closed_stops)}")
        print(f"  State saved:       {STATE_FILE}")
        print(f"{'=' * 70}")
        return 0

    # --- Step 4: Process active stops (price check + trail + profit-take + ladder + sell) ---
    if not QUIET:
        print(f"\n--- Processing trailing stops ---")
        print(f"  Profit-taking: {'ENABLED' if PROFIT_TAKING_ENABLED else 'DISABLED'} "
              f"({len(PROFIT_TIERS)} tiers)")
        print(f"  Ladder buys:   {'ENABLED' if LADDER_BUY_ENABLED else 'DISABLED'} "
              f"({len(LADDER_LEVELS)} levels, equity only)")
        print(f"  Circuit breaker: max {MAX_STOPS_PER_RUN} stops per run")
    active_stops, closed_stops, sells, profit_takes, ladder_buys = process_active_stops(
        active_stops, closed_stops, equity_market_open, emergency_mode=emergency_mode,
        schema_version=state.get("schema_version", "1.0.0"),
    )

    # --- Step 5: Final zero-qty safety filter before state write ---
    pre_filter_count = len(active_stops)
    now_iso = datetime.now(timezone.utc).isoformat()
    filtered_active = []
    for stop in active_stops:
        qty = float(stop.get("qty", 0))
        crypto = is_crypto(stop)
        dust_threshold = 0.001 if crypto else 1
        if qty <= 0 or qty < dust_threshold:
            symbol = stop.get("symbol", "???")
            closed_record = deepcopy(stop)
            closed_record["status"] = "CLEANED_ZERO_QTY"
            closed_record["closed_at"] = now_iso
            closed_record["realized_pnl"] = 0
            closed_record["realized_pnl_pct"] = 0
            closed_stops.append(closed_record)
            print(f"  Cleaned up zero-qty stop for {symbol}")
        else:
            filtered_active.append(stop)
    active_stops = filtered_active
    if len(active_stops) < pre_filter_count:
        print(f"  Filtered {pre_filter_count - len(active_stops)} zero-qty zombie stop(s) before save")

    # --- Step 6: Save state atomically ---
    updated_state = {
        "schema_version": state.get("schema_version", "1.0.0"),
        "active_stops": active_stops,
        "closed_stops": closed_stops,
    }

    if not atomic_write_json(str(STATE_FILE), updated_state):
        print(f"\nFATAL: Could not save state to {STATE_FILE}")
        return 1

    # --- Summary ---
    final_active = len([s for s in active_stops if s.get("status") == "ACTIVE"])
    final_pending = len([s for s in active_stops if s.get("status") == "PENDING_FILL"])
    total_actions = sells + profit_takes + ladder_buys

    if QUIET and total_actions == 0:
        print(f"[QUIET] {final_active} positions checked, 0 actions taken")
    else:
        print(f"\n{'=' * 70}")
        print(f"SUMMARY")
        print(f"  Sells executed:    {sells}")
        print(f"  Profit takes:      {profit_takes}")
        print(f"  Ladder buys:       {ladder_buys}")
        print(f"  Active stops:      {final_active}")
        print(f"  Pending fills:     {final_pending}")
        print(f"  Total closed:      {len(closed_stops)}")
        print(f"  State saved:       {STATE_FILE}")
        print(f"{'=' * 70}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
