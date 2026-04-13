#!/usr/bin/env python3
"""
ATLAS Lite Trailing Stop Monitor

Monitors live prices for all active trailing stops. When price breaches
the floor, executes a market sell for the full position. Handles both
crypto (24/7) and equity (market hours only) stops.

Usage:
    python scripts/trailing_stop_monitor.py

Reads:  state/trailing_stops.json, config/strategy_params.json
Writes: state/trailing_stops.json (atomic), logs/trades.jsonl (append)
API:    Alpaca (paper mode only)
"""

import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from atomic_write import atomic_write_json, atomic_read_json

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
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest, StockLatestQuoteRequest

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
crypto_data = CryptoHistoricalDataClient()  # No keys needed
stock_data = StockHistoricalDataClient(API_KEY, SECRET_KEY)


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

# Profit-taking config (from strategy_params.json or defaults)
PROFIT_TAKING_ENABLED = STRATEGY_PARAMS.get("profit_taking_enabled", True)
PROFIT_TIERS = STRATEGY_PARAMS.get("profit_tiers", [
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
MAX_STOPS_PER_RUN = RISK_PARAMS.get("max_stops_per_run", 3)  # Circuit breaker limit


# ---------------------------------------------------------------------------
# Symbol normalization helpers
# ---------------------------------------------------------------------------
def to_alpaca_position_symbol(symbol: str) -> str:
    """Convert slash format to Alpaca position format: 'BTC/USD' -> 'BTCUSD'."""
    return symbol.replace("/", "")


def to_slash_symbol(symbol: str) -> str:
    """Convert flat format to slash format if it looks like crypto: 'BTCUSD' -> 'BTC/USD'.
    Equities are returned unchanged."""
    crypto_bases = ("BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK",
                    "AAVE", "UNI", "DOT", "MATIC", "SHIB")
    for base in crypto_bases:
        if symbol.startswith(base) and symbol.endswith("USD") and "/" not in symbol:
            return f"{base}/USD"
    return symbol


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
            print(f"  WARNING: No Alpaca position found for {sym} — stop may be stale")
            continue

        alpaca_qty = abs(float(pos.qty))
        local_qty = float(stop["qty"])

        if abs(alpaca_qty - local_qty) > 1e-9:
            print(f"  RECONCILE: {sym} qty updated {local_qty} -> {alpaca_qty}")
            stop["qty"] = alpaca_qty

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
    """Place a market sell order for the full position. Returns the order object or None."""
    symbol = stop["symbol"]
    qty = float(stop["qty"])

    if is_crypto(stop):
        time_in_force = TimeInForce.GTC
    else:
        time_in_force = TimeInForce.GTC

    try:
        order_req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=time_in_force,
        )
        order = trading_client.submit_order(order_req)
        print(f"  SELL ORDER PLACED: {symbol} qty={qty} order_id={order.id}")
        return order
    except Exception as e:
        print(f"  ERROR: Failed to place sell order for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Partial sell execution (for profit-taking)
# ---------------------------------------------------------------------------
def execute_partial_sell(stop: dict, sell_qty: float) -> dict | None:
    """Place a market sell order for a partial position. Returns the order or None."""
    symbol = stop["symbol"]
    crypto = is_crypto(stop)

    time_in_force = TimeInForce.GTC if crypto else TimeInForce.DAY

    try:
        order_req = MarketOrderRequest(
            symbol=symbol,
            qty=sell_qty,
            side=OrderSide.SELL,
            time_in_force=time_in_force,
        )
        order = trading_client.submit_order(order_req)
        print(f"  PARTIAL SELL ORDER PLACED: {symbol} qty={sell_qty} order_id={order.id}")
        return order
    except Exception as e:
        print(f"  ERROR: Failed to place partial sell for {symbol} qty={sell_qty}: {e}")
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
# Trade logging
# ---------------------------------------------------------------------------
def log_trade(record: dict) -> None:
    """Append a trade record to logs/trades.jsonl."""
    TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
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

    print(f"  Equity: ${current_equity:,.2f} | Peak: ${peak_equity:,.2f} | Drawdown: -{drawdown_pct:.2f}%")

    # --- Emergency recovery check ---
    if was_emergency and drawdown_pct < (MAX_DRAWDOWN_PCT - 3.0):
        risk_state["emergency_mode"] = False
        risk_state["emergency_triggered_at"] = None
        save_risk_state(risk_state)
        print(f"  Emergency mode cleared, drawdown recovered to -{drawdown_pct:.2f}%")
        return False, active_stops, closed_stops

    # --- Emergency mode still active but not recovered ---
    if was_emergency and drawdown_pct >= (MAX_DRAWDOWN_PCT - 3.0):
        save_risk_state(risk_state)
        print(f"  EMERGENCY MODE STILL ACTIVE: drawdown -{drawdown_pct:.2f}% (need <-{MAX_DRAWDOWN_PCT - 3.0:.1f}% to clear)")
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
            order = execute_sell(stop)
            if order is None:
                print(f"  ERROR: Emergency sell failed for {symbol} — will retry next run")
                continue

            pnl = round((sell_price - entry_price) * qty, 2)
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
                         emergency_mode: bool = False):
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
            print(f"  {symbol}: equity market closed — skipping (floor=${stop['floor_price']:.4f})")
            still_active.append(stop)
            continue

        # Fetch live price
        current_price = get_current_price(stop)
        if current_price is None:
            print(f"  {symbol}: could not get price — skipping")
            still_active.append(stop)
            continue

        highest_price = float(stop["highest_price"])
        floor_price = float(stop["floor_price"])
        trail_pct = float(stop.get("trail_pct", 5.0))
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
                print(f"  {symbol}: NEW HIGH ${current_price:.4f} — floor raised ${old_floor:.4f} -> ${new_floor:.4f}")
            else:
                print(f"  {symbol}: new high ${current_price:.4f} but floor unchanged at ${floor_price:.4f}")
        else:
            pct_above_floor = ((current_price - floor_price) / floor_price) * 100 if floor_price else 0
            print(f"  {symbol}: ${current_price:.4f} (floor=${floor_price:.4f}, {pct_above_floor:+.2f}% above floor)")

        # --- Profit-taking tiers (BEFORE floor breach check) ---
        # Skipped during emergency mode — no new entries or profit-taking
        if PROFIT_TAKING_ENABLED and not emergency_mode and has_alpaca_position(symbol):
            gain_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price else 0
            tiers_hit = stop.get("profit_tiers_hit", [])

            for tier in PROFIT_TIERS:
                tier_gain = tier["gain_pct"]
                tier_sell_pct = tier["sell_pct"]

                # Skip tiers already triggered
                if tier_gain in tiers_hit:
                    continue

                if gain_pct >= tier_gain:
                    sell_qty = qty * tier_sell_pct / 100

                    # Round to int for equities, keep fractional for crypto
                    if not crypto:
                        sell_qty = int(sell_qty)
                    else:
                        sell_qty = round(sell_qty, 8)

                    if sell_qty <= 0:
                        print(f"  PROFIT TAKE SKIP: {symbol} +{gain_pct:.1f}% — sell_qty rounds to 0")
                        continue

                    order = execute_partial_sell(stop, sell_qty)
                    if order is None:
                        print(f"  ERROR: Profit-take sell failed for {symbol} — skipping tier {tier_gain}%")
                        continue

                    # Calculate PnL for this partial sell
                    sell_price = current_price  # Market order, approximate
                    pnl = round((sell_price - entry_price) * sell_qty, 2)

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
                        "signal_type": "profit_tier",
                        "tier_gain_pct": tier_gain,
                        "tier_sell_pct": tier_sell_pct,
                        "exit_reason": "profit_take",
                        "exit_type": f"tier_{tier['gain_pct']}pct",
                    })

                    # Update stop qty and mark tier as hit
                    qty -= sell_qty
                    stop["qty"] = qty
                    tiers_hit.append(tier_gain)
                    stop["profit_tiers_hit"] = tiers_hit
                    profit_takes_executed += 1

                    print(f"  PROFIT TAKE: {symbol} +{gain_pct:.1f}% — selling {tier_sell_pct}% ({sell_qty} units)")

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

                        # Recalculate floor based on new entry price
                        loss_pct = float(stop.get("loss_pct", 5.0))
                        new_floor = round(current_price * (1 - loss_pct / 100), 6)
                        if new_floor < floor_price:
                            stop["floor_price"] = new_floor
                            floor_price = new_floor
                            print(f"  LADDER BUY: {symbol} -{drop_pct:.1f}% — buying {level_shares} shares "
                                  f"(new avg=${new_entry:.2f}, floor=${new_floor:.4f})")
                        else:
                            print(f"  LADDER BUY: {symbol} -{drop_pct:.1f}% — buying {level_shares} shares "
                                  f"(new avg=${new_entry:.2f}, floor unchanged=${floor_price:.4f})")

        # --- Floor breach check ---
        if current_price <= floor_price:
            print(f"  *** FLOOR BREACHED *** {symbol}: ${current_price:.4f} <= ${floor_price:.4f}")

            # --- Circuit breaker: limit simultaneous stop triggers ---
            stops_triggered_this_run += 1
            if stops_triggered_this_run > MAX_STOPS_PER_RUN:
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

            order = execute_sell(stop)
            if order is None:
                print(f"  ERROR: Sell failed for {symbol} — keeping stop active for retry")
                still_active.append(stop)
                continue

            # Calculate realized PnL
            sell_price = current_price  # Market order, approximate
            pnl = round((sell_price - entry_price) * qty, 2)
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
                "signal_type": "trailing_stop_triggered",
                "floor_price": floor_price,
                "highest_price": float(stop["highest_price"]),
                "trail_pct": trail_pct,
                "exit_reason": "trailing_stop_triggered",
                "exit_type": "floor_breach",
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

    return still_active, closed_stops, sells_executed, profit_takes_executed, ladder_buys_executed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    now = datetime.now(timezone.utc)
    print("=" * 70)
    print(f"ATLAS Lite Trailing Stop Monitor")
    print(f"Run at: {now.isoformat()}")
    print(f"Paper mode: {PAPER}")
    print("=" * 70)

    # --- Load state ---
    state = atomic_read_json(str(STATE_FILE))
    if state is None:
        print(f"FATAL: Could not read {STATE_FILE}")
        return 1

    active_stops = state.get("active_stops", [])
    closed_stops = state.get("closed_stops", [])
    print(f"\nLoaded {len(active_stops)} active stops, {len(closed_stops)} closed stops")

    if not active_stops:
        print("No active stops to monitor. Exiting.")
        return 0

    # --- Check equity market hours once ---
    equity_market_open = market_is_open()
    print(f"Equity market open: {equity_market_open}")

    # --- Step 1: Handle PENDING_FILL stops ---
    pending_count = sum(1 for s in active_stops if s.get("status") == "PENDING_FILL")
    if pending_count > 0:
        print(f"\n--- Checking {pending_count} PENDING_FILL orders ---")
        active_stops = check_pending_fills(active_stops)

    # --- Step 2: Reconcile quantities with Alpaca ---
    active_count = sum(1 for s in active_stops if s.get("status") == "ACTIVE")
    if active_count > 0:
        print(f"\n--- Reconciling quantities for {active_count} ACTIVE stops ---")
        active_stops = reconcile_quantities(active_stops)

    # --- Step 3: Drawdown check + emergency mode ---
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
    print(f"\n--- Processing trailing stops ---")
    print(f"  Profit-taking: {'ENABLED' if PROFIT_TAKING_ENABLED else 'DISABLED'} "
          f"({len(PROFIT_TIERS)} tiers)")
    print(f"  Ladder buys:   {'ENABLED' if LADDER_BUY_ENABLED else 'DISABLED'} "
          f"({len(LADDER_LEVELS)} levels, equity only)")
    print(f"  Circuit breaker: max {MAX_STOPS_PER_RUN} stops per run")
    active_stops, closed_stops, sells, profit_takes, ladder_buys = process_active_stops(
        active_stops, closed_stops, equity_market_open, emergency_mode=emergency_mode
    )

    # --- Step 5: Save state atomically ---
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
