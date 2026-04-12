#!/usr/bin/env python3
"""
ATLAS Lite - Equity Re-Entry Scanner

Scans the equity watchlist for momentum entry signals and places limit buy
orders for stocks not currently held. Closes the trailing-stop loop: when a
stop fires and a stock is sold, this script can buy it back once momentum
confirms a new uptrend.

Entry criteria (ALL must be true):
  - Price > 20-day SMA (short-term uptrend)
  - 20-day SMA > 50-day SMA (golden cross / bullish structure)
  - RSI(14) between 40-65 (momentum, not overbought or knife-catching)
  - Regime is RISK_ON or CAUTIOUS (never buy in RISK_OFF)

Position sizing:
  - max_position_value = account_equity * max_position_pct_equity / 100
  - CAUTIOUS regime halves the size
  - Limit price = current_price * 0.995 (slightly below market)
  - qty = floor(max_position_value / limit_price)

Safety checks:
  - 3-day cooling period after a stop-out (anti-whipsaw)
  - 20% cash reserve enforced before every order
  - Paper trading ONLY

Usage:
    python scripts/equity_scanner.py

Reads:  config/symbol_lists.json, config/strategy_params.json,
        state/trailing_stops.json, state/regime.json
Writes: state/trailing_stops.json (atomic), logs/trades.jsonl (append)
API:    Alpaca (paper mode only)
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from atomic_write import atomic_write_json, atomic_read_json

CONFIG_DIR = PROJECT_ROOT / "config"
STATE_DIR = PROJECT_ROOT / "state"
LOGS_DIR = PROJECT_ROOT / "logs"

SYMBOL_LISTS_FILE = CONFIG_DIR / "symbol_lists.json"
PARAMS_FILE = CONFIG_DIR / "strategy_params.json"
TRAILING_STOPS_FILE = STATE_DIR / "trailing_stops.json"
REGIME_FILE = STATE_DIR / "regime.json"
TRADES_LOG = LOGS_DIR / "trades.jsonl"

# ---------------------------------------------------------------------------
# Alpaca client setup (PAPER ONLY)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")
PAPER = True  # NEVER change this

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
stock_data = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COOLING_PERIOD_DAYS = 3        # Days to wait after a stop-out before re-entry
LIMIT_DISCOUNT = 0.995         # Buy limit = price * 0.995 (0.5% below market)
RSI_ENTRY_LOW = 40             # Minimum RSI for entry
RSI_ENTRY_HIGH = 65            # Maximum RSI for entry
SMA_SHORT_PERIOD = 20          # Short-term moving average
SMA_LONG_PERIOD = 50           # Long-term moving average
RSI_PERIOD = 14                # RSI lookback
ATR_PERIOD = 14                # ATR lookback
BARS_LOOKBACK_DAYS = 90        # How far back to fetch bars (need 50+ for SMA50)


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def calc_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> pd.Series:
    """Average True Range."""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


def compute_equity_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute SMA20, SMA50, RSI(14), and ATR(14) on daily OHLCV bars."""
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["sma20"] = calc_sma(close, SMA_SHORT_PERIOD)
    df["sma50"] = calc_sma(close, SMA_LONG_PERIOD)
    df["rsi"] = calc_rsi(close, RSI_PERIOD)
    df["atr"] = calc_atr(high, low, close, ATR_PERIOD)

    return df


# ============================================================
# DATA LOADING
# ============================================================

def load_symbol_lists() -> list:
    """Load the equity watchlist from symbol_lists.json."""
    data = atomic_read_json(str(SYMBOL_LISTS_FILE))
    if data is None:
        print("FATAL: Could not read symbol_lists.json")
        return []
    symbols = data.get("trailing_stop_watchlist", [])
    print(f"  Equity watchlist: {symbols}")
    return symbols


def load_strategy_params() -> dict:
    """Load strategy parameters from config."""
    data = atomic_read_json(str(PARAMS_FILE))
    if data is None:
        print("WARNING: Could not read strategy_params.json, using defaults")
        return {}
    return data


def load_trailing_stops() -> dict:
    """Load current trailing stops state."""
    data = atomic_read_json(str(TRAILING_STOPS_FILE))
    if data is None:
        print("WARNING: Could not read trailing_stops.json, using empty state")
        return {"schema_version": "1.0.0", "active_stops": [], "closed_stops": []}
    return data


def load_regime() -> dict:
    """Load current market regime."""
    data = atomic_read_json(str(REGIME_FILE))
    if data is None:
        print("WARNING: Could not read regime.json, assuming RISK_OFF for safety")
        return {"current_regime": "RISK_OFF"}
    return data


# ============================================================
# CANDIDATE FILTERING
# ============================================================

def get_active_equity_symbols(stops_data: dict) -> set:
    """Return set of equity symbols that already have active/pending stops."""
    active = set()
    for stop in stops_data.get("active_stops", []):
        if stop.get("asset_class", "equity") == "equity":
            active.add(stop["symbol"])
    return active


def get_recently_stopped_symbols(stops_data: dict,
                                 cooling_days: int = COOLING_PERIOD_DAYS) -> set:
    """Return symbols stopped out within the cooling period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooling_days)
    recently_stopped = set()

    for closed in stops_data.get("closed_stops", []):
        if closed.get("asset_class", "equity") != "equity":
            continue
        closed_at_str = closed.get("closed_at", "")
        if not closed_at_str:
            continue
        try:
            closed_at = datetime.fromisoformat(closed_at_str)
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            if closed_at >= cutoff:
                recently_stopped.add(closed["symbol"])
        except (ValueError, KeyError):
            continue

    return recently_stopped


def identify_candidates(watchlist: list, stops_data: dict) -> list:
    """Find symbols eligible for re-entry (not active, not in cooling period)."""
    active_syms = get_active_equity_symbols(stops_data)
    cooling_syms = get_recently_stopped_symbols(stops_data)

    candidates = []
    for sym in watchlist:
        if sym in active_syms:
            print(f"  {sym}: SKIP (already has active/pending stop)")
            continue
        if sym in cooling_syms:
            print(f"  {sym}: SKIP (stopped out within {COOLING_PERIOD_DAYS}-day cooling period)")
            continue
        candidates.append(sym)

    return candidates


# ============================================================
# MARKET DATA FETCH
# ============================================================

def fetch_daily_bars(symbol: str) -> pd.DataFrame | None:
    """Fetch daily bars from Alpaca for a single symbol. Returns DataFrame or None."""
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=BARS_LOOKBACK_DAYS)

        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = stock_data.get_stock_bars(request)

        # Convert to DataFrame
        if hasattr(bars, "df") and not bars.df.empty:
            df = bars.df
            # If multi-index (symbol, timestamp), select our symbol
            if isinstance(df.index, pd.MultiIndex):
                if symbol in df.index.get_level_values(0):
                    df = df.xs(symbol, level=0)
                else:
                    print(f"  {symbol}: No bars found in response")
                    return None
            # Reset index so timestamp is a column
            df = df.reset_index()
            if "timestamp" in df.columns:
                df = df.sort_values("timestamp")
            return df
        else:
            print(f"  {symbol}: No bars returned")
            return None

    except Exception as e:
        print(f"  {symbol}: ERROR fetching bars: {e}")
        return None


# ============================================================
# SIGNAL EVALUATION
# ============================================================

def evaluate_entry_signal(symbol: str, df: pd.DataFrame) -> dict | None:
    """
    Check if a symbol meets all entry criteria.

    Returns a signal dict if buy, or None if no signal.
    """
    if len(df) < SMA_LONG_PERIOD + 5:
        print(f"  {symbol}: Not enough bars ({len(df)}) for SMA50 calculation")
        return None

    df = compute_equity_indicators(df)
    latest = df.iloc[-1]

    price = float(latest["close"])
    sma20 = float(latest["sma20"])
    sma50 = float(latest["sma50"])
    rsi = float(latest["rsi"])
    atr = float(latest["atr"])

    # Check for NaN values
    if any(np.isnan(v) for v in [sma20, sma50, rsi, atr]):
        print(f"  {symbol}: NaN in indicators, skipping")
        return None

    indicators = {
        "price": round(price, 2),
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "rsi": round(rsi, 2),
        "atr": round(atr, 2),
    }

    # --- Check each criterion ---
    price_above_sma20 = price > sma20
    sma20_above_sma50 = sma20 > sma50
    rsi_in_range = RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH

    # Detailed logging
    checks = []
    checks.append(f"Price > SMA20: {'YES' if price_above_sma20 else 'NO'} "
                   f"(${price:.2f} vs ${sma20:.2f})")
    checks.append(f"SMA20 > SMA50: {'YES' if sma20_above_sma50 else 'NO'} "
                   f"(${sma20:.2f} vs ${sma50:.2f})")
    checks.append(f"RSI {RSI_ENTRY_LOW}-{RSI_ENTRY_HIGH}: "
                   f"{'YES' if rsi_in_range else 'NO'} ({rsi:.1f})")

    for check in checks:
        print(f"    {check}")

    # All criteria must pass
    if price_above_sma20 and sma20_above_sma50 and rsi_in_range:
        # Classify signal strength by RSI position
        if 50 <= rsi <= 58:
            strength = "STRONG"
        elif 45 <= rsi < 50 or 58 < rsi <= 62:
            strength = "MODERATE"
        else:
            strength = "WEAK"

        reason = (f"Price ${price:.2f} > SMA20 ${sma20:.2f} > SMA50 ${sma50:.2f}, "
                  f"RSI={rsi:.1f}, ATR={atr:.2f}")

        return {
            "symbol": symbol,
            "action": "BUY",
            "signal_type": "momentum_reentry",
            "strength": strength,
            "reason": reason,
            "indicators": indicators,
            "price": price,
            "atr": atr,
        }
    else:
        print(f"  {symbol}: NO SIGNAL (criteria not met)")
        return None


# ============================================================
# POSITION SIZING & CASH CHECK
# ============================================================

def get_account_info() -> tuple[float, float]:
    """Return (cash, equity) from Alpaca account."""
    try:
        account = trading_client.get_account()
        cash = float(account.cash)
        equity = float(account.equity)
        return cash, equity
    except Exception as e:
        print(f"ERROR: Could not fetch account info: {e}")
        return 0.0, 0.0


def check_cash_reserve(cash: float, equity: float, order_value: float,
                        reserve_pct: float = 20.0) -> bool:
    """
    Return True if placing an order of order_value would still leave
    at least reserve_pct% of equity in cash.
    """
    if equity <= 0:
        return False
    remaining_cash = cash - order_value
    remaining_pct = (remaining_cash / equity) * 100
    if remaining_pct < reserve_pct:
        print(f"  CASH RESERVE WARNING: After order (${order_value:.2f}), "
              f"cash would be ${remaining_cash:.2f} = {remaining_pct:.1f}% of equity "
              f"(minimum {reserve_pct}%)")
        return False
    return True


def calculate_position_size(equity: float, max_pct: float,
                            regime: str, limit_price: float) -> int:
    """
    Calculate number of shares to buy.

    Returns integer qty (floored). Returns 0 if sizing fails.
    """
    max_value = equity * (max_pct / 100)

    # Halve size in CAUTIOUS regime
    if regime == "CAUTIOUS":
        max_value *= 0.5
        print(f"  CAUTIOUS regime: position size halved to ${max_value:.2f}")

    if limit_price <= 0:
        return 0

    qty = math.floor(max_value / limit_price)
    return max(qty, 0)


# ============================================================
# ORDER PLACEMENT
# ============================================================

def place_limit_buy(symbol: str, qty: int, limit_price: float) -> object | None:
    """Place a GTC limit buy order via Alpaca. Returns the order or None."""
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=round(limit_price, 2),
        ))
        print(f"  ORDER PLACED: {symbol} qty={qty} limit=${limit_price:.2f} "
              f"order_id={order.id} status={order.status}")
        return order
    except Exception as e:
        print(f"  ERROR: Failed to place order for {symbol}: {e}")
        return None


# ============================================================
# LOGGING & STATE
# ============================================================

def log_trade(record: dict) -> None:
    """Append a trade record to logs/trades.jsonl."""
    TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"  ERROR: Failed to write trade log: {e}")


def add_pending_stop(stops_data: dict, symbol: str, qty: int,
                     limit_price: float, order_id: str,
                     trail_pct: float, loss_pct: float,
                     signal: dict) -> dict:
    """Add a new PENDING_FILL entry to trailing_stops.json data."""
    now_iso = datetime.now(timezone.utc).isoformat()

    new_stop = {
        "symbol": symbol,
        "asset_class": "equity",
        "entry_order_id": order_id,
        "qty": qty,
        "entry_price": limit_price,
        "highest_price": limit_price,
        "floor_price": round(limit_price * (1 - loss_pct / 100), 2),
        "trail_pct": trail_pct,
        "loss_pct": loss_pct,
        "status": "PENDING_FILL",
        "trailing_stop_order_id": None,
        "opened_at": now_iso,
        "last_checked": now_iso,
        "signal_type": "momentum_reentry",
        "signal_strength": signal.get("strength", "MODERATE"),
        "indicators_at_entry": signal.get("indicators", {}),
    }

    stops_data["active_stops"].append(new_stop)
    return stops_data


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    now = datetime.now(timezone.utc)
    print("=" * 70)
    print("ATLAS Lite Equity Re-Entry Scanner")
    print(f"Run at: {now.isoformat()}")
    print(f"Paper mode: {PAPER}")
    print("=" * 70)

    # --- Step 1: Load config and state ---
    print("\n--- Loading config and state ---")
    watchlist = load_symbol_lists()
    if not watchlist:
        print("FATAL: Empty watchlist, nothing to scan")
        return 1

    params = load_strategy_params()
    stops_data = load_trailing_stops()
    regime_data = load_regime()

    regime = regime_data.get("current_regime", "RISK_OFF")
    composite_score = regime_data.get("composite_score", 0)
    print(f"  Regime: {regime} (composite: {composite_score})")

    # Risk params
    risk_params = params.get("risk", {})
    max_position_pct = risk_params.get("max_position_pct_equity", 10.0)
    cash_reserve_pct = risk_params.get("cash_reserve_pct", 20.0)

    # Trailing stop params (for the new stop entries)
    ts_params = params.get("trailing_stop", {})
    trail_pct = ts_params.get("trail_pct", 5.0)
    loss_pct = ts_params.get("loss_pct", 5.0)

    print(f"  Max position: {max_position_pct}% of equity")
    print(f"  Cash reserve: {cash_reserve_pct}%")
    print(f"  Trail/loss: {trail_pct}% / {loss_pct}%")

    # --- Step 2: Check regime ---
    if regime == "RISK_OFF":
        print(f"\nREGIME IS RISK_OFF -- skipping all entries")
        print("=" * 70)
        return 0

    # --- Step 3: Identify candidates ---
    print(f"\n--- Identifying candidates ---")
    active_equity = get_active_equity_symbols(stops_data)
    cooling_syms = get_recently_stopped_symbols(stops_data)
    print(f"  Active equity positions: {sorted(active_equity) if active_equity else 'none'}")
    print(f"  In cooling period: {sorted(cooling_syms) if cooling_syms else 'none'}")

    candidates = identify_candidates(watchlist, stops_data)
    if not candidates:
        print("\nNo candidates to scan (all symbols active or in cooling period)")
        print("=" * 70)
        return 0
    print(f"\n  Candidates to scan: {candidates}")

    # --- Step 4: Get account info ---
    print(f"\n--- Account info ---")
    cash, equity = get_account_info()
    if equity <= 0:
        print("FATAL: Could not get account equity")
        return 1
    print(f"  Cash: ${cash:,.2f}")
    print(f"  Equity: ${equity:,.2f}")
    print(f"  Cash ratio: {(cash / equity) * 100:.1f}%")

    # --- Step 5: Scan each candidate ---
    print(f"\n--- Scanning {len(candidates)} candidates for entry signals ---")
    signals = []
    for symbol in candidates:
        print(f"\n  [{symbol}] Fetching {BARS_LOOKBACK_DAYS} days of daily bars...")
        df = fetch_daily_bars(symbol)
        if df is None:
            continue
        print(f"  [{symbol}] Got {len(df)} bars, evaluating signal...")
        signal = evaluate_entry_signal(symbol, df)
        if signal is not None:
            signals.append(signal)
            print(f"  [{symbol}] >>> SIGNAL: {signal['strength']} "
                  f"({signal['reason']})")

    if not signals:
        print(f"\n--- No entry signals found ---")
        print("=" * 70)
        return 0

    print(f"\n--- {len(signals)} entry signals found ---")
    for sig in signals:
        print(f"  {sig['symbol']}: {sig['strength']} | RSI={sig['indicators']['rsi']}")

    # --- Step 6: Place orders ---
    print(f"\n--- Placing orders ---")
    orders_placed = 0
    orders_skipped = 0
    state_modified = False

    for signal in signals:
        symbol = signal["symbol"]
        price = signal["price"]
        limit_price = round(price * LIMIT_DISCOUNT, 2)

        print(f"\n  [{symbol}] Current price: ${price:.2f}, limit: ${limit_price:.2f}")

        # Size the position
        qty = calculate_position_size(equity, max_position_pct, regime, limit_price)
        if qty <= 0:
            print(f"  [{symbol}] SKIP: qty=0 (position too small)")
            orders_skipped += 1
            continue

        order_value = qty * limit_price
        print(f"  [{symbol}] Sizing: {qty} shares x ${limit_price:.2f} = ${order_value:,.2f}")

        # Cash reserve check
        if not check_cash_reserve(cash, equity, order_value, cash_reserve_pct):
            print(f"  [{symbol}] SKIP: would breach {cash_reserve_pct}% cash reserve")
            orders_skipped += 1
            continue

        # Place the order
        order = place_limit_buy(symbol, qty, limit_price)
        if order is None:
            orders_skipped += 1
            continue

        # Deduct from available cash for subsequent reserve checks
        cash -= order_value

        # Log the trade
        log_trade({
            "timestamp": now.isoformat(),
            "strategy": "TRAILING_STOP",
            "action": "BUY",
            "symbol": symbol,
            "asset_class": "equity",
            "qty": qty,
            "price": limit_price,
            "order_type": "limit",
            "order_id": str(order.id),
            "status": str(order.status),
            "signal_type": "momentum_reentry",
            "signal_strength": signal["strength"],
            "indicators": signal["indicators"],
            "regime": regime,
            "pnl": 0,
        })

        # Add PENDING_FILL to trailing stops state
        stops_data = add_pending_stop(
            stops_data, symbol, qty, limit_price,
            str(order.id), trail_pct, loss_pct, signal,
        )
        state_modified = True
        orders_placed += 1

    # --- Step 7: Save state ---
    if state_modified:
        print(f"\n--- Saving state ---")
        success = atomic_write_json(str(TRAILING_STOPS_FILE), stops_data)
        if success:
            print(f"  State saved to {TRAILING_STOPS_FILE}")
        else:
            print(f"  FATAL: Could not save state to {TRAILING_STOPS_FILE}")
            return 1

    # --- Summary ---
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"  Regime:          {regime}")
    print(f"  Watchlist:       {len(watchlist)} symbols")
    print(f"  Candidates:      {len(candidates)} symbols")
    print(f"  Signals:         {len(signals)} entries")
    print(f"  Orders placed:   {orders_placed}")
    print(f"  Orders skipped:  {orders_skipped}")
    total_active = len([s for s in stops_data.get("active_stops", [])
                        if s.get("asset_class") == "equity"])
    print(f"  Active equities: {total_active}")
    print(f"{'=' * 70}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
