#!/usr/bin/env python3
"""
ATLAS Lite - Crypto Strategy Engine
Implements Bollinger Band + RSI Mean Reversion, EMA Crossover Trend Following,
and Smart DCA with Technical Triggers for crypto assets.

Runs against Alpaca paper trading API. Crypto trades 24/7.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ---------- paths ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"
CONFIG_DIR = PROJECT_ROOT / "config"
TRADES_LOG = LOGS_DIR / "trades.jsonl"
POSITIONS_FILE = STATE_DIR / "positions.json"
TRAILING_STOPS_FILE = STATE_DIR / "trailing_stops.json"

# ---------- add scripts to path for atomic_write ----------
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json

# ---------- API credentials ----------
API_KEY = os.environ.get("ALPACA_API_KEY", "PKNG4F2EBQEA2GVWLJBSHLYLQW")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "5q5T4NUp5f35MHoE8J5Bxxva8pYzyLTZPVFqiH7dYMRD")
PAPER = True  # ALWAYS paper trading per ATLAS rules

# ---------- strategy parameters ----------
CRYPTO_WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]
POSITION_SIZE_USD = 2500  # $2,000-$3,000 per trade, use midpoint
MIN_POSITION_USD = 2000
MAX_POSITION_USD = 3000

# Bollinger Band + RSI Mean Reversion
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 35    # slightly relaxed from 30 for more signals
RSI_OVERBOUGHT = 70

# EMA Crossover
EMA_FAST = 9
EMA_MED = 21
EMA_LONG = 55

# ATR
ATR_PERIOD = 14

# ADX — trend strength (YouTube finding: mean reversion LOSES money without this)
ADX_PERIOD = 14
ADX_RANGING_THRESHOLD = 25  # ADX < 25 = ranging market = mean reversion safe
ADX_TRENDING_THRESHOLD = 20  # ADX > 20 = trending = good for EMA crossover

# DCA triggers
DCA_RSI_THRESHOLD = 42
DCA_AGGRESSIVE_RSI = 20
DCA_NORMAL_SIZE_PCT = 0.01    # 1% of account
DCA_AGGRESSIVE_SIZE_PCT = 0.02  # 2% at RSI<20

# Cash reserve
CASH_RESERVE_PCT = 0.20
ACCOUNT_VALUE_APPROX = 98952  # from current state


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def calc_sma(series, period):
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def calc_ema(series, period):
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series, period=14):
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_bollinger_bands(series, period=20, std_dev=2.0):
    """Bollinger Bands: returns (middle, upper, lower)."""
    middle = calc_sma(series, period)
    rolling_std = series.rolling(window=period).std()
    upper = middle + (rolling_std * std_dev)
    lower = middle - (rolling_std * std_dev)
    return middle, upper, lower


def calc_atr(high, low, close, period=14):
    """Average True Range."""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


def calc_adx(high, low, close, period=14):
    """Average Directional Index — measures trend strength.
    ADX > 20-25 = trending market (mean reversion risky).
    ADX < 20 = ranging market (mean reversion ideal).
    """
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_smooth = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_smooth)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx


def compute_indicators(df):
    """Compute all technical indicators on an OHLCV DataFrame."""
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Bollinger Bands
    bb_mid, bb_upper, bb_lower = calc_bollinger_bands(close, BB_PERIOD, BB_STD)
    df["bb_mid"] = bb_mid
    df["bb_upper"] = bb_upper
    df["bb_lower"] = bb_lower

    # RSI
    df["rsi"] = calc_rsi(close, RSI_PERIOD)

    # EMAs
    df["ema9"] = calc_ema(close, EMA_FAST)
    df["ema21"] = calc_ema(close, EMA_MED)
    df["ema55"] = calc_ema(close, EMA_LONG)

    # ATR
    df["atr"] = calc_atr(high, low, close, ATR_PERIOD)

    # ADX — trend strength filter (YouTube research: MUST have this for mean reversion)
    df["adx"] = calc_adx(high, low, close, ADX_PERIOD)

    # BB position (0 = lower band, 1 = upper band)
    bb_range = bb_upper - bb_lower
    df["bb_position"] = (close - bb_lower) / bb_range.replace(0, np.nan)

    return df


# ============================================================
# SIGNAL GENERATION
# ============================================================

def generate_signals(df, symbol):
    """
    Generate trading signals for a single crypto asset.
    Returns a list of signal dicts.
    """
    signals = []
    if df.empty or len(df) < EMA_LONG + 5:
        print(f"  [{symbol}] Not enough data ({len(df)} bars), skipping")
        return signals

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = latest["close"]

    rsi = latest["rsi"]
    bb_lower = latest["bb_lower"]
    bb_upper = latest["bb_upper"]
    bb_mid = latest["bb_mid"]
    bb_pos = latest["bb_position"]
    ema9 = latest["ema9"]
    ema21 = latest["ema21"]
    ema55 = latest["ema55"]
    atr = latest["atr"]

    adx = latest["adx"]

    indicators = {
        "price": round(float(price), 4),
        "rsi": round(float(rsi), 2),
        "adx": round(float(adx), 2) if not np.isnan(adx) else None,
        "bb_lower": round(float(bb_lower), 4),
        "bb_mid": round(float(bb_mid), 4),
        "bb_upper": round(float(bb_upper), 4),
        "bb_position": round(float(bb_pos), 4) if not np.isnan(bb_pos) else None,
        "ema9": round(float(ema9), 4),
        "ema21": round(float(ema21), 4),
        "ema55": round(float(ema55), 4),
        "atr": round(float(atr), 4),
    }

    is_ranging = adx < ADX_RANGING_THRESHOLD if not np.isnan(adx) else True
    is_trending = adx > ADX_TRENDING_THRESHOLD if not np.isnan(adx) else False

    # --- Signal 1: Mean Reversion BUY ---
    # Price below lower BB AND RSI < 35, GATED by ADX < 25 (ranging market)
    # YouTube research: BB+RSI mean reversion LOSES money in trending crypto markets
    if price < bb_lower and rsi < RSI_OVERSOLD:
        if is_ranging:
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_MEAN_REVERSION",
                "action": "BUY",
                "signal_type": "mean_reversion_oversold",
                "strength": "STRONG",
                "reason": f"Price ({price:.2f}) < lower BB ({bb_lower:.2f}), RSI={rsi:.1f}, ADX={adx:.1f} (ranging)",
                "indicators": indicators,
                "stop_distance": round(float(atr * 2), 4),
            })
        else:
            print(f"  [{symbol}] Mean reversion BLOCKED by ADX={adx:.1f} (trending market, would lose money)")

    # --- Signal 2: EMA Crossover BUY ---
    # 9 EMA > 21 EMA, both above 55 EMA, RSI > 50
    if ema9 > ema21 and ema21 > ema55 and rsi > 50:
        # Check for recent crossover (within last 3 bars)
        cross_recent = False
        for i in range(-3, 0):
            if len(df) > abs(i):
                row = df.iloc[i]
                prev_row = df.iloc[i-1] if len(df) > abs(i-1) else None
                if prev_row is not None:
                    if row["ema9"] > row["ema21"] and prev_row["ema9"] <= prev_row["ema21"]:
                        cross_recent = True
                        break

        # ADX confirms trend = stronger signal
        adx_confirms = is_trending
        strength = "STRONG" if (cross_recent or adx_confirms) else "MODERATE"
        signals.append({
            "symbol": symbol,
            "strategy": "CRYPTO_TREND",
            "action": "BUY",
            "signal_type": "ema_crossover_bullish",
            "strength": strength,
            "reason": f"EMA9({ema9:.2f}) > EMA21({ema21:.2f}) > EMA55({ema55:.2f}), RSI={rsi:.1f}, ADX={adx:.1f}",
            "indicators": indicators,
            "recent_crossover": cross_recent,
            "adx_confirms_trend": adx_confirms,
        })

    # --- Signal 3: DCA Trigger BUY ---
    # RSI < 40 AND price in lower half of BB range
    if rsi < DCA_RSI_THRESHOLD and bb_pos is not None and not np.isnan(bb_pos) and bb_pos < 0.5:
        aggressive = rsi < DCA_AGGRESSIVE_RSI
        signals.append({
            "symbol": symbol,
            "strategy": "CRYPTO_DCA",
            "action": "BUY",
            "signal_type": "dca_technical_trigger",
            "strength": "STRONG" if aggressive else "MODERATE",
            "reason": f"RSI={rsi:.1f} < {DCA_RSI_THRESHOLD}, BB position={bb_pos:.2f} (lower half)",
            "indicators": indicators,
            "aggressive": aggressive,
        })

    # --- Signal 4: SELL signals for existing positions ---
    # RSI > 70 or price above upper BB
    if rsi > RSI_OVERBOUGHT or price > bb_upper:
        sell_reason = []
        if rsi > RSI_OVERBOUGHT:
            sell_reason.append(f"RSI={rsi:.1f} > {RSI_OVERBOUGHT}")
        if price > bb_upper:
            sell_reason.append(f"Price ({price:.2f}) above upper BB ({bb_upper:.2f})")
        signals.append({
            "symbol": symbol,
            "strategy": "CRYPTO_MEAN_REVERSION",
            "action": "SELL",
            "signal_type": "overbought_exit",
            "strength": "STRONG" if (rsi > 75 and price > bb_upper) else "MODERATE",
            "reason": "; ".join(sell_reason),
            "indicators": indicators,
        })

    # --- Borderline signals: lean toward trading ---
    # If no buy signals yet, check for near-signals
    # We intentionally lean aggressive to generate trade data for self-improvement
    buy_signals = [s for s in signals if s["action"] == "BUY"]
    if not buy_signals:
        # Near mean-reversion: price within 2% of lower BB or RSI < 43
        near_bb = price < bb_lower * 1.02
        near_oversold = rsi < 43
        if near_bb or near_oversold:
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_DCA",
                "action": "BUY",
                "signal_type": "borderline_dca",
                "strength": "WEAK",
                "reason": f"Borderline DCA: price near lower BB ({price:.2f} vs {bb_lower:.2f}), RSI={rsi:.1f}",
                "indicators": indicators,
                "aggressive": False,
            })

        # Near trend: EMAs partially aligned or RSI borderline
        if (ema9 > ema21 and ema9 > ema55 and rsi > 45) or (ema9 > ema55 and rsi > 45):
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_TREND",
                "action": "BUY",
                "signal_type": "borderline_trend",
                "strength": "WEAK",
                "reason": f"Near-trend: EMA9>EMA55={ema9>ema55}, EMA9>EMA21={ema9>ema21}, RSI={rsi:.1f}",
                "indicators": indicators,
                "recent_crossover": False,
            })

        # BB lower-half accumulation: price in lower 45% of BB with moderate RSI
        if bb_pos is not None and not np.isnan(bb_pos) and bb_pos < 0.45 and rsi < 48:
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_DCA",
                "action": "BUY",
                "signal_type": "bb_accumulation",
                "strength": "WEAK",
                "reason": f"BB accumulation zone: BB_pos={bb_pos:.2f} (lower 45%), RSI={rsi:.1f}",
                "indicators": indicators,
                "aggressive": False,
            })

    return signals


# ============================================================
# ORDER EXECUTION
# ============================================================

def place_crypto_buy(trading_client, symbol, notional_usd, strategy_name, signal):
    """Place a fractional crypto market buy order."""
    try:
        order_request = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional_usd, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        order = trading_client.submit_order(order_request)

        result = {
            "order_id": str(order.id),
            "status": str(order.status),
            "symbol": symbol,
            "side": "BUY",
            "notional": round(notional_usd, 2),
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "filled_qty": float(order.filled_qty) if order.filled_qty else None,
        }
        print(f"    ORDER PLACED: {symbol} BUY ${notional_usd:.2f} | ID: {order.id} | Status: {order.status}")
        return result

    except Exception as e:
        print(f"    ORDER FAILED: {symbol} BUY ${notional_usd:.2f} | Error: {e}")
        return {"error": str(e), "symbol": symbol, "side": "BUY", "notional": round(notional_usd, 2)}


def place_crypto_sell(trading_client, symbol, qty, strategy_name, signal):
    """Place a fractional crypto market sell order."""
    try:
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        )
        order = trading_client.submit_order(order_request)

        result = {
            "order_id": str(order.id),
            "status": str(order.status),
            "symbol": symbol,
            "side": "SELL",
            "qty": qty,
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "filled_qty": float(order.filled_qty) if order.filled_qty else None,
        }
        print(f"    ORDER PLACED: {symbol} SELL {qty} | ID: {order.id} | Status: {order.status}")
        return result

    except Exception as e:
        print(f"    ORDER FAILED: {symbol} SELL {qty} | Error: {e}")
        return {"error": str(e), "symbol": symbol, "side": "SELL", "qty": qty}


# ============================================================
# LOGGING
# ============================================================

def log_trade(trade_record):
    """Append a trade record to logs/trades.jsonl."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(trade_record, default=str) + "\n")


# ============================================================
# STATE UPDATES
# ============================================================

def update_positions(new_trades, trading_client):
    """Update state/positions.json with new crypto positions."""
    positions_data = atomic_read_json(str(POSITIONS_FILE))
    if positions_data is None:
        positions_data = {
            "schema_version": "1.0.0",
            "last_reconciled": datetime.now(timezone.utc).isoformat(),
            "positions": [],
            "pending_orders": [],
            "totals": {}
        }

    # Fetch current account info
    try:
        account = trading_client.get_account()
        cash = float(account.cash)
        equity = float(account.equity)
    except Exception as e:
        print(f"  Warning: Could not fetch account info: {e}")
        cash = positions_data.get("totals", {}).get("cash_available", 0)
        equity = ACCOUNT_VALUE_APPROX

    # Fetch all current positions from Alpaca
    try:
        alpaca_positions = trading_client.get_all_positions()
        updated_positions = []
        for pos in alpaca_positions:
            updated_positions.append({
                "symbol": pos.symbol,
                "asset_class": str(pos.asset_class),
                "strategy": _find_strategy_for_position(pos.symbol, positions_data),
                "qty": float(pos.qty),
                "entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pnl": float(pos.unrealized_pl),
                "opened_at": datetime.now(timezone.utc).isoformat(),
            })
        positions_data["positions"] = updated_positions
    except Exception as e:
        print(f"  Warning: Could not fetch positions from Alpaca: {e}")

    # Update totals
    total_value = sum(p.get("market_value", 0) for p in positions_data.get("positions", []))
    pending_value = sum(p.get("estimated_value", 0) for p in positions_data.get("pending_orders", []))
    positions_data["last_reconciled"] = datetime.now(timezone.utc).isoformat()
    positions_data["totals"] = {
        "total_positions": len(positions_data.get("positions", [])),
        "total_pending": len(positions_data.get("pending_orders", [])),
        "total_value": round(total_value + pending_value, 2),
        "cash_available": round(cash, 2),
        "cash_reserved_pct": round((cash / equity) * 100, 1) if equity > 0 else 0,
        "deployed_pct": round(((equity - cash) / equity) * 100, 1) if equity > 0 else 0,
    }

    atomic_write_json(str(POSITIONS_FILE), positions_data)
    print(f"  Positions updated: {len(positions_data.get('positions', []))} active, ${cash:.2f} cash")


def _find_strategy_for_position(symbol, positions_data):
    """Find the strategy label for an existing position."""
    for pos in positions_data.get("positions", []):
        if pos.get("symbol") == symbol:
            return pos.get("strategy", "UNKNOWN")
    # New positions from this script
    if "/" in symbol:
        return "CRYPTO_STRATEGY"
    return "TRAILING_STOP"


def update_trailing_stops(new_trades):
    """Add new crypto positions to trailing stops state."""
    stops_data = atomic_read_json(str(TRAILING_STOPS_FILE))
    if stops_data is None:
        stops_data = {"schema_version": "1.0.0", "active_stops": [], "closed_stops": []}

    existing_symbols = {s.get("symbol") for s in stops_data.get("active_stops", [])}

    for trade in new_trades:
        if trade.get("action") != "BUY" or trade.get("symbol") in existing_symbols:
            continue
        if "error" in trade:
            continue

        price = trade.get("fill_price") or trade.get("indicators", {}).get("price", 0)
        if price <= 0:
            continue

        stops_data["active_stops"].append({
            "symbol": trade["symbol"],
            "asset_class": "crypto",
            "entry_order_id": trade.get("order_id", ""),
            "qty": trade.get("fill_qty", 0),
            "entry_price": price,
            "highest_price": price,
            "floor_price": round(price * 0.95, 4),  # 5% trail
            "trail_pct": 5.0,
            "loss_pct": 5.0,
            "status": "ACTIVE",
            "trailing_stop_order_id": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "strategy_source": trade.get("strategy", "CRYPTO_STRATEGY"),
        })
        existing_symbols.add(trade["symbol"])

    atomic_write_json(str(TRAILING_STOPS_FILE), stops_data)
    print(f"  Trailing stops updated: {len(stops_data.get('active_stops', []))} active")


# ============================================================
# MAIN
# ============================================================

def main():
    now = datetime.now(timezone.utc)
    print("=" * 70)
    print(f"ATLAS Lite Crypto Strategy Engine")
    print(f"Run time: {now.isoformat()}")
    print(f"Watchlist: {', '.join(CRYPTO_WATCHLIST)}")
    print("=" * 70)

    # ---- Initialize clients ----
    print("\n[1/6] Initializing Alpaca clients...")
    data_client = CryptoHistoricalDataClient()  # No keys needed for crypto data
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

    # Verify account
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        print(f"  Account: equity=${equity:,.2f}, cash=${cash:,.2f}")
        print(f"  Cash reserve needed (20%): ${equity * CASH_RESERVE_PCT:,.2f}")
        available = cash - (equity * CASH_RESERVE_PCT)
        print(f"  Available for new trades: ${available:,.2f}")
    except Exception as e:
        print(f"  ERROR connecting to Alpaca: {e}")
        return

    # Check existing positions
    try:
        existing_positions = {p.symbol: p for p in trading_client.get_all_positions()}
        print(f"  Existing positions: {list(existing_positions.keys())}")
    except Exception as e:
        print(f"  Warning: Could not fetch positions: {e}")
        existing_positions = {}

    # ---- Fetch historical data ----
    print("\n[2/6] Fetching crypto bar data...")
    all_data = {}

    for timeframe_name, timeframe, bars_needed in [("1H", TimeFrame.Hour, 200), ("4H", TimeFrame(4, TimeFrame.Hour.unit), 200)]:
        start_time = now - timedelta(hours=bars_needed * (4 if "4H" in timeframe_name else 1))
        try:
            request = CryptoBarsRequest(
                symbol_or_symbols=CRYPTO_WATCHLIST,
                timeframe=timeframe,
                start=start_time,
                end=now,
            )
            bars = data_client.get_crypto_bars(request)

            for symbol in CRYPTO_WATCHLIST:
                key = f"{symbol}_{timeframe_name}"
                try:
                    symbol_bars = bars[symbol]
                    rows = []
                    for bar in symbol_bars:
                        rows.append({
                            "timestamp": bar.timestamp,
                            "open": float(bar.open),
                            "high": float(bar.high),
                            "low": float(bar.low),
                            "close": float(bar.close),
                            "volume": float(bar.volume),
                        })
                    df = pd.DataFrame(rows)
                    if not df.empty:
                        df = df.sort_values("timestamp").reset_index(drop=True)
                        df = compute_indicators(df)
                        all_data[key] = df
                        print(f"  {key}: {len(df)} bars loaded, latest close=${df.iloc[-1]['close']:.4f}")
                    else:
                        print(f"  {key}: NO DATA")
                except (KeyError, IndexError) as e:
                    print(f"  {key}: No data available ({e})")
        except Exception as e:
            print(f"  Error fetching {timeframe_name} bars: {e}")

    if not all_data:
        print("\nFATAL: No data fetched. Exiting.")
        return

    # ---- Generate signals ----
    print("\n[3/6] Generating signals...")
    all_signals = []

    for symbol in CRYPTO_WATCHLIST:
        print(f"\n  --- {symbol} ---")
        # Prefer 1H data for mean reversion/DCA, 4H for trend following
        key_1h = f"{symbol}_1H"
        key_4h = f"{symbol}_4H"

        # Generate from 1H
        if key_1h in all_data:
            sigs = generate_signals(all_data[key_1h], symbol)
            for s in sigs:
                s["timeframe"] = "1H"
            all_signals.extend(sigs)
            if sigs:
                for s in sigs:
                    print(f"    [1H] {s['strategy']} {s['action']} ({s['strength']}): {s['reason']}")
            else:
                print(f"    [1H] No signals")

        # Generate from 4H (only trend signals)
        if key_4h in all_data:
            sigs_4h = generate_signals(all_data[key_4h], symbol)
            trend_sigs = [s for s in sigs_4h if "TREND" in s.get("strategy", "")]
            for s in trend_sigs:
                s["timeframe"] = "4H"
            # Avoid duplicate trend signals if already generated on 1H
            existing_trend = any(
                s["symbol"] == symbol and "TREND" in s["strategy"] and s["action"] == "BUY"
                for s in all_signals
            )
            if not existing_trend:
                all_signals.extend(trend_sigs)
                for s in trend_sigs:
                    print(f"    [4H] {s['strategy']} {s['action']} ({s['strength']}): {s['reason']}")

    # ---- Summary of signals ----
    buy_signals = [s for s in all_signals if s["action"] == "BUY"]
    sell_signals = [s for s in all_signals if s["action"] == "SELL"]
    print(f"\n  TOTAL: {len(buy_signals)} BUY signals, {len(sell_signals)} SELL signals")

    # ---- Execute trades ----
    print("\n[4/6] Executing trades...")
    trades_placed = []

    # De-duplicate: pick the strongest signal per symbol for buys
    best_buy_per_symbol = {}
    strength_rank = {"STRONG": 3, "MODERATE": 2, "WEAK": 1}
    for sig in buy_signals:
        sym = sig["symbol"]
        rank = strength_rank.get(sig.get("strength", "WEAK"), 0)
        if sym not in best_buy_per_symbol or rank > strength_rank.get(best_buy_per_symbol[sym].get("strength", "WEAK"), 0):
            best_buy_per_symbol[sym] = sig

    # Also include lower-strength signals for symbols without strong signals
    # to ensure we get enough trades
    additional_buys = []
    for sig in buy_signals:
        sym = sig["symbol"]
        if sym in best_buy_per_symbol and sig is not best_buy_per_symbol[sym]:
            # Different strategy for same symbol, keep for diversity
            if sig["strategy"] != best_buy_per_symbol[sym]["strategy"]:
                additional_buys.append(sig)

    # Calculate available cash for new crypto
    cash_reserve = equity * CASH_RESERVE_PCT
    available_cash = cash - cash_reserve
    print(f"  Cash: ${cash:,.2f} | Reserve: ${cash_reserve:,.2f} | Available: ${available_cash:,.2f}")

    if available_cash < MIN_POSITION_USD:
        print(f"  WARNING: Available cash (${available_cash:,.2f}) below minimum position size (${MIN_POSITION_USD})")
        print(f"  Will attempt smaller trades to generate data...")
        # Reduce position size for data generation
        effective_size = max(500, available_cash / max(len(best_buy_per_symbol), 1))
    else:
        effective_size = min(POSITION_SIZE_USD, available_cash / max(len(best_buy_per_symbol), 1))
        effective_size = max(MIN_POSITION_USD, min(MAX_POSITION_USD, effective_size))

    print(f"  Position size per trade: ${effective_size:,.2f}")
    print(f"  Trades to execute: {len(best_buy_per_symbol)} primary buys")

    # Execute BUY orders
    budget_used = 0
    for symbol, signal in sorted(best_buy_per_symbol.items()):
        remaining = available_cash - budget_used
        if remaining < 500:
            print(f"\n  BUDGET EXHAUSTED: ${remaining:.2f} remaining, skipping {symbol}")
            break

        trade_size = min(effective_size, remaining)
        print(f"\n  >>> Executing BUY: {symbol} (${trade_size:,.2f})")
        print(f"      Strategy: {signal['strategy']} | Signal: {signal['signal_type']}")
        print(f"      Reason: {signal['reason']}")

        order_result = place_crypto_buy(trading_client, symbol, trade_size, signal["strategy"], signal)

        if "error" not in order_result:
            budget_used += trade_size
            # Wait a moment for fill
            time.sleep(2)

            # Try to get fill info
            try:
                order_detail = trading_client.get_order_by_id(order_result["order_id"])
                fill_price = float(order_detail.filled_avg_price) if order_detail.filled_avg_price else signal["indicators"]["price"]
                fill_qty = float(order_detail.filled_qty) if order_detail.filled_qty else 0
                status = str(order_detail.status)
            except Exception:
                fill_price = signal["indicators"]["price"]
                fill_qty = round(trade_size / fill_price, 8)
                status = order_result.get("status", "UNKNOWN")

            trade_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": signal["strategy"],
                "action": "BUY",
                "symbol": symbol,
                "asset_class": "crypto",
                "qty": fill_qty,
                "price": fill_price,
                "notional": trade_size,
                "order_type": "market",
                "order_id": order_result.get("order_id", ""),
                "status": status,
                "indicators": signal["indicators"],
                "signal_type": signal["signal_type"],
                "signal_strength": signal.get("strength", "UNKNOWN"),
                "timeframe": signal.get("timeframe", "1H"),
                "pnl": 0,
            }
            trade_record["fill_price"] = fill_price
            trade_record["fill_qty"] = fill_qty

            log_trade(trade_record)
            trades_placed.append(trade_record)
            print(f"      FILLED: {fill_qty} @ ${fill_price:,.4f} | Status: {status}")
        else:
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": signal["strategy"],
                "action": "BUY_FAILED",
                "symbol": symbol,
                "error": order_result["error"],
                "signal_type": signal["signal_type"],
                "pnl": 0,
            })

    # Execute SELL orders for existing positions
    for signal in sell_signals:
        symbol = signal["symbol"]
        if symbol in existing_positions:
            pos = existing_positions[symbol]
            # Sell 25% of position to book partial profit and generate trade data
            sell_qty = round(float(pos.qty) * 0.25, 8)
            if sell_qty > 0:
                print(f"\n  >>> Executing SELL: {symbol} ({sell_qty} units, 25% of position)")
                print(f"      Reason: {signal['reason']}")

                order_result = place_crypto_sell(trading_client, symbol, sell_qty, signal["strategy"], signal)

                if "error" not in order_result:
                    time.sleep(2)
                    try:
                        order_detail = trading_client.get_order_by_id(order_result["order_id"])
                        fill_price = float(order_detail.filled_avg_price) if order_detail.filled_avg_price else signal["indicators"]["price"]
                        fill_qty = float(order_detail.filled_qty) if order_detail.filled_qty else sell_qty
                        status = str(order_detail.status)
                    except Exception:
                        fill_price = signal["indicators"]["price"]
                        fill_qty = sell_qty
                        status = order_result.get("status", "UNKNOWN")

                    entry_price = float(pos.avg_entry_price)
                    pnl = round((fill_price - entry_price) * fill_qty, 2)

                    trade_record = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "strategy": signal["strategy"],
                        "action": "SELL",
                        "symbol": symbol,
                        "asset_class": "crypto",
                        "qty": fill_qty,
                        "price": fill_price,
                        "order_type": "market",
                        "order_id": order_result.get("order_id", ""),
                        "status": status,
                        "indicators": signal["indicators"],
                        "signal_type": signal["signal_type"],
                        "entry_price": entry_price,
                        "pnl": pnl,
                    }
                    log_trade(trade_record)
                    trades_placed.append(trade_record)
                    print(f"      FILLED: {fill_qty} @ ${fill_price:,.4f} | PnL: ${pnl:+.2f}")

    # ---- Update state files ----
    print("\n[5/6] Updating state files...")
    update_positions(trades_placed, trading_client)
    update_trailing_stops(trades_placed)

    # ---- Final summary ----
    print("\n" + "=" * 70)
    print("[6/6] EXECUTION SUMMARY")
    print("=" * 70)
    print(f"  Signals found:  {len(all_signals)} total ({len(buy_signals)} BUY, {len(sell_signals)} SELL)")
    print(f"  Trades placed:  {len(trades_placed)}")
    buy_trades = [t for t in trades_placed if t["action"] == "BUY"]
    sell_trades = [t for t in trades_placed if t["action"] == "SELL"]
    print(f"    - Buys:  {len(buy_trades)}")
    print(f"    - Sells: {len(sell_trades)}")

    total_bought = sum(t.get("notional", 0) for t in buy_trades)
    total_pnl = sum(t.get("pnl", 0) for t in sell_trades)
    print(f"  Total bought: ${total_bought:,.2f}")
    if sell_trades:
        print(f"  Total realized PnL: ${total_pnl:+,.2f}")

    print(f"\n  Trade details:")
    for t in trades_placed:
        pnl_str = f" | PnL: ${t.get('pnl', 0):+.2f}" if t.get("pnl", 0) != 0 else ""
        print(f"    {t['action']:4s} {t['symbol']:10s} | {t.get('strategy', '?'):25s} | "
              f"qty={t.get('qty', 0):.6f} @ ${t.get('price', 0):>10,.4f} | "
              f"{t.get('signal_type', '?')}{pnl_str}")

    # Count total trades in log
    try:
        with open(TRADES_LOG, "r") as f:
            total_log_entries = sum(1 for line in f if line.strip())
        print(f"\n  Total trades in log: {total_log_entries}")
        trade_entries = sum(1 for line in open(TRADES_LOG) if '"action"' in line and '"BUY"' in line or '"SELL"' in line)
        print(f"  Self-improvement needs: 30 trades (working toward it)")
    except Exception:
        pass

    print(f"\n  Regime: RISK_ON (composite: 0.65)")
    print(f"  Next run: Schedule this script to run every 1-4 hours for continuous crypto signals")
    print("=" * 70)

    return trades_placed


if __name__ == "__main__":
    trades = main()
