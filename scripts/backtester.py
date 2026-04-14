#!/usr/bin/env python3
"""
ATLAS Lite Walk-Forward Backtesting Engine

Replays historical crypto bar data through compute_indicators + generate_signals
to test parameter combinations 1000x faster than live trading.

Uses walk-forward methodology: train on 60 days, test on 20 days.
Only the TEST window score determines parameter ranking (prevents overfitting).

Usage:
    python scripts/backtester.py --test                  # Self-test on BTC/USD
    python scripts/backtester.py --rank <signal_type>    # Grid-search a signal
"""

import copy
import itertools
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project paths and imports
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Import strategy functions -- set QUIET IMMEDIATELY to suppress 400K prints
import crypto_strategy
crypto_strategy.QUIET = True

from crypto_strategy import compute_indicators, generate_signals, load_params

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

# Import self-improvement scoring and param maps
from self_improvement import (
    SIGNAL_PARAM_MAP,
    DEFAULT_BOUNDS,
    composite_score,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SLIPPAGE = 0.001  # Fixed 0.10% for all backtests -- do NOT read live model
MIN_TEST_TRADES = 15  # Minimum simulated trades to accept a result

# Out-of-sample symbols for overfit detection
OOS_SYMBOLS = ["NEAR/USD", "ATOM/USD", "FTM/USD", "OP/USD"]


# ===========================================================================
# 1. fetch_historical_bars
# ===========================================================================

def fetch_historical_bars(symbols, days_back, timeframe_str="1H"):
    """
    Fetch historical crypto bars from Alpaca.

    Args:
        symbols: list of crypto pair strings (e.g. ["BTC/USD", "ETH/USD"])
        days_back: number of calendar days of history to fetch
        timeframe_str: one of "1H", "4H", "1D"

    Returns:
        dict mapping symbol -> pandas DataFrame with columns:
        [timestamp, open, high, low, close, volume]
    """
    client = CryptoHistoricalDataClient()  # No API keys needed for crypto data

    tf_map = {
        "15M": TimeFrame(15, TimeFrame.Minute.unit),
        "1H": TimeFrame.Hour,
        "4H": TimeFrame(4, TimeFrame.Hour.unit),
        "1D": TimeFrame.Day,
    }
    timeframe = tf_map.get(timeframe_str, TimeFrame.Hour)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)

    request = CryptoBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=now,
    )
    bars = client.get_crypto_bars(request)

    result = {}
    for symbol in symbols:
        try:
            rows = [
                {
                    "timestamp": b.timestamp,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
                for b in bars[symbol]
            ]
            df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
            if not df.empty:
                result[symbol] = df
        except (KeyError, IndexError):
            pass

    return result


# ===========================================================================
# 2. simulate_trades
# ===========================================================================

def simulate_trades(bars_df, symbol, params, initial_cash=100000, position_size_usd=4000):
    """
    Replay bars through compute_indicators + generate_signals, simulating portfolio.

    Design constraints (from adversarial review):
    - Stop check uses bar.close <= floor (not bar.low) -- matches real 5-min monitor
    - DCA signals only execute when symbol already in positions (post-filter)
    - Fixed 0.10% slippage on all fills
    - generate_signals called with dry_run=True to skip log_blocked_signal calls

    Args:
        bars_df: DataFrame with OHLCV + timestamp columns
        symbol: crypto pair string
        params: strategy parameter dict (v2 format from load_params)
        initial_cash: starting simulated cash
        position_size_usd: base USD per trade

    Returns:
        dict with keys: trades, final_equity, max_drawdown, total_bars, total_trades
    """
    # Compute indicators ONCE on full DataFrame (all backward-looking, no look-ahead)
    df = compute_indicators(bars_df.copy(), params)

    # Warmup period: need enough bars for longest indicator to stabilize
    ema_long = params.get("shared", params).get("ema_long", 55)
    warmup = ema_long + 10

    if len(df) <= warmup:
        return {
            "trades": [],
            "final_equity": initial_cash,
            "max_drawdown": 0.0,
            "total_bars": 0,
            "total_trades": 0,
        }

    # Simulation state
    cash = float(initial_cash)
    positions = {}  # symbol -> position dict
    trades = []
    equity_curve = []

    # Get trailing stop and profit tier config from params
    trail_pct = params.get("trail_pct", 5.0)
    loss_pct = params.get("loss_pct", 5.0)
    profit_tiers = [
        {"gain_pct": 3, "sell_pct": 8},
        {"gain_pct": 5, "sell_pct": 10},
        {"gain_pct": 10, "sell_pct": 15},
        {"gain_pct": 20, "sell_pct": 20},
    ]

    for i in range(warmup, len(df)):
        bar = df.iloc[i]
        price = float(bar["close"])
        bar_time = str(bar.get("timestamp", i))

        # === Update trailing stop state and check exits ===
        if symbol in positions:
            pos = positions[symbol]

            # Ratchet trailing stop upward
            if price > pos["highest_price"]:
                pos["highest_price"] = price
                new_floor = price * (1 - trail_pct / 100)
                if new_floor > pos.get("floor_price", 0):
                    pos["floor_price"] = new_floor

            # Check floor breach using bar.close (adversarial review: NOT bar.low)
            floor = pos.get("floor_price", 0)
            if price <= floor and floor > 0:
                # STOP OUT
                fill_price = price * (1 - SLIPPAGE)
                pnl = (fill_price - pos["entry_price"]) * pos["qty"]
                trades.append({
                    "timestamp": bar_time,
                    "action": "STOP_SELL",
                    "symbol": symbol,
                    "signal_type": "trailing_stop_triggered",
                    "entry_signal_type": pos.get("entry_signal_type", "unknown"),
                    "qty": pos["qty"],
                    "price": round(fill_price, 6),
                    "entry_price": pos["entry_price"],
                    "pnl": round(pnl, 2),
                    "exit_reason": "trailing_stop_triggered",
                })
                cash += fill_price * pos["qty"]
                del positions[symbol]

            # Check profit tiers (only if still holding after stop check)
            elif symbol in positions:
                pos = positions[symbol]
                gain_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100
                tiers_hit = pos.get("profit_tiers_hit", [])
                original_qty = pos["qty"]  # Snapshot for tier calculation

                for tier in profit_tiers:
                    tier_gain = tier["gain_pct"]
                    if tier_gain in tiers_hit:
                        continue
                    if gain_pct >= tier_gain:
                        sell_qty = original_qty * tier["sell_pct"] / 100
                        if sell_qty > pos["qty"]:
                            sell_qty = pos["qty"]
                        if sell_qty <= 0:
                            continue

                        fill_price = price * (1 - SLIPPAGE)
                        pnl = (fill_price - pos["entry_price"]) * sell_qty
                        trades.append({
                            "timestamp": bar_time,
                            "action": "PROFIT_TAKE",
                            "symbol": symbol,
                            "signal_type": "profit_tier",
                            "entry_signal_type": pos.get("entry_signal_type", "unknown"),
                            "qty": sell_qty,
                            "price": round(fill_price, 6),
                            "entry_price": pos["entry_price"],
                            "pnl": round(pnl, 2),
                            "exit_reason": "profit_take",
                        })
                        cash += fill_price * sell_qty
                        pos["qty"] -= sell_qty
                        tiers_hit.append(tier_gain)
                        pos["profit_tiers_hit"] = tiers_hit

                # Clean up if position fully closed by profit tiers
                if symbol in positions and pos["qty"] <= 0.000001:
                    del positions[symbol]

        # === Generate signals via the real strategy engine ===
        # Pass df up to current bar (generate_signals reads the last row)
        df_slice = df.iloc[: i + 1]
        signals = generate_signals(df_slice, symbol, params, dry_run=True)

        # === Process signals ===
        for sig in signals:
            if sig["action"] != "BUY":
                # Process SELL signals (overbought exit) -- 25% partial sell like live
                if sig["action"] == "SELL" and symbol in positions:
                    pos = positions[symbol]
                    sell_qty = pos["qty"] * 0.25
                    if sell_qty > 0:
                        fill_price = price * (1 - SLIPPAGE)
                        pnl = (fill_price - pos["entry_price"]) * sell_qty
                        trades.append({
                            "timestamp": bar_time,
                            "action": "SELL",
                            "symbol": symbol,
                            "signal_type": sig.get("signal_type", "overbought_exit"),
                            "entry_signal_type": pos.get("entry_signal_type", "unknown"),
                            "qty": sell_qty,
                            "price": round(fill_price, 6),
                            "entry_price": pos["entry_price"],
                            "pnl": round(pnl, 2),
                            "exit_reason": "overbought_signal",
                        })
                        cash += fill_price * sell_qty
                        pos["qty"] -= sell_qty
                        if pos["qty"] <= 0.000001:
                            del positions[symbol]
                continue

            # --- BUY signal processing ---

            # DCA filter: dca_technical_trigger signals only execute when position exists
            if sig.get("signal_type") == "dca_technical_trigger" and symbol not in positions:
                continue

            # Skip if already holding (non-DCA) or not enough cash
            if symbol in positions and sig.get("signal_type") != "dca_technical_trigger":
                continue

            if cash < position_size_usd * 0.5:
                continue

            trade_size = min(position_size_usd, cash * 0.25)  # Max 25% of cash per trade
            fill_price = price * (1 + SLIPPAGE)
            qty = trade_size / fill_price

            if symbol in positions:
                # DCA add -- weighted average entry price
                pos = positions[symbol]
                old_qty = pos["qty"]
                old_entry = pos["entry_price"]
                new_total = old_qty + qty
                pos["entry_price"] = ((old_entry * old_qty) + (fill_price * qty)) / new_total
                pos["qty"] = new_total
            else:
                # New position
                floor_val = fill_price * (1 - loss_pct / 100)
                positions[symbol] = {
                    "qty": qty,
                    "entry_price": fill_price,
                    "highest_price": fill_price,
                    "floor_price": floor_val,
                    "entry_idx": i,
                    "entry_signal_type": sig.get("signal_type", "unknown"),
                    "profit_tiers_hit": [],
                }

            cash -= trade_size
            trades.append({
                "timestamp": bar_time,
                "action": "BUY",
                "symbol": symbol,
                "signal_type": sig.get("signal_type", "unknown"),
                "entry_signal_type": sig.get("signal_type", "unknown"),
                "qty": qty,
                "price": round(fill_price, 6),
                "pnl": 0,
                "strategy": sig.get("strategy", "CRYPTO_STRATEGY"),
                "entry_idx": i,  # Bar index for ML feature reconstruction
            })

        # Track equity at end of each bar
        pos_value = sum(p["qty"] * price for p in positions.values())
        equity = cash + pos_value
        equity_curve.append(equity)

    # Compute max drawdown from equity curve
    max_dd = 0.0
    peak = float(initial_cash)
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    final_equity = equity_curve[-1] if equity_curve else float(initial_cash)

    return {
        "trades": trades,
        "final_equity": round(final_equity, 2),
        "max_drawdown": round(max_dd, 2),
        "total_bars": len(df) - warmup,
        "total_trades": len(trades),
    }


# ===========================================================================
# 3. walk_forward_test
# ===========================================================================

def walk_forward_test(symbols, params_grid, train_days=60, test_days=20):
    """
    Run walk-forward backtest across multiple symbols and parameter sets.

    For each param set:
    - Split each symbol's bars into train / test windows
    - Score each window using composite_score (from self_improvement)
    - Average across symbols
    - Sort by TEST score (out-of-sample) to prevent overfitting

    Args:
        symbols: list of crypto pair strings
        params_grid: list of parameter dicts to test
        train_days: calendar days for in-sample training window
        test_days: calendar days for out-of-sample test window

    Returns:
        list of result dicts sorted by avg_test_score descending
    """
    total_days = train_days + test_days + 5  # +5 for warmup buffer
    all_bars = fetch_historical_bars(symbols, total_days)

    results = []
    for param_set in params_grid:
        train_scores = []
        test_scores = []
        all_test_trades = []

        for symbol in symbols:
            if symbol not in all_bars:
                continue
            df = all_bars[symbol]
            split_idx = int(len(df) * train_days / (train_days + test_days))

            train_df = df.iloc[:split_idx].reset_index(drop=True)
            test_df = df.iloc[split_idx:].reset_index(drop=True)

            if len(train_df) < 100 or len(test_df) < 50:
                continue

            train_result = simulate_trades(train_df, symbol, param_set)
            test_result = simulate_trades(test_df, symbol, param_set)

            train_closed = [t for t in train_result["trades"] if t.get("pnl", 0) != 0]
            test_closed = [t for t in test_result["trades"] if t.get("pnl", 0) != 0]

            if train_closed:
                ts, _tm, _td = composite_score(train_closed)
                train_scores.append(ts)
            if test_closed:
                ts, _tm, _td = composite_score(test_closed)
                test_scores.append(ts)
                all_test_trades.extend(test_closed)

        # Average across symbols
        avg_train = sum(train_scores) / len(train_scores) if train_scores else 0.0
        avg_test = sum(test_scores) / len(test_scores) if test_scores else 0.0

        results.append({
            "params": param_set,
            "avg_train_score": round(avg_train, 4),
            "avg_test_score": round(avg_test, 4),
            "test_trade_count": len(all_test_trades),
            "test_trades": all_test_trades,
        })

    # Sort by test score (out-of-sample) -- prevents overfitting
    results.sort(key=lambda r: r["avg_test_score"], reverse=True)
    return results


# ===========================================================================
# 4. generate_param_grid
# ===========================================================================

def generate_param_grid(base_params, signal_type, num_points=5, max_combos=50):
    """
    Generate parameter grid for a signal type spread across bounds.

    Reads SIGNAL_PARAM_MAP to find tunable params for the signal, then
    generates a cartesian product of values evenly spaced within each
    param's defined bounds.

    Args:
        base_params: baseline parameter dict (v2 format)
        signal_type: key into SIGNAL_PARAM_MAP (e.g. "ema_crossover_bullish")
        num_points: values per param dimension
        max_combos: cap on total grid size (random sample if exceeded)

    Returns:
        list of parameter dicts
    """
    mapping = SIGNAL_PARAM_MAP.get(signal_type)
    if not mapping:
        return [base_params]

    # Get signal-specific params from the base config
    signal_params = base_params.get("signal", {}).get(signal_type, {})
    tunable = [p for p in mapping["params"] if p != "_weight" and p in signal_params]

    if not tunable:
        return [base_params]

    # Generate evenly-spaced values for each tunable param
    param_values = {}
    for p in tunable:
        current = signal_params.get(p, 1)
        bounds_key = f"{p}_bounds"
        if bounds_key in signal_params:
            lo, hi = signal_params[bounds_key]
        elif p in DEFAULT_BOUNDS:
            lo, hi = DEFAULT_BOUNDS[p]
        else:
            lo, hi = current * 0.5, current * 1.5

        values = [lo + idx * (hi - lo) / (num_points - 1) for idx in range(num_points)]
        # Ensure the current live value is always included
        if current not in values:
            values.append(current)
        values.sort()
        param_values[p] = values

    # Cartesian product of all param dimensions, capped at max_combos
    keys = list(param_values.keys())
    all_combos = list(itertools.product(*[param_values[k] for k in keys]))

    if len(all_combos) > max_combos:
        random.shuffle(all_combos)
        all_combos = all_combos[:max_combos]

    # Build full param dicts for each combination
    grid = []
    for combo in all_combos:
        p = copy.deepcopy(base_params)
        sig = p.get("signal", {}).get(signal_type, {})
        for j, key in enumerate(keys):
            val = combo[j]
            # Preserve int types for integer params (periods, etc.)
            if isinstance(signal_params.get(key), int):
                val = int(round(val))
            else:
                val = round(val, 4)
            sig[key] = val
        if "signal" not in p:
            p["signal"] = {}
        p["signal"][signal_type] = sig
        grid.append(p)

    return grid


# ===========================================================================
# 5. backtest_and_rank
# ===========================================================================

def backtest_and_rank(signal_type, base_params=None, train_days=60, test_days=20, top_n=3):
    """
    Top-level orchestrator: grid search with walk-forward validation.

    1. Generate param grid for the signal type
    2. Run walk-forward test across traded + OOS symbols
    3. Filter results requiring minimum test trades
    4. Check out-of-sample consistency on held-out symbols
    5. Flag potential overfit (in-sample good, OOS bad)

    Args:
        signal_type: key into SIGNAL_PARAM_MAP
        base_params: baseline params (loaded from config if None)
        train_days: in-sample training window
        test_days: out-of-sample test window
        top_n: number of top candidates to return

    Returns:
        list of ranked result dicts with OOS scores and overfit flags
    """
    if base_params is None:
        base_params = load_params()

    # Traded symbols + out-of-sample for overfit detection
    traded = base_params.get(
        "watchlist",
        ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"],
    )
    all_symbols = traded + OOS_SYMBOLS

    print(f"  Backtesting {signal_type} across {len(all_symbols)} symbols...")

    grid = generate_param_grid(base_params, signal_type)
    print(f"  Testing {len(grid)} param combinations...")

    results = walk_forward_test(all_symbols, grid, train_days, test_days)

    # Filter: require minimum trades in test window
    valid = [r for r in results if r["test_trade_count"] >= MIN_TEST_TRADES]

    if not valid:
        print(f"  No valid results (all had < {MIN_TEST_TRADES} test trades)")
        return []

    # Check out-of-sample consistency for top candidates
    total_days = train_days + test_days + 5
    oos_bars = fetch_historical_bars(OOS_SYMBOLS, total_days)

    for r in valid[:top_n]:
        oos_scores = []
        for sym in OOS_SYMBOLS:
            if sym not in oos_bars:
                continue
            df = oos_bars[sym]
            split = int(len(df) * train_days / (train_days + test_days))
            test_df = df.iloc[split:].reset_index(drop=True)
            if len(test_df) < 50:
                continue
            res = simulate_trades(test_df, sym, r["params"])
            closed = [t for t in res["trades"] if t.get("pnl", 0) != 0]
            if closed:
                s, _m, _d = composite_score(closed)
                oos_scores.append(s)

        r["oos_avg_score"] = round(sum(oos_scores) / len(oos_scores), 4) if oos_scores else 0.0
        r["oos_count"] = len(oos_scores)
        # Flag if in-sample looks good but OOS is bad -- likely overfit
        if r["avg_test_score"] > 0 and r["oos_avg_score"] <= 0:
            r["overfit_flag"] = True
        else:
            r["overfit_flag"] = False

    return valid[:top_n]


# ===========================================================================
# CLI entry point
# ===========================================================================

if __name__ == "__main__":
    if "--test" in sys.argv:
        # Self-test mode: fetch 30 days of BTC/USD, simulate, print results
        params = load_params()
        print("Backtester self-test: BTC/USD with current params")
        bars = fetch_historical_bars(["BTC/USD"], 30)
        if "BTC/USD" in bars:
            result = simulate_trades(bars["BTC/USD"], "BTC/USD", params)
            print(f"  Bars: {result['total_bars']}")
            print(f"  Trades: {result['total_trades']}")
            print(f"  Final equity: ${result['final_equity']:,.2f}")
            print(f"  Max drawdown: {result['max_drawdown']:.1f}%")
            closed = [t for t in result["trades"] if t.get("pnl", 0) != 0]
            print(f"  Closed trades: {len(closed)}")
            if closed:
                total_pnl = sum(t["pnl"] for t in closed)
                print(f"  Total PnL: ${total_pnl:+,.2f}")
        else:
            print("  ERROR: No BTC/USD data fetched")

    elif "--rank" in sys.argv:
        # Grid-search mode: rank params for a signal type
        rank_idx = sys.argv.index("--rank")
        signal = (
            sys.argv[rank_idx + 1]
            if len(sys.argv) > rank_idx + 1
            else "ema_crossover_bullish"
        )
        print(f"Ranking params for {signal}...")
        ranked = backtest_and_rank(signal)
        for i, r in enumerate(ranked):
            oos = r.get("oos_avg_score", "N/A")
            overfit = r.get("overfit_flag", "N/A")
            print(
                f"  #{i+1}: test={r['avg_test_score']:.4f} "
                f"oos={oos} trades={r['test_trade_count']} "
                f"overfit={overfit}"
            )

    else:
        print("Usage: python backtester.py --test | --rank <signal_type>")
