#!/usr/bin/env python3
"""
ATLAS Lite Self-Improvement Engine v2 --Accelerated Karpathy Loop

Major changes from v1:
  - Parallel experiments: one per config section (up to 4 simultaneous)
  - Composite scoring: win_rate -> profit_factor -> Sharpe based on sample size
  - Trade-count triggers: evaluate when enough closed trades exist, not calendar
  - Active-only targeting: only experiment on strategies with recent trade data
  - 3-day default eval with 7-day max, not weekly

Runs every 3 days (or on demand). Phases per experiment slot:
  1. Pre-checks (enough data per strategy, cooldown per slot)
  2. Baseline measurement (composite score per strategy)
  3. Experiment design (mutate one param)
  4. Deploy (update strategy_params.json, mark RUNNING)
  5. Evaluate (trade-count or time trigger, accept/reject)
"""

import json
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from atomic_write import atomic_write_json, atomic_read_json
from metrics import load_trades, calculate_metrics_by_strategy

STATE_DIR = PROJECT_ROOT / "state"
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
SELF_IMPROVEMENT_DIR = PROJECT_ROOT / "self-improvement"

EXPERIMENTS_FILE = STATE_DIR / "experiments.json"
STRATEGY_PARAMS_FILE = CONFIG_DIR / "strategy_params.json"
METRICS_HISTORY_FILE = SELF_IMPROVEMENT_DIR / "metrics_history.json"
TRADES_FILE = LOGS_DIR / "trades.jsonl"

# ---------------------------------------------------------------------------
# Strategy -> config section mapping + tunable params
# ---------------------------------------------------------------------------
STRATEGY_PARAM_MAP = {
    "TRAILING_STOP": {
        "section": "trailing_stop",
        "params": ["loss_pct", "trail_pct", "full_close_after_tiers", "trail_decay_per_day"],
    },
    "WHEEL": {
        "section": "wheel",
        "params": ["put_delta", "call_delta", "dte_target"],
    },
    "POLITICIAN_COPY": {
        "section": "politician_copy",
        "params": ["max_position_pct", "staleness_threshold_days"],
    },
}

# Per-signal param map for crypto (v2 config schema)
# Each signal type maps to its tunable params inside crypto_strategy.signal.<key>
SIGNAL_PARAM_MAP = {
    "mean_reversion_oversold": {
        "section": "crypto_strategy",
        "signal_key": "mean_reversion_oversold",
        "params": ["rsi_oversold", "bb_std"],
    },
    "ema_crossover_bullish": {
        "section": "crypto_strategy",
        "signal_key": "ema_crossover_bullish",
        "params": ["ema_fast", "ema_med", "ema_long", "vwap_slope_chop_threshold"],
    },
    "dca_technical_trigger": {
        "section": "crypto_strategy",
        "signal_key": "dca_technical_trigger",
        "params": ["dca_rsi_threshold", "dca_aggressive_rsi", "rsi_oversold"],
    },
    "overbought_exit": {
        "section": "crypto_strategy",
        "signal_key": "overbought_exit",
        "params": ["rsi_overbought"],
    },
    "volume_engulfing_bullish": {
        "section": "crypto_strategy",
        "signal_key": "volume_engulfing_bullish",
        "params": ["engulf_vol_multiplier"],
    },
    "body_momentum_bullish": {
        "section": "crypto_strategy",
        "signal_key": "body_momentum_bullish",
        "params": ["body_momentum_period"],
    },
}

# Inject _weight as a tunable param for every signal type (Phase 4: Adaptive Weights)
for _sig_type in SIGNAL_PARAM_MAP:
    if "_weight" not in SIGNAL_PARAM_MAP[_sig_type]["params"]:
        SIGNAL_PARAM_MAP[_sig_type]["params"].append("_weight")

# Config sections that can run parallel experiments (one experiment per section)
# Non-crypto strategies get one slot each; crypto signals each get their own slot.
EXPERIMENT_SLOTS = {
    "trailing_stop": ["TRAILING_STOP"],
    "wheel": ["WHEEL"],
    "politician_copy": ["POLITICIAN_COPY"],
    # Per-signal slots for crypto (6 parallel experiments possible)
    "crypto.mean_reversion_oversold": ["mean_reversion_oversold"],
    "crypto.ema_crossover_bullish": ["ema_crossover_bullish"],
    "crypto.dca_technical_trigger": ["dca_technical_trigger"],
    "crypto.volume_engulfing_bullish": ["volume_engulfing_bullish"],
    "crypto.body_momentum_bullish": ["body_momentum_bullish"],
    "crypto.overbought_exit": ["overbought_exit"],
    # Per-signal slots for 15-minute crypto (6 parallel fast-cycling experiments)
    "crypto.15m.mean_reversion_oversold": ["mean_reversion_oversold_15m"],
    "crypto.15m.ema_crossover_bullish": ["ema_crossover_bullish_15m"],
    "crypto.15m.dca_technical_trigger": ["dca_technical_trigger_15m"],
    "crypto.15m.volume_engulfing_bullish": ["volume_engulfing_bullish_15m"],
    "crypto.15m.body_momentum_bullish": ["body_momentum_bullish_15m"],
    "crypto.15m.overbought_exit": ["overbought_exit_15m"],
}

# 15-minute signal param map (points to crypto_strategy_15m config section)
SIGNAL_PARAM_MAP_15M = {
    "mean_reversion_oversold_15m": {
        "section": "crypto_strategy_15m",
        "signal_key": "mean_reversion_oversold",
        "params": ["rsi_oversold", "bb_std"],
    },
    "ema_crossover_bullish_15m": {
        "section": "crypto_strategy_15m",
        "signal_key": "ema_crossover_bullish",
        "params": ["ema_fast", "ema_med", "ema_long", "vwap_slope_chop_threshold"],
    },
    "dca_technical_trigger_15m": {
        "section": "crypto_strategy_15m",
        "signal_key": "dca_technical_trigger",
        "params": ["dca_rsi_threshold", "dca_aggressive_rsi", "rsi_oversold"],
    },
    "volume_engulfing_bullish_15m": {
        "section": "crypto_strategy_15m",
        "signal_key": "volume_engulfing_bullish",
        "params": ["engulf_vol_multiplier"],
    },
    "body_momentum_bullish_15m": {
        "section": "crypto_strategy_15m",
        "signal_key": "body_momentum_bullish",
        "params": ["body_momentum_period"],
    },
    "overbought_exit_15m": {
        "section": "crypto_strategy_15m",
        "signal_key": "overbought_exit",
        "params": ["rsi_overbought"],
    },
}

# Merge 15m signal params into main map
SIGNAL_PARAM_MAP.update(SIGNAL_PARAM_MAP_15M)

# Inject _weight param into 15m signals too
for sig_type in SIGNAL_PARAM_MAP_15M:
    if "_weight" not in SIGNAL_PARAM_MAP[sig_type]["params"]:
        SIGNAL_PARAM_MAP[sig_type]["params"].append("_weight")

# Fallback bounds
DEFAULT_BOUNDS = {
    "loss_pct": (3.0, 15.0),
    "trail_pct": (2.0, 10.0),
    "full_close_after_tiers": (2, 5),
    "trail_decay_per_day": (0.1, 2.0),
    "ml_confidence_threshold": (0.0, 0.80),
    "put_delta": (0.20, 0.35),
    "call_delta": (0.20, 0.35),
    "dte_target": (14, 45),
    "max_position_pct": (2.0, 10.0),
    "staleness_threshold_days": (20, 60),
    "dca_rsi_threshold": (30, 50),
    "rsi_oversold": (25, 40),
    "rsi_overbought": (65, 80),
    "bb_std": (1.5, 3.0),
    "ema_fast": (5, 15),
    "ema_med": (15, 30),
    "ema_long": (40, 80),
    # Video strategy feature bounds
    "vwap_period": (12, 48),
    "vwap_slope_period": (3, 10),
    "vwap_slope_chop_threshold": (0.01, 0.15),
    "adr_exhaustion_pct": (70, 95),
    "engulf_vol_multiplier": (1.2, 2.5),
    "body_momentum_period": (3, 10),
    "ema_trail_buffer_pct": (0.1, 2.0),
    "_weight": (0.0, 2.0),
}

# Evaluation thresholds --adapt to sample size
MIN_TRADES_TO_EVALUATE = 3       # Absolute minimum closed trades to score (lowered from 5 for faster learning)
MIN_TRADES_FOR_EXPERIMENT = 2    # Minimum recent trades to consider a strategy active (lowered from 3)

# Default eval timing (used for non-crypto slots)
EVAL_TRADE_TARGET = 8
EVAL_MAX_DAYS = 7
EVAL_MIN_DAYS = 2
COOLDOWN_HOURS_PER_SLOT = 12


def get_eval_params(section):
    """Return eval timing params, faster for crypto, ultra-fast for 15-min."""
    if section.startswith("crypto.15m."):
        return {"min_days": 0.5, "max_days": 2, "trade_target": 3, "cooldown_hours": 3}
    if section.startswith("crypto."):
        return {"min_days": 2, "max_days": 5, "trade_target": 5, "cooldown_hours": 6}
    return {"min_days": EVAL_MIN_DAYS, "max_days": EVAL_MAX_DAYS,
            "trade_target": EVAL_TRADE_TARGET, "cooldown_hours": COOLDOWN_HOURS_PER_SLOT}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)

def iso_now():
    return now_utc().isoformat()

def log(msg):
    print(f"[SELF_IMPROVEMENT] {msg}")

def git_commit(message):
    subprocess.run(
        ["git", "add", "config/", "state/", "self-improvement/", "logs/"],
        cwd=str(PROJECT_ROOT), capture_output=True,
    )
    result = subprocess.run(
        ["git", "commit", "-m", f"[SELF_IMPROVEMENT] {message}"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )
    if result.returncode == 0:
        log(f"Git commit: {message}")
    else:
        log(f"Git commit skipped: {result.stderr.strip()}")

def load_experiments():
    data = atomic_read_json(str(EXPERIMENTS_FILE))
    if data is None:
        return _empty_experiments()
    # Migrate v1 -> v2 if needed
    if data.get("schema_version") != "2.0.0":
        return _migrate_v1_to_v2(data)
    return data

def _empty_experiments():
    return {
        "schema_version": "2.0.0",
        "active_experiments": [],
        "engine_status": "IDLE",
        "engine_status_reason": "",
        "engine_status_updated": iso_now(),
        "completed_experiments": [],
        "statistics": {
            "total_experiments": 0,
            "accepted": 0,
            "rejected": 0,
            "acceptance_rate": 0,
        },
    }

def _migrate_v1_to_v2(old):
    """Migrate v1 (single current_experiment) to v2 (active_experiments array)."""
    log("Migrating experiments.json from v1 to v2 schema...")
    new = _empty_experiments()
    new["completed_experiments"] = old.get("completed_experiments", [])
    new["statistics"] = old.get("statistics", new["statistics"])

    current = old.get("current_experiment")
    if current and current.get("status") == "RUNNING":
        # Carry over the running experiment with new fields
        current.setdefault("trades_at_start", 0)
        current.setdefault("baseline_score", current.get("baseline_sharpe", 0.0))
        current.setdefault("baseline_method", "SHARPE")
        new["active_experiments"] = [current]
        new["engine_status"] = "RUNNING_EXPERIMENTS"
        new["engine_status_reason"] = f"Migrated v1 experiment {current.get('id', '?')}"
    elif current and current.get("status") in ("ACCEPTED", "REJECTED", "COMPLETED"):
        new["completed_experiments"].append(current)

    new["engine_status_updated"] = iso_now()
    return new

def save_experiments(data):
    atomic_write_json(str(EXPERIMENTS_FILE), data)

def load_strategy_params():
    return atomic_read_json(str(STRATEGY_PARAMS_FILE))

def save_strategy_params(data):
    atomic_write_json(str(STRATEGY_PARAMS_FILE), data)

def load_metrics_history():
    data = atomic_read_json(str(METRICS_HISTORY_FILE))
    if data is None:
        return {"schema_version": "1.0.0", "daily_metrics": [], "experiment_metrics": []}
    return data

def save_metrics_history(data):
    atomic_write_json(str(METRICS_HISTORY_FILE), data)

def get_bounds(params_section, param_name):
    bounds_key = f"{param_name}_bounds"
    if bounds_key in params_section:
        b = params_section[bounds_key]
        return (b[0], b[1])
    if param_name in DEFAULT_BOUNDS:
        return DEFAULT_BOUNDS[param_name]
    current = params_section.get(param_name, 1)
    return (current * 0.5, current * 1.5)

def clamp(value, lo, hi):
    return max(lo, min(hi, value))

def generate_experiment_id():
    now = now_utc()
    return f"exp_{now.strftime('%Y%m%d')}_{random.randint(1, 999):03d}"


# ---------------------------------------------------------------------------
# Trade counting --the backbone of the new system
# ---------------------------------------------------------------------------

def load_all_trades():
    """Load all trades from the JSONL log."""
    return load_trades(str(TRADES_FILE))

def count_closed_trades_by_strategy(trades, since=None):
    """
    Count trades that represent closed positions (sells, profit takes, stop exits).
    These are the trades that actually have realized PnL and teach us something.

    Returns: dict[strategy_name] -> int
    """
    sell_actions = {"SELL", "PROFIT_TAKE", "STOP_SELL", "EXIT", "CLOSE"}
    counts = defaultdict(int)
    for t in trades:
        strategy = t.get("strategy", "")
        action = t.get("action", "").upper()
        ts = t.get("timestamp", "")

        if since and ts < since:
            continue

        # Count sells / profit takes / exits --these close positions
        if action in sell_actions or t.get("exit_reason"):
            counts[strategy] += 1

    return dict(counts)

def count_all_trades_by_strategy(trades, since=None):
    """Count all trades (buys + sells) per strategy since a given time.
    Excludes BLOCKED records -- these are filter diagnostics, not real trades."""
    counts = defaultdict(int)
    for t in trades:
        action = t.get("action", "").upper()
        if action == "BLOCKED":
            continue
        strategy = t.get("strategy", "")
        ts = t.get("timestamp", "")
        if since and ts < since:
            continue
        if strategy:
            counts[strategy] += 1
    return dict(counts)

def get_strategy_trades(trades, strategy, since=None):
    """Get all trade records for a specific strategy since a given time."""
    result = []
    for t in trades:
        if t.get("strategy") == strategy:
            ts = t.get("timestamp", "")
            if since and ts < since:
                continue
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Blocked-trade intelligence (Phase 5)
# ---------------------------------------------------------------------------

def analyze_blocked_trades(all_trades, since=None):
    """
    Compute filter efficiency from blocked trade records.
    For each blocked trade, check if subsequent price data shows the block
    was correct (price fell) or a missed opportunity (price rose).
    Returns: dict[filter_name] -> {blocked, would_have_won, would_have_lost, accuracy}
    """
    blocked = [t for t in all_trades
               if t.get("action") == "BLOCKED"
               and (not since or t.get("timestamp", "") >= since)]

    if not blocked:
        return {}

    by_filter = defaultdict(lambda: {"blocked": 0, "would_have_won": 0,
                                      "would_have_lost": 0, "neutral": 0})

    for b in blocked:
        filter_name = b.get("filter", "unknown")
        by_filter[filter_name]["blocked"] += 1

        symbol = b.get("symbol")
        block_price = (b.get("indicators") or {}).get("price", 0)
        block_time = b.get("timestamp", "")
        if block_price <= 0 or not block_time:
            continue

        # Look ahead: find price data for this symbol within 24 hours after block
        future_return = _compute_future_return(all_trades, symbol, block_time, block_price, hours=24)
        if future_return is not None:
            if future_return > 0.02:
                by_filter[filter_name]["would_have_won"] += 1
            elif future_return < -0.02:
                by_filter[filter_name]["would_have_lost"] += 1
            else:
                by_filter[filter_name]["neutral"] += 1

    for f, stats in by_filter.items():
        decisive = stats["would_have_won"] + stats["would_have_lost"]
        stats["accuracy"] = round(stats["would_have_lost"] / decisive, 3) if decisive > 0 else None

    return dict(by_filter)


def _compute_future_return(all_trades, symbol, after_ts, block_price, hours=24):
    """Find price data for symbol in the window after a block and compute return."""
    try:
        cutoff = datetime.fromisoformat(after_ts)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        end = cutoff + timedelta(hours=hours)
    except Exception:
        return None

    prices = []
    for t in all_trades:
        ts = t.get("timestamp", "")
        if not ts or t.get("symbol") != symbol:
            continue
        if t.get("action") == "BLOCKED":
            continue
        try:
            tt = datetime.fromisoformat(ts)
            if tt.tzinfo is None:
                tt = tt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if cutoff < tt <= end:
            p = (t.get("indicators") or {}).get("price") or t.get("fill_price") or t.get("price")
            if p:
                try:
                    prices.append(float(p))
                except (ValueError, TypeError):
                    pass

    if not prices:
        return None
    return (prices[-1] - block_price) / block_price


# ---------------------------------------------------------------------------
# Performance tracker feedback -- auto-adjust signal weights
# ---------------------------------------------------------------------------

def apply_weight_recommendations(experiments):
    """
    Read recommended weights from performance tracker and auto-adjust.
    Guards:
    - Skips signals with ANY active experiment (prevents dual-variable testing)
    - Staleness check: skips if signal_scores.json older than 6 hours
    - Only moves halfway toward recommendation (conservative damping)
    """
    import subprocess

    # Run performance tracker first for fresh scores
    try:
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "performance_tracker.py"), "--quiet"],
            capture_output=True, timeout=60
        )
    except Exception as e:
        log(f"  Warning: Could not refresh performance tracker: {e}")

    scores_file = STATE_DIR / "signal_scores.json"
    scores = atomic_read_json(str(scores_file))
    if not scores:
        return 0

    # Staleness check: skip if scores older than 6 hours
    last_updated = scores.get("last_updated", "")
    if last_updated:
        try:
            updated_dt = datetime.fromisoformat(last_updated)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_hours = (now_utc() - updated_dt).total_seconds() / 3600
            if age_hours > 6:
                log(f"  Signal scores are {age_hours:.1f}h old (>6h), skipping weight adjustments")
                return 0
        except Exception:
            pass

    weights_file = STATE_DIR / "signal_weights.json"
    weights_data = atomic_read_json(str(weights_file))
    if not weights_data or "weights" not in weights_data:
        return 0

    # Build set of signals with ANY active experiment (not just _weight)
    active_signal_keys = set()
    for e in experiments.get("active_experiments", []):
        sk = e.get("signal_key", "")
        if sk:
            active_signal_keys.add(sk)

    adjustments = 0
    scoreboard = scores.get("signal_scoreboard", {})

    for signal_type, score_info in scoreboard.items():
        recommended = score_info.get("recommended_weight")
        if recommended is None:
            continue

        # Skip signals with active experiments
        if signal_type in active_signal_keys:
            log(f"  Weight skip: {signal_type} has active experiment, not adjusting")
            continue

        current_entry = weights_data.get("weights", {}).get(signal_type, {})
        if not isinstance(current_entry, dict):
            continue
        current_weight = current_entry.get("weight", 1.0)

        diff = recommended - current_weight
        if abs(diff) < 0.2:
            continue  # Too small to bother

        # Move halfway toward recommendation (conservative)
        new_weight = current_weight + diff * 0.5
        new_weight = max(0.0, min(2.0, round(new_weight, 2)))

        weights_data["weights"][signal_type]["weight"] = new_weight
        weights_data["weights"][signal_type]["last_tuned"] = iso_now()
        adjustments += 1
        log(f"  Weight auto-adjust: {signal_type} {current_weight:.2f} -> {new_weight:.2f} "
            f"(recommended: {recommended:.2f})")

    if adjustments > 0:
        weights_data["last_updated"] = iso_now()
        atomic_write_json(str(weights_file), weights_data)
        git_commit(f"Auto-adjusted {adjustments} signal weights from performance tracker recommendations")

    return adjustments


# ---------------------------------------------------------------------------
# Composite scoring --the key innovation
# ---------------------------------------------------------------------------

def composite_score(trades):
    """
    Adaptive scoring based on sample size:
      < 5 trades:  INSUFFICIENT --can't score
      5-14 trades: WIN_RATE --simple, stable, works with few data points
      15-49 trades: PROFIT_FACTOR --captures magnitude, not just direction
      50+ trades:  SHARPE --statistically meaningful with enough data

    Returns: (score: float, method: str, details: dict)
    Score is always in the same direction: higher = better.
    """
    if not trades or len(trades) < MIN_TRADES_TO_EVALUATE:
        return 0.0, "INSUFFICIENT", {"trade_count": len(trades) if trades else 0}

    n = len(trades)
    pnls = [t.get("pnl", 0) for t in trades]

    # Guard: all-zero PnL means no real trading happened (e.g., only buys, no closes)
    if all(p == 0 for p in pnls):
        return 0.0, "INSUFFICIENT", {"trade_count": n, "reason": "all_zero_pnl"}

    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n

    if n < 15:
        # Small sample: win rate, boosted by avg_pnl direction
        # Use sign of avg_pnl with a fixed ±0.05 bonus to avoid scale dependency
        pnl_bonus = 0.05 if avg_pnl > 0 else (-0.05 if avg_pnl < 0 else 0.0)
        score = win_rate + pnl_bonus
        method = "WIN_RATE"
        details = {"win_rate": round(win_rate, 3), "avg_pnl": round(avg_pnl, 2),
                   "pnl_bonus": round(pnl_bonus, 3), "trade_count": n}

    elif n < 50:
        # Medium sample: profit factor (gross_profit / gross_loss)
        gross_profit = sum(p for p in pnls if p > 0) or 0.01
        gross_loss = abs(sum(p for p in pnls if p < 0)) or 0.01
        profit_factor = min(gross_profit / gross_loss, 10.0)  # Cap to avoid blow-up when all wins/losses
        # Normalize: PF of 1.0 = breakeven, 2.0 = good, 3.0+ = excellent
        score = profit_factor
        method = "PROFIT_FACTOR"
        details = {"profit_factor": round(profit_factor, 3), "gross_profit": round(gross_profit, 2),
                   "gross_loss": round(gross_loss, 2), "win_rate": round(win_rate, 3),
                   "trade_count": n}

    else:
        # Large sample: Sharpe ratio
        from metrics import calculate_sharpe
        sharpe = calculate_sharpe(trades)
        score = sharpe
        method = "SHARPE"
        details = {"sharpe": sharpe, "win_rate": round(win_rate, 3),
                   "total_pnl": round(total_pnl, 2), "trade_count": n}

    return round(score, 4), method, details


def min_improvement_for_method(method):
    """
    How much better does the experiment need to be to accept?
    Adapted per metric type because they live on different scales.
    """
    return {
        "WIN_RATE": 0.03,        # 3% win rate improvement
        "PROFIT_FACTOR": 0.15,   # PF 1.5 -> 1.65
        "SHARPE": 0.1,           # Sharpe 0.5 -> 0.6
        "INSUFFICIENT": 999,     # Never accept with insufficient data
    }.get(method, 0.1)


# ---------------------------------------------------------------------------
# Directional learning (carried over from v1, works the same)
# ---------------------------------------------------------------------------

def get_mutation_direction(param_name, strategy, completed_experiments):
    """
    Learn from past experiments which direction to push a parameter.
    Returns: float in [-1, 1] (positive = increase worked, negative = decrease worked)
    """
    history = []
    for exp in completed_experiments:
        if exp.get("parameter") == param_name and exp.get("strategy") == strategy:
            direction = "increase" if exp.get("test_value", 0) > exp.get("original_value", 0) else "decrease"
            outcome = exp.get("status")
            history.append({"direction": direction, "outcome": outcome,
                          "improvement": exp.get("score_improvement", exp.get("sharpe_improvement", 0))})

    if not history:
        return 0.0

    increase_score = 0
    decrease_score = 0
    for h in history:
        if h["direction"] == "increase":
            increase_score += 1.0 if h["outcome"] == "ACCEPTED" else -0.5
        else:
            decrease_score += 1.0 if h["outcome"] == "ACCEPTED" else -0.5

    total = abs(increase_score) + abs(decrease_score)
    if total == 0:
        return 0.0
    return round((increase_score - decrease_score) / total, 3)


def select_parameter(valid_params, strategy, completed_experiments):
    """Weighted random selection: prefer untested params, then successful ones."""
    tested_params = {}
    for exp in completed_experiments:
        if exp.get("strategy") == strategy:
            p = exp.get("parameter")
            if p not in tested_params:
                tested_params[p] = {"accepted": 0, "rejected": 0}
            if exp.get("status") == "ACCEPTED":
                tested_params[p]["accepted"] += 1
            elif exp.get("status") == "REJECTED":
                tested_params[p]["rejected"] += 1

    scored = []
    for p in valid_params:
        if p not in tested_params:
            score = 10  # Never tested --explore first
        else:
            t = tested_params[p]
            if t["accepted"] > t["rejected"]:
                score = 7
            elif t["accepted"] > 0:
                score = 5
            elif t["rejected"] <= 2:
                score = 3
            else:
                score = 1
        scored.append((p, score))

    total_score = sum(s for _, s in scored)
    r = random.uniform(0, total_score)
    cumulative = 0
    for p, s in scored:
        cumulative += s
        if r <= cumulative:
            return p
    return scored[-1][0]


# ---------------------------------------------------------------------------
# Slot management --which config sections have active experiments?
# ---------------------------------------------------------------------------

def get_active_slot_sections(experiments):
    """Return set of config sections that currently have a running experiment."""
    return {e["config_section"] for e in experiments.get("active_experiments", [])
            if e.get("status") == "RUNNING"}


def get_experiment_for_slot(experiments, section):
    """Get the active experiment for a given config section, if any."""
    for e in experiments.get("active_experiments", []):
        if e.get("config_section") == section and e.get("status") == "RUNNING":
            return e
    return None


def slot_cooldown_ok(experiments, section):
    """Check if enough time has passed since the last completed experiment in this slot."""
    completed = experiments.get("completed_experiments", [])
    slot_completed = [e for e in completed if e.get("config_section") == section]
    if not slot_completed:
        return True

    latest = max(slot_completed, key=lambda e: e.get("completed_at", "2000-01-01"))
    ts = latest.get("completed_at")
    if not ts:
        return True

    last_end = datetime.fromisoformat(ts)
    if last_end.tzinfo is None:
        last_end = last_end.replace(tzinfo=timezone.utc)

    elapsed = now_utc() - last_end
    ep = get_eval_params(section)
    return elapsed >= timedelta(hours=ep["cooldown_hours"])


# ---------------------------------------------------------------------------
# Experiment readiness check
# ---------------------------------------------------------------------------

def experiment_ready_to_evaluate(experiment, trades):
    """
    Decide if an experiment has enough data or time to evaluate.

    Triggers (any one is sufficient):
      1. EVAL_TRADE_TARGET new closed trades in the strategy since experiment start
      2. EVAL_MAX_DAYS elapsed (evaluate with whatever we have)

    Blocks:
      - Less than EVAL_MIN_DAYS elapsed (too early, noise)
    """
    started_at = experiment.get("started_at", "")
    if not started_at:
        return False, "no start time"

    start_dt = datetime.fromisoformat(started_at)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    elapsed = now_utc() - start_dt
    elapsed_days = elapsed.total_seconds() / 86400

    section = experiment.get("config_section", "")
    ep = get_eval_params(section)

    # Hard floor: don't evaluate before min days (slot-type-aware)
    if elapsed_days < ep["min_days"]:
        return False, f"too early ({elapsed_days:.1f}d < {ep['min_days']}d minimum)"

    # Count relevant closed trades since experiment start
    if section.startswith("crypto."):
        # Per-signal: match by signal_type (for buys) OR entry_signal_type (for exits)
        signal_type = experiment.get("signal_key", experiment.get("strategy", ""))
        sell_actions = {"SELL", "PROFIT_TAKE", "STOP_SELL", "EXIT", "CLOSE"}
        total_related_closed = sum(
            1 for t in trades
            if (t.get("signal_type") == signal_type or t.get("entry_signal_type") == signal_type)
            and (not started_at or t.get("timestamp", "") >= started_at)
            and (t.get("action", "").upper() in sell_actions or t.get("exit_reason"))
        )
    else:
        # Strategy-level: count by strategy name
        strategy = experiment.get("strategy", "")
        closed_since_start = count_closed_trades_by_strategy(trades, since=started_at)
        related = EXPERIMENT_SLOTS.get(section, [])
        total_related_closed = sum(closed_since_start.get(s, 0) for s in related)

    # Trigger 1: enough new closed trades (slot-type-aware target)
    if total_related_closed >= ep["trade_target"]:
        return True, f"{total_related_closed} new closed trades (target: {ep['trade_target']})"

    # Trigger 2: max time elapsed (evaluate with whatever data we have)
    if elapsed_days >= ep["max_days"]:
        return True, f"max eval time reached ({elapsed_days:.1f}d >= {ep['max_days']}d)"

    remaining_days = ep["max_days"] - elapsed_days
    return False, (f"{total_related_closed}/{ep['trade_target']} closed trades, "
                   f"{remaining_days:.1f}d until forced eval")


# ---------------------------------------------------------------------------
# Phase: Evaluate a single experiment
# ---------------------------------------------------------------------------

def evaluate_experiment(experiment, experiments, strategy_params, all_trades):
    """
    Evaluate one running experiment using composite scoring.
    Accept if score improved by the method-appropriate threshold.
    """
    exp_id = experiment["id"]
    strategy = experiment["strategy"]
    param_name = experiment["parameter"]
    section_key = experiment["config_section"]
    original_value = experiment["original_value"]
    test_value = experiment["test_value"]
    baseline_score = experiment.get("baseline_score", 0.0)
    baseline_method = experiment.get("baseline_method", "WIN_RATE")

    # Get trades for evaluation -- crypto signal slots filter by signal_type,
    # non-crypto slots filter by strategy name
    started_at = experiment.get("started_at", "")

    eval_trades = []
    if section_key.startswith("crypto."):
        # Per-signal evaluation: filter by signal_type field
        signal_type = experiment.get("signal_key", experiment.get("strategy", ""))
        eval_trades = [t for t in all_trades
                       if (t.get("signal_type") == signal_type or t.get("entry_signal_type") == signal_type)
                       and (not started_at or t.get("timestamp", "") >= started_at)
                       and t.get("action", "").upper() != "BLOCKED"]
    else:
        # Strategy-level evaluation (existing behavior)
        section_strategies = EXPERIMENT_SLOTS.get(section_key, [strategy])
        for s in section_strategies:
            eval_trades.extend(get_strategy_trades(all_trades, s, since=started_at))

    # Score the experiment period
    new_score, new_method, details = composite_score(eval_trades)

    # Use the MORE conservative threshold (in case method changed due to more data)
    threshold = max(min_improvement_for_method(baseline_method),
                    min_improvement_for_method(new_method))
    score_diff = new_score - baseline_score

    log(f"  Evaluating {exp_id}:")
    log(f"    Strategy:  {strategy} | Param: {param_name} = {test_value} (was {original_value})")
    log(f"    Baseline:  {baseline_score:.4f} ({baseline_method})")
    log(f"    Current:   {new_score:.4f} ({new_method})")
    log(f"    Diff:      {score_diff:+.4f} | Threshold: {threshold}")
    log(f"    Details:   {details}")

    if new_method == "INSUFFICIENT":
        # Not enough closed trades to score --reject but don't penalize
        decision = "REJECTED"
        log(f"    REJECTED --insufficient data ({details.get('trade_count', 0)} trades)")
    elif score_diff >= threshold:
        decision = "ACCEPTED"
        log(f"    ACCEPTED --score improved by {score_diff:.4f}")
    else:
        decision = "REJECTED"
        log(f"    REJECTED --improvement {score_diff:.4f} < threshold {threshold}")

    # Apply decision
    experiment["status"] = decision
    experiment["result_score"] = new_score
    experiment["result_method"] = new_method
    experiment["score_improvement"] = round(score_diff, 4)
    experiment["eval_details"] = details
    experiment["completed_at"] = iso_now()

    if decision == "REJECTED":
        # Revert parameter -- path depends on param type and config location
        reverted = False
        if param_name == "_weight":
            # Weight lives in signal_weights.json, not strategy_params
            weights_file = STATE_DIR / "signal_weights.json"
            weights_data = atomic_read_json(str(weights_file)) or {"schema_version": "1.0.0", "weights": {}}
            signal_key = experiment.get("signal_key", "")
            if signal_key in weights_data.get("weights", {}):
                weights_data["weights"][signal_key]["weight"] = original_value
                weights_data["weights"][signal_key]["last_tuned"] = iso_now()
                weights_data["last_updated"] = iso_now()
                atomic_write_json(str(weights_file), weights_data)
                reverted = True
        elif section_key.startswith("crypto."):
            signal_key = experiment.get("signal_key", "")
            cs = strategy_params.get("crypto_strategy", {})
            sig_section = cs.get("signal", {}).get(signal_key, {})
            if param_name in sig_section:
                sig_section[param_name] = original_value
                strategy_params["last_updated"] = iso_now()
                save_strategy_params(strategy_params)
                reverted = True
        else:
            if section_key in strategy_params and param_name in strategy_params[section_key]:
                strategy_params[section_key][param_name] = original_value
                strategy_params["last_updated"] = iso_now()
                save_strategy_params(strategy_params)
                reverted = True

        if reverted:
            log(f"    Reverted {param_name} to {original_value}")

        experiments["statistics"]["rejected"] = experiments["statistics"].get("rejected", 0) + 1
    else:
        # Keep new value
        experiments["statistics"]["accepted"] = experiments["statistics"].get("accepted", 0) + 1

    # Move to completed
    experiments["active_experiments"] = [
        e for e in experiments["active_experiments"] if e["id"] != exp_id
    ]
    experiments["completed_experiments"].append(experiment)

    # Update stats
    accepted = experiments["statistics"].get("accepted", 0)
    rejected = experiments["statistics"].get("rejected", 0)
    total_decided = accepted + rejected
    experiments["statistics"]["acceptance_rate"] = (
        round(accepted / total_decided, 4) if total_decided > 0 else 0
    )

    # Log to metrics history
    history = load_metrics_history()
    history["experiment_metrics"].append({
        "timestamp": iso_now(),
        "type": "evaluation",
        "experiment_id": exp_id,
        "strategy": strategy,
        "parameter": param_name,
        "original_value": original_value,
        "test_value": test_value,
        "baseline_score": baseline_score,
        "baseline_method": baseline_method,
        "result_score": new_score,
        "result_method": new_method,
        "score_improvement": round(score_diff, 4),
        "decision": decision,
        "eval_details": details,
    })
    save_metrics_history(history)

    git_commit(
        f"Experiment {exp_id} {decision}: {param_name}={'kept' if decision == 'ACCEPTED' else 'reverted'} "
        f"({baseline_method} {baseline_score:.3f} -> {new_method} {new_score:.3f})"
    )

    return decision


# ---------------------------------------------------------------------------
# Phase: Launch a new experiment for one slot
# ---------------------------------------------------------------------------

def _deploy_backtest_experiment(section, signal_type, backtest_result, experiments,
                                strategy_params, baseline_score, baseline_method,
                                baseline_details, signal_trades):
    """Deploy the best backtest-validated param set as a live experiment."""
    exp_id = generate_experiment_id()
    best_params = backtest_result["params"]

    # Find which params changed from current
    cs = strategy_params.get("crypto_strategy", {})
    signal_section = cs.get("signal", {}).get(signal_type, {})

    changes = {}
    for key, val in best_params.get("signal", {}).get(signal_type, {}).items():
        if key.endswith("_bounds") or key == "enabled":
            continue
        current = signal_section.get(key)
        if current is not None and val != current:
            changes[key] = {"from": current, "to": val}

    if not changes:
        log(f"  Backtest winner has same params as current -- no experiment needed")
        return None

    # Pick the most impactful change (or first one)
    chosen_param = list(changes.keys())[0]
    original_value = changes[chosen_param]["from"]
    new_value = changes[chosen_param]["to"]

    experiment = {
        "id": exp_id,
        "strategy": signal_type,
        "signal_key": signal_type,
        "parameter": chosen_param,
        "config_section": section,
        "original_value": original_value,
        "test_value": new_value,
        "bounds": list(get_bounds(signal_section, chosen_param)),
        "baseline_score": baseline_score,
        "baseline_method": baseline_method,
        "baseline_details": baseline_details,
        "source": "backtest",
        "backtest_test_score": backtest_result.get("avg_test_score", 0),
        "backtest_oos_score": backtest_result.get("oos_avg_score", 0),
        "backtest_trade_count": backtest_result.get("test_trade_count", 0),
        "all_backtest_changes": changes,
        "mutation_direction": "increase" if new_value > original_value else "decrease",
        "trades_at_start": len(signal_trades),
        "status": "RUNNING",
        "started_at": iso_now(),
    }

    # Deploy: write new value to config
    signal_section[chosen_param] = new_value
    strategy_params["last_updated"] = iso_now()
    save_strategy_params(strategy_params)

    experiments["active_experiments"].append(experiment)
    experiments["statistics"]["total_experiments"] = experiments["statistics"].get("total_experiments", 0) + 1
    save_experiments(experiments)

    log(f"  LAUNCHED (backtest-validated): {exp_id}")
    log(f"    {signal_type}.{chosen_param}: {original_value} -> {new_value}")
    log(f"    Backtest score: {backtest_result.get('avg_test_score', 0):.4f}, "
        f"OOS: {backtest_result.get('oos_avg_score', 'N/A')}")

    git_commit(f"Experiment {exp_id}: backtest-validated {signal_type}.{chosen_param} "
               f"{original_value} -> {new_value}")

    return experiment


def _launch_signal_experiment(section, experiments, strategy_params, all_trades):
    """Launch a per-signal experiment for a crypto signal slot.
    Tries backtest-validated params first, falls back to random mutation.
    Reads/writes params at crypto_strategy.signal.<signal_key>.<param>."""
    signal_type = section.split(".", 1)[1]
    mapping = SIGNAL_PARAM_MAP.get(signal_type)
    if not mapping:
        log(f"  Signal [{signal_type}]: no mapping in SIGNAL_PARAM_MAP")
        return None

    completed = experiments.get("completed_experiments", [])

    # --- Try backtest-validated params FIRST (before live trade check) ---
    backtest_enabled = strategy_params.get("self_improvement", {}).get("backtest_enabled", False)
    backtest_candidate = None
    if backtest_enabled:
        try:
            from backtester import backtest_and_rank
            log(f"  Signal [{signal_type}]: running walk-forward backtest...")
            from crypto_strategy import load_params
            base_params = load_params()
            results = backtest_and_rank(signal_type, base_params=base_params)
            if results and results[0]["avg_test_score"] > 0 and not results[0].get("overfit_flag"):
                backtest_candidate = results[0]
                log(f"    Backtest winner: test_score={backtest_candidate['avg_test_score']:.4f}, "
                    f"trades={backtest_candidate['test_trade_count']}, "
                    f"oos={backtest_candidate.get('oos_avg_score', 'N/A')}")
        except Exception as e:
            log(f"  Backtest failed for {signal_type}: {e}")
            log(f"  Falling back to random mutation")

    # Check recent trades for this signal type
    recent_cutoff = (now_utc() - timedelta(days=14)).isoformat()
    signal_trades = [t for t in all_trades
                     if (t.get("signal_type") == signal_type or t.get("entry_signal_type") == signal_type)
                     and t.get("timestamp", "") >= recent_cutoff
                     and t.get("action", "").upper() != "BLOCKED"]

    # With backtest candidate, we can proceed even without live trades
    if not backtest_candidate and len(signal_trades) < MIN_TRADES_FOR_EXPERIMENT:
        log(f"  Signal [{signal_type}]: not enough trades ({len(signal_trades)}) in 14d and no backtest")
        return None

    # Compute baseline score from live trades (or use 0 if backtest-sourced)
    if signal_trades:
        score, method, details = composite_score(signal_trades)
    else:
        score, method, details = 0.0, "BACKTEST_ONLY", {"trade_count": 0}

    if not backtest_candidate and method == "INSUFFICIENT":
        log(f"  Signal [{signal_type}]: insufficient baseline data ({details.get('trade_count', 0)} trades)")
        return None

    log(f"  Signal [{signal_type}]: baseline {score:.4f} ({method}, {details.get('trade_count', 0)} trades)")

    # Get signal-specific params from config v2
    cs = strategy_params.get("crypto_strategy", {})
    signal_section = cs.get("signal", {}).get(signal_type, {})

    # _weight is always valid (lives in signal_weights.json, not strategy_params)
    valid_params = [p for p in mapping["params"] if p in signal_section or p == "_weight"]
    if not valid_params:
        log(f"  Signal [{signal_type}]: no tunable params found in config")
        return None

    # --- If backtest provided a winner, use it instead of random mutation ---
    if backtest_candidate:
        return _deploy_backtest_experiment(
            section, signal_type, backtest_candidate, experiments,
            strategy_params, score, method, details, signal_trades
        )

    # Select param to mutate (random mutation fallback)
    chosen_param = select_parameter(valid_params, signal_type, completed)

    # _weight param lives in signal_weights.json, not strategy_params
    is_weight_param = (chosen_param == "_weight")
    if is_weight_param:
        weights_file = STATE_DIR / "signal_weights.json"
        weights_data = atomic_read_json(str(weights_file)) or {"schema_version": "1.0.0", "weights": {}}
        current_value = weights_data.get("weights", {}).get(signal_type, {}).get("weight", 1.0)
        bounds = DEFAULT_BOUNDS["_weight"]
    else:
        current_value = signal_section[chosen_param]
        bounds = get_bounds(signal_section, chosen_param)
    lo, hi = bounds

    max_change_pct = strategy_params.get("self_improvement", {}).get("max_param_change_pct", 20)

    # Directional bias from history
    bias = get_mutation_direction(chosen_param, signal_type, completed)

    # Filter intelligence: override bias for filter threshold params based on accuracy
    filter_threshold_params = {
        "vwap_slope_chop_threshold": "vwap_slope_chop",
        "adr_exhaustion_pct": "adr_exhaustion",
        "engulf_vol_multiplier": "engulf_vol",
    }

    if chosen_param in filter_threshold_params:
        filter_name = filter_threshold_params[chosen_param]
        filter_acc = analyze_blocked_trades(all_trades, since=recent_cutoff)
        acc_data = filter_acc.get(filter_name, {})
        accuracy = acc_data.get("accuracy")

        if accuracy is not None:
            if accuracy < 0.5:
                # Filter blocks too many good trades -- loosen
                bias = 0.5 if chosen_param != "engulf_vol_multiplier" else -0.5
                log(f"    Filter intelligence: {filter_name} accuracy={accuracy:.0%} < 50%, biasing LOOSEN")
            elif accuracy > 0.8:
                # Filter works well -- tighten slightly
                bias = -0.3 if chosen_param != "engulf_vol_multiplier" else 0.3
                log(f"    Filter intelligence: {filter_name} accuracy={accuracy:.0%} > 80%, biasing TIGHTEN")

    if abs(bias) > 0.3:
        if bias > 0:
            random_change = random.uniform(0, max_change_pct / 100)
        else:
            random_change = random.uniform(-max_change_pct / 100, 0)
        log(f"    Bias: {'INCREASE' if bias > 0 else 'DECREASE'} ({bias:+.3f})")
    else:
        random_change = random.uniform(-max_change_pct / 100, max_change_pct / 100)
        log(f"    Bias: NONE ({bias:+.3f}), exploring randomly")

    new_value = current_value * (1 + random_change)
    new_value = clamp(new_value, lo, hi)

    if isinstance(current_value, int) and not is_weight_param:
        new_value = int(round(new_value))
        if new_value == current_value:
            new_value = current_value + (1 if random_change >= 0 else -1)
            new_value = int(clamp(new_value, lo, hi))
    else:
        new_value = round(new_value, 4)

    exp_id = generate_experiment_id()
    experiment = {
        "id": exp_id,
        "strategy": signal_type,
        "signal_key": signal_type,
        "parameter": chosen_param,
        "config_section": section,
        "original_value": current_value,
        "test_value": new_value,
        "bounds": list(bounds),
        "baseline_score": score,
        "baseline_method": method,
        "baseline_details": details,
        "mutation_direction": "increase" if new_value > current_value else "decrease",
        "direction_bias": bias,
        "trades_at_start": len(signal_trades),
        "status": "RUNNING",
        "started_at": iso_now(),
    }

    # Deploy: write new value to the appropriate file
    if is_weight_param:
        # Weight lives in signal_weights.json
        weights_data.setdefault("weights", {}).setdefault(signal_type, {"weight": 1.0, "auto_tuned": True, "last_tuned": None})
        weights_data["weights"][signal_type]["weight"] = round(new_value, 4)
        weights_data["weights"][signal_type]["last_tuned"] = iso_now()
        weights_data["last_updated"] = iso_now()
        atomic_write_json(str(weights_file), weights_data)
    else:
        # Normal param lives in strategy_params.json
        signal_section[chosen_param] = new_value
        strategy_params["last_updated"] = iso_now()
        save_strategy_params(strategy_params)

    # Add to active experiments
    experiments["active_experiments"].append(experiment)
    experiments["statistics"]["total_experiments"] = (
        experiments["statistics"].get("total_experiments", 0) + 1
    )

    log(f"  DEPLOYED: {exp_id} --signal.{signal_type}.{chosen_param} = {current_value} -> {new_value}")

    git_commit(
        f"Experiment {exp_id}: signal.{signal_type}.{chosen_param}={new_value} (was {current_value})"
    )

    return experiment


def launch_experiment_for_slot(section, experiments, strategy_params, all_trades):
    """
    Find the best strategy to experiment on within a slot, design and deploy.
    Delegates to _launch_signal_experiment for crypto signal slots.
    Returns the experiment dict, or None if nothing to do.
    """
    # Crypto signal slots use per-signal experiment logic
    if section.startswith("crypto."):
        return _launch_signal_experiment(section, experiments, strategy_params, all_trades)

    strategies_in_slot = EXPERIMENT_SLOTS.get(section, [])
    completed = experiments.get("completed_experiments", [])

    # Find which strategies in this slot have recent activity
    recent_cutoff = (now_utc() - timedelta(days=14)).isoformat()
    trade_counts = count_all_trades_by_strategy(all_trades, since=recent_cutoff)

    active_strategies = [
        s for s in strategies_in_slot
        if trade_counts.get(s, 0) >= MIN_TRADES_FOR_EXPERIMENT
    ]

    if not active_strategies:
        log(f"  Slot [{section}]: no active strategies (need {MIN_TRADES_FOR_EXPERIMENT}+ trades in 14d)")
        return None

    # Score each active strategy --pick the worst performer to improve
    strategy_scores = {}
    for strat in active_strategies:
        strat_trades = get_strategy_trades(all_trades, strat, since=recent_cutoff)
        score, method, details = composite_score(strat_trades)
        strategy_scores[strat] = {"score": score, "method": method, "details": details}
        log(f"    {strat}: {score:.4f} ({method}, {details.get('trade_count', 0)} trades)")

    # Target the weakest active strategy
    weakest = min(strategy_scores.keys(), key=lambda s: strategy_scores[s]["score"])
    baseline = strategy_scores[weakest]
    log(f"  Slot [{section}]: targeting {weakest} (score={baseline['score']:.4f})")

    # SAFETY: refuse to launch when baseline is INSUFFICIENT --
    # any nonzero score would look like an improvement against 0.0
    if baseline["method"] == "INSUFFICIENT":
        log(f"  Slot [{section}]: {weakest} has insufficient data for baseline ({baseline['details'].get('trade_count', 0)} trades), skipping")
        return None

    # Design experiment
    mapping = STRATEGY_PARAM_MAP.get(weakest)
    if not mapping:
        log(f"  No tunable params for {weakest}")
        return None

    params_section = strategy_params.get(mapping["section"], {})
    valid_params = [p for p in mapping["params"] if p in params_section]
    if not valid_params:
        log(f"  No valid params in config section '{mapping['section']}'")
        return None

    chosen_param = select_parameter(valid_params, weakest, completed)
    current_value = params_section[chosen_param]
    bounds = get_bounds(params_section, chosen_param)
    lo, hi = bounds

    max_change_pct = strategy_params.get("self_improvement", {}).get("max_param_change_pct", 20)

    # Directional bias from history
    bias = get_mutation_direction(chosen_param, weakest, completed)
    if abs(bias) > 0.3:
        if bias > 0:
            random_change = random.uniform(0, max_change_pct / 100)
        else:
            random_change = random.uniform(-max_change_pct / 100, 0)
        log(f"    Bias: {'INCREASE' if bias > 0 else 'DECREASE'} ({bias:+.3f})")
    else:
        random_change = random.uniform(-max_change_pct / 100, max_change_pct / 100)
        log(f"    Bias: NONE ({bias:+.3f}), exploring randomly")

    new_value = current_value * (1 + random_change)
    new_value = clamp(new_value, lo, hi)

    if isinstance(current_value, int):
        new_value = int(round(new_value))
        if new_value == current_value:
            new_value = current_value + (1 if random_change >= 0 else -1)
            new_value = int(clamp(new_value, lo, hi))
    else:
        new_value = round(new_value, 4)

    # Count current closed trades for this strategy (for trade-count trigger)
    all_closed = count_closed_trades_by_strategy(all_trades)
    slot_closed = sum(all_closed.get(s, 0) for s in strategies_in_slot)

    exp_id = generate_experiment_id()
    experiment = {
        "id": exp_id,
        "strategy": weakest,
        "parameter": chosen_param,
        "config_section": section,
        "original_value": current_value,
        "test_value": new_value,
        "bounds": list(bounds),
        "baseline_score": baseline["score"],
        "baseline_method": baseline["method"],
        "baseline_details": baseline["details"],
        "mutation_direction": "increase" if new_value > current_value else "decrease",
        "direction_bias": bias,
        "trades_at_start": slot_closed,
        "status": "RUNNING",
        "started_at": iso_now(),
    }

    # Deploy: update config
    strategy_params[section][chosen_param] = new_value
    strategy_params["last_updated"] = iso_now()
    save_strategy_params(strategy_params)

    # Add to active experiments
    experiments["active_experiments"].append(experiment)
    experiments["statistics"]["total_experiments"] = (
        experiments["statistics"].get("total_experiments", 0) + 1
    )

    log(f"  DEPLOYED: {exp_id} --{weakest}.{chosen_param} = {current_value} -> {new_value}")

    git_commit(
        f"Experiment {exp_id}: {weakest}.{chosen_param}={new_value} (was {current_value})"
    )

    return experiment


# ---------------------------------------------------------------------------
# Exit condition checks
# ---------------------------------------------------------------------------

def check_exit_conditions(experiments):
    stats = experiments.get("statistics", {})
    accepted = stats.get("accepted", 0)
    rejected = stats.get("rejected", 0)
    total = accepted + rejected

    if total < 3:
        return

    completed = experiments.get("completed_experiments", [])
    if len(completed) >= 5:
        last_5 = completed[-5:]
        if all(e.get("status") == "REJECTED" for e in last_5):
            log("WARNING: 5 consecutive rejections. Consider reviewing strategy performance.")
            # Don't pause the whole engine in v2 --just log the warning
            # Individual slots will naturally cool down

    rate = stats.get("acceptance_rate", 0)
    if rate < 0.15 and total >= 8:
        log(f"NOTE: Acceptance rate {rate:.0%} is very low. May need larger mutations or longer eval windows.")
    elif rate > 0.75 and total >= 8:
        log(f"NOTE: Acceptance rate {rate:.0%} is high. System is learning fast.")


# ---------------------------------------------------------------------------
# Config v1 -> v2 migration
# ---------------------------------------------------------------------------

def migrate_config_v1_to_v2(strategy_params):
    """Migrate flat crypto_strategy config to nested shared/signal v2 format.
    Returns True if migration was performed, False if already v2 or no crypto section."""
    cs = strategy_params.get("crypto_strategy")
    if not cs:
        return False

    # Already v2 -- has both signal and shared keys
    if "signal" in cs and "shared" in cs:
        return False

    log("Migrating strategy_params.json from v1 (flat) to v2 (nested shared/signal)...")

    # Build shared params from current flat values
    shared = {
        "bb_period": cs.get("bb_period", 20),
        "bb_std": cs.get("bb_std", 2.0),
        "rsi_period": cs.get("rsi_period", 14),
        "atr_period": cs.get("atr_period", 14),
        "adx_period": cs.get("adx_period", 14),
        "adx_ranging_threshold": cs.get("adx_ranging_threshold", 25),
        "adx_trending_threshold": cs.get("adx_trending_threshold", 20),
        "ema_fast": cs.get("ema_fast", 9),
        "ema_med": cs.get("ema_med", 21),
        "ema_long": cs.get("ema_long", 55),
        "vwap_period": cs.get("vwap_period", 24),
        "vwap_slope_period": cs.get("vwap_slope_period", 5),
        "body_momentum_period": cs.get("body_momentum_period", 5),
        "vwap_filter_enabled": cs.get("vwap_filter_enabled", True),
        "adr_filter_enabled": cs.get("adr_filter_enabled", True),
        "ema_trail_enabled": cs.get("ema_trail_enabled", True),
        "adr_lookback_days": cs.get("adr_lookback_days", 10),
        "adr_exhaustion_pct": cs.get("adr_exhaustion_pct", 85),
        "ema_trail_buffer_pct": cs.get("ema_trail_buffer_pct", 0.5),
        "stop_atr_multiplier": cs.get("stop_atr_multiplier", 2.0),
        "cash_reserve_pct": cs.get("cash_reserve_pct", 0.20),
        "dca_aggressive_rsi": cs.get("dca_aggressive_rsi", 20),
    }

    # Build per-signal params from current flat values
    signal = {
        "mean_reversion_oversold": {
            "enabled": True,
            "rsi_oversold": cs.get("rsi_oversold", 35),
            "rsi_oversold_bounds": cs.get("rsi_oversold_bounds", [25, 40]),
            "bb_std": cs.get("bb_std", 2.0),
            "bb_std_bounds": cs.get("bb_std_bounds", [1.5, 3.0]),
        },
        "ema_crossover_bullish": {
            "enabled": True,
            "ema_fast": cs.get("ema_fast", 9),
            "ema_fast_bounds": cs.get("ema_fast_bounds", [5, 15]),
            "ema_med": cs.get("ema_med", 21),
            "ema_med_bounds": cs.get("ema_med_bounds", [15, 30]),
            "ema_long": cs.get("ema_long", 55),
            "ema_long_bounds": cs.get("ema_long_bounds", [40, 80]),
            "vwap_slope_chop_threshold": cs.get("vwap_slope_chop_threshold", 0.05),
            "vwap_slope_chop_threshold_bounds": cs.get("vwap_slope_chop_threshold_bounds", [0.01, 0.15]),
        },
        "dca_technical_trigger": {
            "enabled": True,
            "dca_rsi_threshold": cs.get("dca_rsi_threshold", 42),
            "dca_rsi_threshold_bounds": cs.get("dca_rsi_threshold_bounds", [30, 50]),
            "dca_aggressive_rsi": cs.get("dca_aggressive_rsi", 20),
            "rsi_oversold": cs.get("rsi_oversold", 35),
            "rsi_oversold_bounds": cs.get("rsi_oversold_bounds", [25, 40]),
        },
        "overbought_exit": {
            "enabled": True,
            "rsi_overbought": cs.get("rsi_overbought", 70),
            "rsi_overbought_bounds": cs.get("rsi_overbought_bounds", [65, 80]),
        },
        "volume_engulfing_bullish": {
            "enabled": True,
            "engulf_vol_multiplier": cs.get("engulf_vol_multiplier", 1.5),
            "engulf_vol_multiplier_bounds": cs.get("engulf_vol_multiplier_bounds", [1.2, 2.5]),
        },
        "body_momentum_bullish": {
            "enabled": True,
            "body_momentum_period": cs.get("body_momentum_period", 5),
            "body_momentum_period_bounds": cs.get("body_momentum_period_bounds", [3, 10]),
        },
        "borderline_dca": {"enabled": True},
        "borderline_trend": {
            "enabled": True,
            "ema_trail_buffer_pct": cs.get("ema_trail_buffer_pct", 0.5),
            "ema_trail_buffer_pct_bounds": cs.get("ema_trail_buffer_pct_bounds", [0.1, 2.0]),
        },
        "bb_accumulation": {"enabled": True},
    }

    # Remove flat keys that are now namespaced into shared/signal
    flat_keys_to_remove = [
        "bb_period", "bb_std", "bb_std_bounds",
        "rsi_period", "rsi_oversold", "rsi_oversold_bounds",
        "rsi_overbought", "rsi_overbought_bounds",
        "ema_fast", "ema_fast_bounds", "ema_med", "ema_med_bounds",
        "ema_long", "ema_long_bounds", "atr_period",
        "adx_period", "adx_ranging_threshold", "adx_trending_threshold",
        "dca_rsi_threshold", "dca_rsi_threshold_bounds",
        "dca_aggressive_rsi", "stop_atr_multiplier",
        "vwap_filter_enabled", "vwap_period", "vwap_period_bounds",
        "vwap_slope_period", "vwap_slope_period_bounds",
        "vwap_slope_chop_threshold", "vwap_slope_chop_threshold_bounds",
        "adr_filter_enabled", "adr_lookback_days",
        "adr_exhaustion_pct", "adr_exhaustion_pct_bounds",
        "ema_trail_enabled", "ema_trail_buffer_pct", "ema_trail_buffer_pct_bounds",
        "body_momentum_enabled", "body_momentum_period", "body_momentum_period_bounds",
        "engulf_enabled", "engulf_vol_multiplier", "engulf_vol_multiplier_bounds",
        "cash_reserve_pct",
    ]
    for k in flat_keys_to_remove:
        cs.pop(k, None)

    # Set v2 structure
    cs["shared"] = shared
    cs["signal"] = signal
    strategy_params["version"] = "2.0.0"
    strategy_params["last_updated"] = iso_now()

    save_strategy_params(strategy_params)
    git_commit("Config migration v1 -> v2: per-signal parameter isolation")
    log("Migration complete: crypto_strategy now uses shared/signal nested format")
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    log("=" * 60)
    log(f"Self-improvement engine v2 starting at {iso_now()}")
    log("=" * 60)

    experiments = load_experiments()
    strategy_params = load_strategy_params()

    if strategy_params is None:
        log("FATAL: Cannot read strategy_params.json. Aborting.")
        sys.exit(1)

    # Auto-migrate config v1 -> v2 if needed
    if migrate_config_v1_to_v2(strategy_params):
        # Reload after migration
        strategy_params = load_strategy_params()

    all_trades = load_all_trades()
    log(f"Loaded {len(all_trades)} total trade records")

    # ML model retraining check
    try:
        from ml_scorer import retrain_if_needed
        retrain_result = retrain_if_needed()
        if retrain_result:
            log(f"ML model retrained: AUC={retrain_result.get('cv_auc', 0):.3f}, "
                f"samples={retrain_result.get('n_samples', 0)}")
        else:
            log("ML model retraining: not needed (model is current)")
    except ImportError:
        log("ML scorer not available (install xgboost + scikit-learn)")
    except Exception as e:
        log(f"ML model retraining error: {e}")

    # Count closed trades across all strategies
    closed_counts = count_closed_trades_by_strategy(all_trades)
    total_closed = sum(closed_counts.values())
    log(f"Closed trades by strategy: {dict(closed_counts)}")
    log(f"Total closed trades: {total_closed}")

    actions_taken = 0

    # --- Step 1: Evaluate any active experiments that are ready ---
    log("-" * 40)
    log("Step 1: Check active experiments for evaluation")

    active = experiments.get("active_experiments", [])
    if not active:
        log("  No active experiments running")
    else:
        for exp in list(active):  # Copy list since we may modify it
            ready, reason = experiment_ready_to_evaluate(exp, all_trades)
            if ready:
                log(f"  Experiment {exp['id']} ready: {reason}")
                evaluate_experiment(exp, experiments, strategy_params, all_trades)
                actions_taken += 1
            else:
                log(f"  Experiment {exp['id']} ({exp['strategy']}.{exp['parameter']}): {reason}")

    # --- Step 2: Launch new experiments in open slots ---
    log("-" * 40)
    log("Step 2: Launch experiments in open slots")

    active_sections = get_active_slot_sections(experiments)
    launched = 0

    for section, strategies in EXPERIMENT_SLOTS.items():
        if section in active_sections:
            log(f"  Slot [{section}]: already has active experiment")
            continue

        if not slot_cooldown_ok(experiments, section):
            log(f"  Slot [{section}]: cooling down")
            continue

        log(f"  Slot [{section}]: open, looking for candidates...")
        exp = launch_experiment_for_slot(section, experiments, strategy_params, all_trades)
        if exp:
            launched += 1
            actions_taken += 1

    log(f"  Launched {launched} new experiments")

    # --- Step 3: Update engine status and save ---
    log("-" * 40)
    active_count = len(experiments.get("active_experiments", []))
    if active_count > 0:
        active_descs = [
            f"{e['strategy']}.{e['parameter']}={e['test_value']}"
            for e in experiments.get("active_experiments", [])
        ]
        experiments["engine_status"] = "RUNNING_EXPERIMENTS"
        experiments["engine_status_reason"] = f"{active_count} active: {', '.join(active_descs)}"
    else:
        experiments["engine_status"] = "IDLE"
        experiments["engine_status_reason"] = "No active experiments"
    experiments["engine_status_updated"] = iso_now()

    save_experiments(experiments)
    check_exit_conditions(experiments)

    # --- Step 4: Analyze blocked trade filter accuracy ---
    log("-" * 40)
    log("Step 4: Blocked-trade filter accuracy analysis")
    filter_acc = analyze_blocked_trades(all_trades)
    if filter_acc:
        filter_acc_output = {
            "last_updated": iso_now(),
            "filters": filter_acc,
        }
        atomic_write_json(str(STATE_DIR / "filter_accuracy.json"), filter_acc_output)
        log(f"  Filter accuracy analysis: {len(filter_acc)} filters tracked")
        for f, stats in filter_acc.items():
            acc_str = f"{stats['accuracy']:.0%}" if stats['accuracy'] is not None else "N/A"
            log(f"    {f}: {stats['blocked']} blocked, accuracy={acc_str}")
    else:
        log("  No blocked trades found for analysis")

    # --- Step 5: Auto-adjust signal weights from performance tracker ---
    log("-" * 40)
    log("Step 5: Apply signal weight recommendations")
    weight_adjustments = apply_weight_recommendations(experiments)
    log(f"  {weight_adjustments} weight adjustments applied")

    # Summary
    stats = experiments["statistics"]
    log(f"Summary: {active_count} active, "
        f"{stats.get('total_experiments', 0)} total, "
        f"{stats.get('accepted', 0)} accepted, "
        f"{stats.get('rejected', 0)} rejected, "
        f"rate={stats.get('acceptance_rate', 0):.0%}")
    log("=" * 60)


if __name__ == "__main__":
    main()
