#!/usr/bin/env python3
"""
ATLAS Lite Signal Performance Tracker.

Analyzes every trade in the log, pairs buys with sells to create round trips,
grades each signal type and strategy, and outputs a scoreboard that other
scripts can use to make smarter allocation and gating decisions.

Output: state/signal_scores.json
Usage:
    python scripts/performance_tracker.py          # standalone run
    from performance_tracker import load_signal_scores  # import from other scripts
"""

import json
import os
import sys
import statistics
from pathlib import Path
from datetime import datetime, timezone

# Ensure stdout handles unicode (Windows cp1252 fallback)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict

# ---------------------------------------------------------------------------
# Project paths & imports
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json

TRADES_LOG = PROJECT_ROOT / "logs" / "trades.jsonl"
EXPERIMENTS_FILE = PROJECT_ROOT / "state" / "experiments.json"
SCORES_FILE = PROJECT_ROOT / "state" / "signal_scores.json"
REGIME_FILE = PROJECT_ROOT / "state" / "regime.json"

# Signal types considered actual trade entries (not system events)
BUY_ACTIONS = {"BUY", "BUY_FILL"}
SELL_ACTIONS = {"SELL", "SELL_FILL"}


# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------
def normalize_symbol(symbol):
    """
    Normalize crypto symbol formats for consistent matching.
    BTCUSD -> BTC/USD, ETHUSD -> ETH/USD, etc.
    Leaves equity symbols unchanged.
    """
    if symbol.endswith("USD") and "/" not in symbol and len(symbol) > 3:
        return symbol[:-3] + "/USD"
    return symbol


def symbol_key(symbol):
    """Return a canonical key for matching buys to sells across symbol formats."""
    s = symbol.upper().replace("/", "").replace("-", "")
    return s


# ---------------------------------------------------------------------------
# Trade log loading
# ---------------------------------------------------------------------------
def load_trades(logfile=None):
    """
    Load trades from JSONL file, filtering to actual trade entries only.

    Returns:
        list: Trade dicts that represent actual buy/sell orders (not events).
    """
    if logfile is None:
        logfile = str(TRADES_LOG)
    trades = []
    try:
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Keep only actual trade entries (have action + symbol + strategy)
                if entry.get("action") and entry.get("symbol") and entry.get("strategy"):
                    trades.append(entry)
    except FileNotFoundError:
        print(f"Warning: {logfile} not found")
    return trades


# ---------------------------------------------------------------------------
# Live position fetching from Alpaca
# ---------------------------------------------------------------------------
def fetch_live_positions():
    """
    Fetch current open positions from Alpaca for unrealized P&L.

    Returns:
        dict: {canonical_symbol_key: position_dict} or empty dict on failure.
    """
    API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
    SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

    if not API_KEY or not SECRET_KEY:
        print("Warning: Alpaca API keys not set -- using local state for unrealized P&L")
        return _fallback_positions_from_state()

    try:
        from alpaca.trading.client import TradingClient
        trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
        positions = trading_client.get_all_positions()

        result = {}
        for pos in positions:
            key = symbol_key(pos.symbol)
            result[key] = {
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price) if pos.avg_entry_price else 0,
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc) if pos.unrealized_plpc else 0,
            }
        return result
    except ImportError:
        print("Warning: alpaca-py not installed -- using local state for unrealized P&L")
        return _fallback_positions_from_state()
    except Exception as e:
        print(f"Warning: Alpaca API error ({e}) -- using local state for unrealized P&L")
        return _fallback_positions_from_state()


def _fallback_positions_from_state():
    """Load position data from local state/positions.json as fallback."""
    data = atomic_read_json(str(PROJECT_ROOT / "state" / "positions.json"))
    if not data:
        return {}
    result = {}
    for pos in data.get("positions", []):
        sym = pos.get("symbol", "")
        key = symbol_key(sym)
        result[key] = {
            "symbol": sym,
            "qty": float(pos.get("qty", 0)),
            "avg_entry_price": float(pos.get("entry_price", 0)),
            "current_price": float(pos.get("current_price", 0)),
            "market_value": float(pos.get("market_value", 0)),
            "unrealized_pl": float(pos.get("unrealized_pnl", 0)),
            "unrealized_plpc": 0,
        }
    return result


# ---------------------------------------------------------------------------
# Round-trip matching: pair BUYs with SELLs per symbol (FIFO)
# ---------------------------------------------------------------------------
def build_round_trips(trades, live_positions):
    """
    Match BUY trades with subsequent SELL trades per symbol (FIFO order).
    Open positions (buys without a matching sell) get unrealized P&L from
    live Alpaca positions.

    Returns:
        list: Round-trip dicts (closed and open).
    """
    # Separate buys and sells, grouped by canonical symbol
    buys_by_symbol = defaultdict(list)
    sells_by_symbol = defaultdict(list)

    for t in trades:
        action = t.get("action", "").upper()
        sym = t.get("symbol", "")
        key = symbol_key(sym)

        if action in BUY_ACTIONS:
            buys_by_symbol[key].append(t)
        elif action in SELL_ACTIONS:
            sells_by_symbol[key].append(t)

    # For BUY_FILL entries, skip the corresponding BUY (pending) entry
    # to avoid double-counting. If a BUY_FILL exists for the same order_id,
    # use the fill and drop the pending BUY.
    for key in buys_by_symbol:
        entries = buys_by_symbol[key]
        fill_order_ids = {
            t.get("order_id") for t in entries
            if t.get("action", "").upper() == "BUY_FILL"
        }
        deduped = []
        for t in entries:
            action = t.get("action", "").upper()
            oid = t.get("order_id")
            # Skip a pending BUY if its fill already exists
            if action == "BUY" and oid in fill_order_ids:
                continue
            deduped.append(t)
        buys_by_symbol[key] = sorted(deduped, key=lambda x: x.get("timestamp", ""))

    for key in sells_by_symbol:
        entries = sells_by_symbol[key]
        fill_order_ids = {
            t.get("order_id") for t in entries
            if t.get("action", "").upper() == "SELL_FILL"
        }
        deduped = []
        for t in entries:
            action = t.get("action", "").upper()
            oid = t.get("order_id")
            if action == "SELL" and oid in fill_order_ids:
                continue
            deduped.append(t)
        sells_by_symbol[key] = sorted(deduped, key=lambda x: x.get("timestamp", ""))

    round_trips = []
    all_symbols = set(buys_by_symbol.keys()) | set(sells_by_symbol.keys())

    # Get current regime for context
    regime_data = atomic_read_json(str(REGIME_FILE))
    current_regime = regime_data.get("current_regime", "UNKNOWN") if regime_data else "UNKNOWN"

    for key in sorted(all_symbols):
        buys = list(buys_by_symbol.get(key, []))
        sells = list(sells_by_symbol.get(key, []))
        sell_idx = 0

        for buy in buys:
            entry_price = _get_price(buy)
            entry_qty = _get_qty(buy)
            entry_ts = buy.get("timestamp", "")
            signal_type = buy.get("signal_type", "unknown")
            signal_strength = buy.get("signal_strength", "")
            strategy = buy.get("strategy", "UNKNOWN")
            display_symbol = buy.get("symbol", key)
            regime_at_entry = buy.get("regime", current_regime)

            if sell_idx < len(sells):
                # Closed round trip
                sell = sells[sell_idx]
                sell_idx += 1
                exit_price = _get_price(sell)
                exit_ts = sell.get("timestamp", "")
                exit_reason = sell.get("signal_type", sell.get("reason", "unknown"))

                pnl = (exit_price - entry_price) * entry_qty if entry_price else 0
                pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
                hold_hours = _hours_between(entry_ts, exit_ts)

                round_trips.append({
                    "symbol": display_symbol,
                    "signal_type": signal_type,
                    "strategy": strategy,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "qty": entry_qty,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "hold_time_hours": round(hold_hours, 1),
                    "entry_timestamp": entry_ts,
                    "exit_timestamp": exit_ts,
                    "exit_reason": exit_reason,
                    "regime_at_entry": regime_at_entry,
                    "signal_strength": signal_strength,
                    "status": "closed",
                })
            else:
                # Open position -- use live data for unrealized P&L
                live = live_positions.get(key, {})
                current_price = live.get("current_price", entry_price)
                unrealized_pl = live.get("unrealized_pl", 0)

                # If we have a live position, compute P&L from that
                if live:
                    pnl = unrealized_pl / max(1, len(buys) - sell_idx)  # spread across open buys
                    pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
                else:
                    pnl = 0
                    pnl_pct = 0

                hold_hours = _hours_between(entry_ts, datetime.now(timezone.utc).isoformat())

                round_trips.append({
                    "symbol": display_symbol,
                    "signal_type": signal_type,
                    "strategy": strategy,
                    "entry_price": entry_price,
                    "exit_price": None,
                    "qty": entry_qty,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "hold_time_hours": round(hold_hours, 1),
                    "entry_timestamp": entry_ts,
                    "exit_timestamp": None,
                    "exit_reason": None,
                    "regime_at_entry": regime_at_entry,
                    "signal_strength": signal_strength,
                    "status": "open",
                })

    return round_trips


def _get_price(trade):
    """Extract the best available price from a trade record."""
    for field in ("fill_price", "price", "limit_price"):
        val = trade.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return 0.0


def _get_qty(trade):
    """Extract quantity from a trade record."""
    for field in ("fill_qty", "qty"):
        val = trade.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return 0.0


def _hours_between(ts1, ts2):
    """Calculate hours between two ISO timestamps. Returns 0 on parse errors."""
    try:
        t1 = datetime.fromisoformat(ts1)
        t2 = datetime.fromisoformat(ts2)
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=timezone.utc)
        if t2.tzinfo is None:
            t2 = t2.replace(tzinfo=timezone.utc)
        return abs((t2 - t1).total_seconds()) / 3600
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Grading logic
# ---------------------------------------------------------------------------
def grade_signal(win_rate, avg_pnl, total_pnl, closed_trades):
    """
    Assign a letter grade to a signal type based on win rate and P&L.

    Grading:
        A: win_rate >= 0.60 and avg_pnl > 0
        B: win_rate >= 0.50 and avg_pnl > 0
        C: win_rate >= 0.40 or (win_rate < 0.40 and avg_pnl > 0)
        D: win_rate >= 0.30 and avg_pnl <= 0
        F: win_rate < 0.30 or (total_pnl < -200 with 5+ trades)

    Returns "INSUFFICIENT_DATA" if fewer than 3 closed trades.
    """
    if closed_trades < 3:
        return "INSUFFICIENT_DATA"

    # F conditions checked first (worst outcomes)
    if total_pnl < -200 and closed_trades >= 5:
        return "F"
    if win_rate < 0.30:
        return "F"

    # A through D
    if win_rate >= 0.60 and avg_pnl > 0:
        return "A"
    if win_rate >= 0.50 and avg_pnl > 0:
        return "B"
    if win_rate >= 0.40 or avg_pnl > 0:
        return "C"
    if win_rate >= 0.30:
        return "D"

    return "F"


# ---------------------------------------------------------------------------
# Scoreboard builders
# ---------------------------------------------------------------------------
def build_signal_scoreboard(round_trips):
    """
    Group round trips by signal_type and compute aggregate metrics.

    Returns:
        dict: {signal_type: score_dict}
    """
    by_signal = defaultdict(list)
    for rt in round_trips:
        by_signal[rt["signal_type"]].append(rt)

    scoreboard = {}
    for sig, trips in sorted(by_signal.items()):
        scoreboard[sig] = _score_group(trips)

    return scoreboard


def build_strategy_scoreboard(round_trips):
    """
    Group round trips by strategy and compute aggregate metrics.

    Returns:
        dict: {strategy: score_dict}
    """
    by_strategy = defaultdict(list)
    for rt in round_trips:
        by_strategy[rt["strategy"]].append(rt)

    scoreboard = {}
    for strat, trips in sorted(by_strategy.items()):
        scoreboard[strat] = _score_group(trips)

    return scoreboard


def _score_group(trips):
    """Compute aggregate metrics for a group of round trips."""
    closed = [t for t in trips if t["status"] == "closed"]
    opens = [t for t in trips if t["status"] == "open"]

    closed_pnls = [t["pnl"] for t in closed]
    wins = sum(1 for p in closed_pnls if p > 0)
    losses = sum(1 for p in closed_pnls if p <= 0)
    closed_count = len(closed)

    win_rate = (wins / closed_count) if closed_count > 0 else 0.0
    avg_pnl = statistics.mean(closed_pnls) if closed_pnls else 0.0
    total_pnl = sum(closed_pnls)

    all_pnls = [t["pnl"] for t in trips]
    avg_pnl_pct_values = [t["pnl_pct"] for t in closed if t["pnl_pct"] is not None]
    avg_pnl_pct = statistics.mean(avg_pnl_pct_values) if avg_pnl_pct_values else 0.0

    hold_hours = [t["hold_time_hours"] for t in closed if t["hold_time_hours"] > 0]
    avg_hold = statistics.mean(hold_hours) if hold_hours else 0.0

    best_pnl = max(closed_pnls) if closed_pnls else 0.0
    worst_pnl = min(closed_pnls) if closed_pnls else 0.0

    open_unrealized = sum(t["pnl"] for t in opens)

    grade = grade_signal(win_rate, avg_pnl, total_pnl, closed_count)

    return {
        "total_trades": len(trips),
        "closed_trades": closed_count,
        "open_trades": len(opens),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "avg_pnl": round(avg_pnl, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 4),
        "total_pnl": round(total_pnl, 2),
        "avg_hold_hours": round(avg_hold, 1),
        "best_trade_pnl": round(best_pnl, 2),
        "worst_trade_pnl": round(worst_pnl, 2),
        "open_unrealized_pnl": round(open_unrealized, 2),
        "grade": grade,
    }


# ---------------------------------------------------------------------------
# Parameter correlation tracking
# ---------------------------------------------------------------------------
def build_param_correlations():
    """
    Read completed experiments from state/experiments.json and build a mapping
    of parameter values to their observed Sharpe ratios and trade counts.

    Returns:
        dict: {param_name: [{"value": ..., "period_sharpe": ..., "trades": ...}, ...]}
    """
    data = atomic_read_json(str(EXPERIMENTS_FILE))
    if not data:
        return {}

    correlations = defaultdict(list)

    # Include the current experiment if it has enough data
    current = data.get("current_experiment")
    if current and current.get("status") == "RUNNING":
        param = current.get("parameter", "unknown")
        correlations[param].append({
            "value": current.get("original_value"),
            "period_sharpe": current.get("baseline_sharpe", 0.0),
            "trades": 0,
            "status": "baseline",
        })
        correlations[param].append({
            "value": current.get("test_value"),
            "period_sharpe": None,  # not yet evaluated
            "trades": 0,
            "status": "testing",
        })

    # Completed experiments
    for exp in data.get("completed_experiments", []):
        param = exp.get("parameter", "unknown")
        correlations[param].append({
            "value": exp.get("original_value"),
            "period_sharpe": exp.get("baseline_sharpe", 0.0),
            "trades": exp.get("baseline_trades", 0),
            "status": "completed_baseline",
        })
        correlations[param].append({
            "value": exp.get("test_value"),
            "period_sharpe": exp.get("test_sharpe", 0.0),
            "trades": exp.get("test_trades", 0),
            "status": "completed_test",
        })

    return dict(correlations)


# ---------------------------------------------------------------------------
# Recommendations engine
# ---------------------------------------------------------------------------
def generate_recommendations(signal_scoreboard, strategy_scoreboard):
    """
    Produce human-readable recommendations based on scoreboards.

    Returns:
        list: Recommendation strings sorted by priority.
    """
    recs = []

    for sig, score in signal_scoreboard.items():
        grade = score["grade"]
        wr = score["win_rate"]
        total = score["total_pnl"]
        closed = score["closed_trades"]
        wr_pct = f"{wr * 100:.0f}%"

        if grade == "F" and closed >= 3:
            recs.append(
                f"DISABLE signal '{sig}' -- win rate {wr_pct}, F grade, "
                f"${total:+,.2f} total PnL over {closed} closed trades"
            )
        elif grade == "D" and closed >= 3:
            recs.append(
                f"REDUCE allocation to '{sig}' -- win rate {wr_pct}, D grade, "
                f"${total:+,.2f} total PnL"
            )
        elif grade == "A" and closed >= 3:
            recs.append(
                f"INCREASE allocation to '{sig}' -- A grade, {wr_pct} win rate, "
                f"${total:+,.2f} total PnL"
            )
        elif grade == "C" and closed >= 3:
            recs.append(
                f"Consider tightening '{sig}' -- C grade, marginal at {wr_pct} win rate"
            )
        elif grade == "INSUFFICIENT_DATA":
            recs.append(
                f"MONITOR '{sig}' -- only {closed} closed trades, need 3+ for grading"
            )

    # Strategy-level recommendations
    for strat, score in strategy_scoreboard.items():
        grade = score["grade"]
        wr = score["win_rate"]
        closed = score["closed_trades"]
        if grade == "F" and closed >= 5:
            recs.append(
                f"REVIEW strategy '{strat}' -- F grade with {closed} closed trades, "
                f"consider pausing"
            )

    # Sort: DISABLE first, then REDUCE, then REVIEW, then INCREASE, then others
    priority = {"DISABLE": 0, "REDUCE": 1, "REVIEW": 2, "Consider": 3, "INCREASE": 4, "MONITOR": 5}

    def sort_key(rec):
        first_word = rec.split()[0]
        return priority.get(first_word, 99)

    recs.sort(key=sort_key)
    return recs


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def print_scoreboard(signal_scoreboard, strategy_scoreboard, recommendations):
    """Print a formatted scoreboard to stdout."""
    print()
    print("=" * 76)
    print("  ATLAS Lite Signal Scoreboard")
    print("=" * 76)
    print()

    # Signal table
    header = f"{'Signal Type':<28} {'Trades':>6} {'Wins':>5} {'WR%':>7} {'Avg PnL':>10} {'Total PnL':>11} {'Grade':>6}"
    print(header)
    print("\u2500" * 76)

    for sig, sc in sorted(signal_scoreboard.items(), key=lambda x: _grade_sort(x[1]["grade"])):
        wr_str = f"{sc['win_rate'] * 100:.1f}%" if sc["closed_trades"] > 0 else "N/A"
        grade_display = sc["grade"]
        avg_pnl_str = f"${sc['avg_pnl']:,.2f}"
        total_pnl_str = f"${sc['total_pnl']:,.2f}"
        print(
            f"{sig:<28} {sc['total_trades']:>6} {sc['wins']:>5} {wr_str:>7} "
            f"{avg_pnl_str:>10} {total_pnl_str:>11} {grade_display:>6}"
        )

    # Open position unrealized
    total_open_unreal = sum(sc["open_unrealized_pnl"] for sc in signal_scoreboard.values())
    if total_open_unreal != 0:
        print()
        print(f"  Open positions unrealized P&L: ${total_open_unreal:,.2f}")

    # Strategy table
    print()
    print("-" * 76)
    print("  Strategy Scoreboard")
    print("-" * 76)
    print()

    header = f"{'Strategy':<28} {'Trades':>6} {'Wins':>5} {'WR%':>7} {'Avg PnL':>10} {'Total PnL':>11} {'Grade':>6}"
    print(header)
    print("\u2500" * 76)

    for strat, sc in sorted(strategy_scoreboard.items(), key=lambda x: _grade_sort(x[1]["grade"])):
        wr_str = f"{sc['win_rate'] * 100:.1f}%" if sc["closed_trades"] > 0 else "N/A"
        grade_display = sc["grade"]
        avg_pnl_str = f"${sc['avg_pnl']:,.2f}"
        total_pnl_str = f"${sc['total_pnl']:,.2f}"
        print(
            f"{strat:<28} {sc['total_trades']:>6} {sc['wins']:>5} {wr_str:>7} "
            f"{avg_pnl_str:>10} {total_pnl_str:>11} {grade_display:>6}"
        )

    # Recommendations
    if recommendations:
        print()
        print("-" * 76)
        print("  Recommendations")
        print("-" * 76)
        for rec in recommendations:
            print(f"  * {rec}")

    print()
    print("=" * 76)
    print()


def _grade_sort(grade):
    """Sort order for grades: A first, F last, INSUFFICIENT_DATA at the end."""
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4, "INSUFFICIENT_DATA": 5}
    return order.get(grade, 99)


# ---------------------------------------------------------------------------
# Public API: load scores from file
# ---------------------------------------------------------------------------
def load_signal_scores(scores_file=None):
    """
    Load the signal scores from the output JSON file.
    Other scripts can import and call this to get the latest scores.

    Returns:
        dict: Signal scores data, or None if the file does not exist.
    """
    if scores_file is None:
        scores_file = str(SCORES_FILE)
    return atomic_read_json(scores_file)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """Run the full performance tracking pipeline."""
    print()
    print("=" * 76)
    print("  ATLAS Lite Performance Tracker")
    print("=" * 76)
    print()

    # 1. Load trade log
    print("[1] Loading trade log ...")
    trades = load_trades()
    buy_count = sum(1 for t in trades if t.get("action", "").upper() in BUY_ACTIONS)
    sell_count = sum(1 for t in trades if t.get("action", "").upper() in SELL_ACTIONS)
    print(f"    Total trade entries: {len(trades)} ({buy_count} buys, {sell_count} sells)")
    print()

    if not trades:
        print("    No trades found. Nothing to analyze.")
        print()
        return 0

    # 2. Fetch live positions for unrealized P&L
    print("[2] Fetching live positions for unrealized P&L ...")
    live_positions = fetch_live_positions()
    print(f"    Live positions found: {len(live_positions)}")
    print()

    # 3. Build round trips
    print("[3] Building round trips (matching buys to sells) ...")
    round_trips = build_round_trips(trades, live_positions)
    closed_count = sum(1 for rt in round_trips if rt["status"] == "closed")
    open_count = sum(1 for rt in round_trips if rt["status"] == "open")
    print(f"    Round trips: {len(round_trips)} ({closed_count} closed, {open_count} open)")
    print()

    # 4. Build scoreboards
    print("[4] Building scoreboards ...")
    signal_scoreboard = build_signal_scoreboard(round_trips)
    strategy_scoreboard = build_strategy_scoreboard(round_trips)
    print(f"    Signal types tracked: {len(signal_scoreboard)}")
    print(f"    Strategies tracked:   {len(strategy_scoreboard)}")
    print()

    # 5. Parameter correlations
    print("[5] Building parameter correlations ...")
    param_correlations = build_param_correlations()
    print(f"    Parameters tracked: {len(param_correlations)}")
    print()

    # 6. Generate recommendations
    print("[6] Generating recommendations ...")
    recommendations = generate_recommendations(signal_scoreboard, strategy_scoreboard)
    print(f"    Recommendations: {len(recommendations)}")
    print()

    # 7. Write output
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "signal_scoreboard": signal_scoreboard,
        "strategy_scoreboard": strategy_scoreboard,
        "param_correlations": param_correlations,
        "recommendations": recommendations,
        "round_trips_summary": {
            "total": len(round_trips),
            "closed": closed_count,
            "open": open_count,
        },
    }

    print("[7] Writing signal scores ...")
    if atomic_write_json(str(SCORES_FILE), output):
        print(f"    Saved: {SCORES_FILE}")
    else:
        print(f"    ERROR: Failed to save {SCORES_FILE}")
        return 1
    print()

    # 8. Print formatted scoreboard
    print_scoreboard(signal_scoreboard, strategy_scoreboard, recommendations)

    return 0


if __name__ == "__main__":
    sys.exit(main())
