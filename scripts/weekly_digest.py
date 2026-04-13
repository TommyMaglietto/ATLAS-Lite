#!/usr/bin/env python3
"""
ATLAS Lite Weekly Performance Digest.
Comprehensive weekly report covering portfolio performance, signal scoreboard,
strategy breakdown, self-improvement status, risk state, and actionable
recommendations.

Designed to run every Sunday at 7:30 PM ET, 30 minutes before the
self-improvement engine evaluates experiments.

Usage:
    python scripts/weekly_digest.py
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ---------------------------------------------------------------------------
# Project paths & imports
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json

STATE_DIR = PROJECT_ROOT / "state"
LOGS_DIR = PROJECT_ROOT / "logs"
DIGESTS_DIR = LOGS_DIR / "weekly_digests"
TRADES_LOG = LOGS_DIR / "trades.jsonl"

POSITIONS_FILE = STATE_DIR / "positions.json"
EXPERIMENTS_FILE = STATE_DIR / "experiments.json"
REGIME_FILE = STATE_DIR / "regime.json"
RISK_STATE_FILE = STATE_DIR / "risk_state.json"
SIGNAL_SCORES_FILE = STATE_DIR / "signal_scores.json"
STRATEGY_PARAMS_FILE = PROJECT_ROOT / "config" / "strategy_params.json"


# ---------------------------------------------------------------------------
# 1. Trade aggregation
# ---------------------------------------------------------------------------

def load_all_trades():
    """Load every line from logs/trades.jsonl."""
    trades = []
    if not TRADES_LOG.exists():
        return trades
    try:
        with open(TRADES_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"  Warning: Could not read trades log: {e}")
    return trades


def filter_trades_last_n_days(trades, days=7):
    """Return trades from the last *days* calendar days."""
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    filtered = []
    for t in trades:
        ts_str = t.get("timestamp", "2000-01-01")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                filtered.append(t)
        except (ValueError, TypeError):
            continue
    return filtered


def is_actual_trade(entry):
    """Return True if the log entry represents a real buy or sell (not a meta-event)."""
    action = entry.get("action", entry.get("event", "")).upper()
    return action in (
        "BUY", "SELL", "BUY_FILL", "SELL_FILL",
        "BOUGHT", "SOLD",
    )


def aggregate_trades(trades):
    """Compute headline trade statistics for the week."""
    actual = [t for t in trades if is_actual_trade(t)]
    buys = [t for t in actual if t.get("action", "").upper() in ("BUY", "BUY_FILL", "BOUGHT")]
    sells = [t for t in actual if t.get("action", "").upper() in ("SELL", "SELL_FILL", "SOLD")]

    realized_pnl = sum(float(t.get("pnl", 0)) for t in actual)

    # Identify best / worst trade by PnL
    trades_with_pnl = [t for t in actual if float(t.get("pnl", 0)) != 0]
    best_trade = max(trades_with_pnl, key=lambda t: float(t.get("pnl", 0))) if trades_with_pnl else None
    worst_trade = min(trades_with_pnl, key=lambda t: float(t.get("pnl", 0))) if trades_with_pnl else None

    # Win rate from closed (sell) trades that have a PnL
    closed = [t for t in actual if float(t.get("pnl", 0)) != 0]
    winners = [t for t in closed if float(t.get("pnl", 0)) > 0]
    win_rate = len(winners) / len(closed) if closed else None

    return {
        "total_trades": len(actual),
        "buys": len(buys),
        "sells": len(sells),
        "realized_pnl": round(realized_pnl, 2),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "best_trade": _trade_summary(best_trade) if best_trade else None,
        "worst_trade": _trade_summary(worst_trade) if worst_trade else None,
    }


def _trade_summary(t):
    """Compact dict for a single noteworthy trade."""
    return {
        "symbol": t.get("symbol", "???"),
        "action": t.get("action", ""),
        "pnl": round(float(t.get("pnl", 0)), 2),
        "strategy": t.get("strategy", ""),
        "timestamp": t.get("timestamp", ""),
    }


# ---------------------------------------------------------------------------
# 2. Alpaca portfolio fetch
# ---------------------------------------------------------------------------

def fetch_alpaca_data():
    """
    Fetch live account + positions from Alpaca.
    Returns (account_dict, positions_list) or (None, None) on failure.
    """
    API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
    SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

    if not API_KEY or not SECRET_KEY:
        print("  Warning: Alpaca API keys not set. Using local state fallback.")
        return None, None

    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(API_KEY, SECRET_KEY, paper=True)

        account = client.get_account()
        positions = client.get_all_positions()

        acct = {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value) if account.portfolio_value else 0,
            "status": str(account.status),
        }

        pos_list = []
        for p in positions:
            pos_list.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price) if p.avg_entry_price else 0,
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "asset_class": str(p.asset_class),
            })

        return acct, pos_list

    except ImportError:
        print("  Warning: alpaca-py not installed. Using local state fallback.")
        return None, None
    except Exception as e:
        print(f"  Warning: Alpaca API error: {e}. Using local state fallback.")
        return None, None


def portfolio_from_local_state():
    """Build account/position dicts from local state files when Alpaca is unavailable."""
    pos_data = atomic_read_json(str(POSITIONS_FILE)) or {}
    totals = pos_data.get("totals", {})

    acct = {
        "equity": float(totals.get("total_value", 0)) + float(totals.get("cash_available", 0)),
        "cash": float(totals.get("cash_available", 0)),
        "buying_power": float(totals.get("cash_available", 0)),
        "portfolio_value": float(totals.get("total_value", 0)) + float(totals.get("cash_available", 0)),
        "status": "LOCAL_FALLBACK",
    }

    pos_list = []
    for p in pos_data.get("positions", []):
        pos_list.append({
            "symbol": p.get("symbol", ""),
            "qty": float(p.get("qty", 0)),
            "avg_entry_price": float(p.get("entry_price", 0)),
            "current_price": float(p.get("current_price", 0)),
            "market_value": float(p.get("market_value", 0)),
            "unrealized_pl": float(p.get("unrealized_pnl", 0)),
            "asset_class": p.get("asset_class", ""),
        })

    return acct, pos_list


# ---------------------------------------------------------------------------
# 3. Signal scoreboard
# ---------------------------------------------------------------------------

def load_signal_scores():
    """Read state/signal_scores.json if it exists."""
    if not SIGNAL_SCORES_FILE.exists():
        return None
    return atomic_read_json(str(SIGNAL_SCORES_FILE))


def signal_scoreboard(signal_scores):
    """
    Return top-3 and bottom-3 signals sorted by grade/win_rate.
    signal_scores is the full signal_scores.json dict -- drill into
    'signal_scoreboard' key to get per-signal data.
    """
    if not signal_scores:
        return None

    # The raw file has top-level keys: signal_scoreboard, strategy_scoreboard, etc.
    # We need the signal_scoreboard sub-dict.
    scoreboard_data = signal_scores.get("signal_scoreboard", signal_scores)

    grade_order = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}

    items = []
    for name, score in scoreboard_data.items():
        if not isinstance(score, dict):
            continue
        items.append({
            "name": name,
            "grade": score.get("grade", "?"),
            "win_rate": score.get("win_rate", 0),
            "avg_pnl": score.get("avg_pnl", 0),
            "total_pnl": score.get("total_pnl", 0),
            "total_trades": score.get("total_trades", 0),
        })

    # Sort: best first (lowest grade_order, then highest win_rate)
    items.sort(key=lambda x: (grade_order.get(x["grade"], 5), -x["win_rate"]))

    top3 = items[:3]
    bottom3 = items[-3:] if len(items) > 3 else items
    bottom3 = list(reversed(bottom3))  # worst first

    return {"top": top3, "bottom": bottom3, "all": items}


# ---------------------------------------------------------------------------
# 4. Strategy performance
# ---------------------------------------------------------------------------

def strategy_performance(trades):
    """Per-strategy breakdown: trades, PnL, Sharpe, win rate."""
    by_strategy = defaultdict(list)
    for t in trades:
        if not is_actual_trade(t):
            continue
        strat = t.get("strategy", "UNKNOWN")
        by_strategy[strat].append(t)

    results = {}
    for strat, strat_trades in sorted(by_strategy.items()):
        pnls = [float(t.get("pnl", 0)) for t in strat_trades]
        total_pnl = sum(pnls)
        winners = sum(1 for p in pnls if p > 0)
        trades_with_pnl = [p for p in pnls if p != 0]
        win_rate = winners / len(trades_with_pnl) if trades_with_pnl else None

        # Daily Sharpe
        daily_pnls = defaultdict(float)
        for t in strat_trades:
            date = t.get("timestamp", "")[:10]
            daily_pnls[date] += float(t.get("pnl", 0))
        daily_vals = list(daily_pnls.values())

        if len(daily_vals) >= 2:
            import statistics
            mean_r = statistics.mean(daily_vals)
            std_r = statistics.stdev(daily_vals)
            sharpe = round((mean_r / std_r) * (252 ** 0.5), 2) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        results[strat] = {
            "trades": len(strat_trades),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "sharpe": sharpe,
        }

    return results


# ---------------------------------------------------------------------------
# 5. Self-improvement summary
# ---------------------------------------------------------------------------

def self_improvement_summary():
    """Read experiments.json and summarise."""
    data = atomic_read_json(str(EXPERIMENTS_FILE))
    if not data:
        return {
            "available": False,
            "engine_status": "UNKNOWN",
            "current_experiment": None,
            "statistics": {"total_experiments": 0, "accepted": 0, "rejected": 0, "acceptance_rate": 0},
            "last_completed": None,
        }

    current = data.get("current_experiment")
    stats = data.get("statistics", {})
    completed = data.get("completed_experiments", [])
    last_completed = completed[-1] if completed else None

    return {
        "available": True,
        "engine_status": data.get("engine_status", "UNKNOWN"),
        "engine_status_reason": data.get("engine_status_reason", ""),
        "current_experiment": current,
        "statistics": stats,
        "last_completed": last_completed,
    }


# ---------------------------------------------------------------------------
# 6. Risk status
# ---------------------------------------------------------------------------

def risk_status():
    """Read risk_state.json for drawdown / emergency info."""
    data = atomic_read_json(str(RISK_STATE_FILE))
    if not data:
        return {
            "available": False,
            "current_drawdown_pct": 0,
            "emergency_mode": False,
            "circuit_breaker_hit": False,
            "peak_equity": None,
        }

    return {
        "available": True,
        "peak_equity": data.get("peak_equity"),
        "peak_date": data.get("peak_date"),
        "current_drawdown_pct": data.get("current_drawdown_pct", 0),
        "emergency_mode": data.get("emergency_mode", False),
        "emergency_triggered_at": data.get("emergency_triggered_at"),
        "circuit_breaker_hit": data.get("circuit_breaker_hit", False),
        "circuit_breaker_at": data.get("circuit_breaker_at"),
    }


# ---------------------------------------------------------------------------
# 7. Recommendations engine
# ---------------------------------------------------------------------------

def generate_recommendations(
    trade_agg, signal_scores, strat_perf, risk, regime_data, acct, positions
):
    """Produce a list of actionable recommendation strings."""
    recommendations = []

    # --- Signal quality ---
    # Drill into the signal_scoreboard sub-dict (raw file has top-level wrapper)
    sig_data = {}
    if signal_scores and isinstance(signal_scores, dict):
        sig_data = signal_scores.get("signal_scoreboard", signal_scores)
    if sig_data and isinstance(sig_data, dict):
        for sig_name, score in sig_data.items():
            if not isinstance(score, dict):
                continue
            grade = score.get("grade", "")
            total_trades = score.get("total_trades", 0)
            win_rate = score.get("win_rate", 0)
            total_pnl = score.get("total_pnl", 0)
            avg_pnl = score.get("avg_pnl", 0)

            if grade == "F" and total_trades >= 5:
                recommendations.append(
                    f"DISABLE signal '{sig_name}' -- F grade, "
                    f"{win_rate:.0%} win rate, ${total_pnl:+.2f} total PnL"
                )
            elif grade == "A" and total_trades >= 3:
                recommendations.append(
                    f"INCREASE allocation to '{sig_name}' -- A grade, "
                    f"${avg_pnl:+.2f} avg PnL"
                )

    # --- Strategy-level ---
    for strat, metrics in (strat_perf or {}).items():
        pnl = metrics.get("total_pnl", 0)
        if pnl < -500:
            recommendations.append(
                f"REVIEW {strat} -- losing ${abs(pnl):.2f} this week"
            )

    # --- Regime ---
    current_regime = (regime_data or {}).get("current_regime", "")
    if current_regime == "RISK_OFF":
        recommendations.append(
            "RISK_OFF regime active -- all entries paused, monitor for recovery"
        )
    elif current_regime == "CAUTIOUS":
        recommendations.append(
            "CAUTIOUS regime -- position sizes halved, watch for deterioration"
        )

    # --- Cash reserve ---
    equity = acct.get("equity", 0) if acct else 0
    cash = acct.get("cash", 0) if acct else 0
    if equity > 0:
        cash_pct = (cash / equity) * 100
        if cash_pct < 25:
            recommendations.append(
                f"Cash reserve low ({cash_pct:.1f}%) -- approaching 20% minimum"
            )

    # --- Drawdown ---
    dd = risk.get("current_drawdown_pct", 0) if risk else 0
    if dd < -5:
        recommendations.append(
            f"Drawdown at {dd:.1f}% -- consider reducing exposure"
        )
    if risk and risk.get("emergency_mode"):
        recommendations.append(
            "EMERGENCY MODE active -- all trading halted until manual review"
        )

    # --- Largest unrealized loss ---
    if positions:
        worst_pos = min(positions, key=lambda p: float(p.get("unrealized_pl", 0)))
        worst_upl = float(worst_pos.get("unrealized_pl", 0))
        if worst_upl < -100:
            recommendations.append(
                f"Monitor {worst_pos['symbol']} position -- "
                f"largest unrealized loss ${worst_upl:+,.2f}"
            )

    # --- No sells yet ---
    if trade_agg.get("sells", 0) == 0 and trade_agg.get("buys", 0) > 0:
        recommendations.append(
            "No sells this week -- need closed trades for meaningful Sharpe calculation"
        )

    return recommendations


# ---------------------------------------------------------------------------
# 8. Week-over-week equity change
# ---------------------------------------------------------------------------

def week_over_week_change(current_equity):
    """
    Compute equity change vs. last week's digest or risk_state peak.
    Returns (delta_dollars, delta_pct) or (None, None).
    """
    # Try last week's digest first
    if DIGESTS_DIR.exists():
        try:
            digest_files = sorted(DIGESTS_DIR.glob("*.json"), reverse=True)
            for df in digest_files:
                prev = atomic_read_json(str(df))
                if prev and prev.get("account", {}).get("equity"):
                    prev_equity = float(prev["account"]["equity"])
                    delta = current_equity - prev_equity
                    pct = (delta / prev_equity) * 100 if prev_equity else 0
                    return round(delta, 2), round(pct, 2)
        except Exception:
            pass

    # Fall back to risk_state peak
    risk = atomic_read_json(str(RISK_STATE_FILE))
    if risk and risk.get("peak_equity"):
        peak = float(risk["peak_equity"])
        delta = current_equity - peak
        pct = (delta / peak) * 100 if peak else 0
        return round(delta, 2), round(pct, 2)

    return None, None


# ---------------------------------------------------------------------------
# Formatted output
# ---------------------------------------------------------------------------

def format_report(digest):
    """Build a human-readable text report from the digest dict."""
    week_of = digest["week_of"]
    acct = digest["account"]
    ta = digest["trade_aggregates"]
    scoreboard = digest.get("signal_scoreboard")
    strat = digest.get("strategy_performance", {})
    si = digest.get("self_improvement", {})
    risk = digest.get("risk_status", {})
    recs = digest.get("recommendations", [])
    wow = digest.get("week_over_week", {})
    positions = digest.get("positions", [])

    lines = []

    # Header
    title = f"ATLAS Lite Weekly Digest -- Week of {week_of}"
    pad = max(0, 50 - len(title)) // 2
    lines.append("")
    lines.append("+" + "=" * 52 + "+")
    lines.append("|" + " " * pad + title + " " * (52 - pad - len(title)) + "|")
    lines.append("+" + "=" * 52 + "+")
    lines.append("")

    # Portfolio
    equity = acct.get("equity", 0)
    cash = acct.get("cash", 0)
    cash_pct = (cash / equity * 100) if equity else 0
    wow_delta = wow.get("delta_dollars")
    wow_pct = wow.get("delta_pct")
    dd = risk.get("current_drawdown_pct", 0)

    lines.append("  PORTFOLIO")
    lines.append(f"    Equity:      ${equity:,.2f}")
    lines.append(f"    Cash:        ${cash:,.2f} ({cash_pct:.1f}%)")
    if wow_delta is not None:
        lines.append(f"    Week P&L:    ${wow_delta:+,.2f} ({wow_pct:+.2f}%)")
    lines.append(f"    Drawdown:    {dd:.2f}% from peak")
    lines.append("")

    # Trades
    lines.append("  TRADES THIS WEEK")
    lines.append(
        f"    Buys: {ta['buys']}  |  Sells: {ta['sells']}  |  "
        f"Realized P&L: ${ta['realized_pnl']:,.2f}"
    )

    total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    lines.append(f"    Unrealized:  ${total_unrealized:+,.2f}")

    best = ta.get("best_trade")
    worst = ta.get("worst_trade")
    best_str = f"{best['symbol']} ${best['pnl']:+.2f}" if best else "N/A"
    worst_str = f"{worst['symbol']} ${worst['pnl']:+.2f}" if worst else "N/A"
    lines.append(f"    Best:  {best_str}  |  Worst: {worst_str}")

    if ta.get("win_rate") is not None:
        lines.append(f"    Win Rate: {ta['win_rate']:.0%}")
    lines.append("")

    # Signal scoreboard
    lines.append("  SIGNAL SCOREBOARD")
    if scoreboard:
        for s in scoreboard.get("top", []):
            wr = f"{s['win_rate']:.0%}" if isinstance(s["win_rate"], float) else str(s["win_rate"])
            lines.append(
                f"    [+] {s['name']:<28s} -- {s['grade']} grade, "
                f"{wr} WR, ${s['avg_pnl']:+.2f} avg"
            )
        for s in scoreboard.get("bottom", []):
            wr = f"{s['win_rate']:.0%}" if isinstance(s["win_rate"], float) else str(s["win_rate"])
            lines.append(
                f"    [-] {s['name']:<28s} -- {s['grade']} grade, "
                f"{wr} WR, ${s['avg_pnl']:+.2f} avg"
            )
    else:
        lines.append("    Signal scores not available yet (run performance_tracker.py first)")
    lines.append("")

    # Strategy performance
    lines.append("  STRATEGY PERFORMANCE")
    if strat:
        lines.append(f"    {'Strategy':<22s} {'Trades':>6s} {'PnL':>10s} {'WR':>7s} {'Sharpe':>7s}")
        lines.append(f"    {'-'*22} {'-'*6} {'-'*10} {'-'*7} {'-'*7}")
        for name, m in sorted(strat.items()):
            wr_str = f"{m['win_rate']:.0%}" if m.get("win_rate") is not None else "N/A"
            lines.append(
                f"    {name:<22s} {m['trades']:>6d} "
                f"${m['total_pnl']:>9.2f} {wr_str:>7s} {m['sharpe']:>7.2f}"
            )
    else:
        lines.append("    No strategy trades this week.")
    lines.append("")

    # Self-improvement
    lines.append("  SELF-IMPROVEMENT")
    if si.get("available"):
        cur = si.get("current_experiment")
        if cur:
            param = cur.get("parameter", "?")
            orig = cur.get("original_value", "?")
            test = cur.get("test_value", "?")
            eval_until = cur.get("eval_until", "?")
            if isinstance(eval_until, str) and len(eval_until) >= 10:
                eval_date = eval_until[:10]
                try:
                    eval_dt = datetime.fromisoformat(eval_until.replace("Z", "+00:00"))
                    days_left = (eval_dt - datetime.now(timezone.utc)).days
                    eval_date = f"{eval_until[:10]} ({days_left}d left)"
                except (ValueError, TypeError):
                    pass
            else:
                eval_date = str(eval_until)
            lines.append(f"    Current: {param} {orig} -> {test} (eval {eval_date})")
        else:
            lines.append(f"    Current: none ({si.get('engine_status', '?')})")
        stats = si.get("statistics", {})
        lines.append(
            f"    Total: {stats.get('total_experiments', 0)} | "
            f"Accepted: {stats.get('accepted', 0)} | "
            f"Rejected: {stats.get('rejected', 0)}"
        )
        last = si.get("last_completed")
        if last:
            lines.append(
                f"    Last result: {last.get('parameter', '?')} -- "
                f"{'ACCEPTED' if last.get('accepted') else 'REJECTED'}"
            )
    else:
        lines.append("    Experiments file not available.")
    lines.append("")

    # Risk
    lines.append("  RISK STATUS")
    if risk.get("available"):
        lines.append(f"    Drawdown from peak:  {risk.get('current_drawdown_pct', 0):.2f}%")
        lines.append(f"    Emergency mode:      {'YES' if risk.get('emergency_mode') else 'No'}")
        lines.append(f"    Circuit breaker:     {'HIT' if risk.get('circuit_breaker_hit') else 'Clear'}")
    else:
        lines.append("    Risk state file not available.")
    lines.append("")

    # Regime
    regime = digest.get("regime", {})
    lines.append("  REGIME")
    lines.append(f"    Current: {regime.get('current_regime', 'UNKNOWN')} "
                 f"(composite {regime.get('composite_score', 0):.2f})")
    lines.append("")

    # Recommendations
    lines.append("  RECOMMENDATIONS")
    if recs:
        for i, r in enumerate(recs, 1):
            lines.append(f"    {i}. {r}")
    else:
        lines.append("    No actionable recommendations this week.")
    lines.append("")

    lines.append("+" + "=" * 52 + "+")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 64)
    print("  ATLAS Lite Weekly Digest Generator")
    print("=" * 64)
    print()

    now = datetime.now(timezone.utc)
    week_of = now.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # 1. Load trades and filter to last 7 days
    # ------------------------------------------------------------------
    print("[1] Loading trades (last 7 days) ...")
    all_trades = load_all_trades()
    weekly_trades = filter_trades_last_n_days(all_trades, days=7)
    trade_agg = aggregate_trades(weekly_trades)
    print(f"    Total log entries this week: {len(weekly_trades)}")
    print(f"    Actual trades (buy/sell):    {trade_agg['total_trades']}")
    print()

    # ------------------------------------------------------------------
    # 2. Fetch live portfolio from Alpaca (with local fallback)
    # ------------------------------------------------------------------
    print("[2] Fetching portfolio state ...")
    acct, positions = fetch_alpaca_data()
    if acct is None:
        acct, positions = portfolio_from_local_state()
        print("    (using local state fallback)")
    print(f"    Equity:    ${acct.get('equity', 0):,.2f}")
    print(f"    Cash:      ${acct.get('cash', 0):,.2f}")
    print(f"    Positions: {len(positions)}")
    print()

    # ------------------------------------------------------------------
    # 3. Week-over-week equity change
    # ------------------------------------------------------------------
    print("[3] Computing week-over-week change ...")
    wow_delta, wow_pct = week_over_week_change(acct.get("equity", 0))
    if wow_delta is not None:
        print(f"    Delta: ${wow_delta:+,.2f} ({wow_pct:+.2f}%)")
    else:
        print("    No previous digest to compare against.")
    print()

    # ------------------------------------------------------------------
    # 4. Signal scoreboard
    # ------------------------------------------------------------------
    print("[4] Loading signal scores ...")
    raw_signal_scores = load_signal_scores()
    scoreboard = signal_scoreboard(raw_signal_scores)
    if scoreboard:
        print(f"    Signals tracked: {len(scoreboard['all'])}")
    else:
        print("    signal_scores.json not found (will be created by performance_tracker.py)")
    print()

    # ------------------------------------------------------------------
    # 5. Strategy performance
    # ------------------------------------------------------------------
    print("[5] Computing strategy performance ...")
    strat_perf = strategy_performance(weekly_trades)
    for name, m in sorted(strat_perf.items()):
        print(f"    {name}: {m['trades']} trades, ${m['total_pnl']:+.2f}")
    print()

    # ------------------------------------------------------------------
    # 6. Self-improvement summary
    # ------------------------------------------------------------------
    print("[6] Reading self-improvement state ...")
    si = self_improvement_summary()
    print(f"    Engine: {si.get('engine_status', 'UNKNOWN')}")
    print()

    # ------------------------------------------------------------------
    # 7. Risk status
    # ------------------------------------------------------------------
    print("[7] Reading risk state ...")
    risk = risk_status()
    print(f"    Drawdown: {risk.get('current_drawdown_pct', 0):.2f}%")
    print(f"    Emergency: {'YES' if risk.get('emergency_mode') else 'No'}")
    print()

    # ------------------------------------------------------------------
    # 8. Regime
    # ------------------------------------------------------------------
    regime_data = atomic_read_json(str(REGIME_FILE)) or {}

    # ------------------------------------------------------------------
    # 9. Generate recommendations
    # ------------------------------------------------------------------
    print("[8] Generating recommendations ...")
    recs = generate_recommendations(
        trade_agg, raw_signal_scores, strat_perf, risk, regime_data, acct, positions
    )
    for r in recs:
        print(f"    -> {r}")
    print()

    # ------------------------------------------------------------------
    # 10. Assemble digest object
    # ------------------------------------------------------------------
    digest = {
        "week_of": week_of,
        "generated_at": now.isoformat(),
        "account": acct,
        "positions": positions,
        "week_over_week": {
            "delta_dollars": wow_delta,
            "delta_pct": wow_pct,
        },
        "trade_aggregates": trade_agg,
        "signal_scoreboard": scoreboard,
        "strategy_performance": strat_perf,
        "self_improvement": si,
        "risk_status": risk,
        "regime": {
            "current_regime": regime_data.get("current_regime", "UNKNOWN"),
            "composite_score": regime_data.get("composite_score", 0),
            "last_updated": regime_data.get("last_updated", ""),
        },
        "recommendations": recs,
    }

    # ------------------------------------------------------------------
    # 11. Save JSON
    # ------------------------------------------------------------------
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    digest_file = DIGESTS_DIR / f"{week_of}.json"

    print("[9] Saving digest ...")
    if atomic_write_json(str(digest_file), digest):
        print(f"    Saved to {digest_file}")
    else:
        print(f"    ERROR: Failed to save {digest_file}")
        return 1
    print()

    # ------------------------------------------------------------------
    # 12. Print formatted report
    # ------------------------------------------------------------------
    report = format_report(digest)
    print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
