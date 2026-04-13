#!/usr/bin/env python3
"""
ATLAS Lite Self-Improvement Engine -- Karpathy Loop

Runs weekly (Sunday 8PM ET). Implements a 5-phase optimization loop:
  1. Pre-checks (enough data, no overlap, cooldown)
  2. Baseline measurement (Sharpe per strategy, identify weakest)
  3. Experiment design (mutate one param of weakest strategy)
  4. Deploy (update strategy_params.json, mark RUNNING)
  5. Evaluate (on next invocation after eval_period_days, accept or reject)

Every phase that changes state is git-committed.
"""

import json
import random
import subprocess
import sys
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
# Mapping: strategy name -> (section in strategy_params.json, list of tunable params)
# Each tunable param must have a corresponding <param>_bounds key in the config.
# ---------------------------------------------------------------------------
STRATEGY_PARAM_MAP = {
    "TRAILING_STOP": {
        "section": "trailing_stop",
        "params": ["loss_pct", "trail_pct"],
    },
    "WHEEL": {
        "section": "wheel",
        "params": ["put_delta", "call_delta", "dte_target"],
    },
    "POLITICIAN_COPY": {
        "section": "politician_copy",
        "params": ["max_position_pct", "staleness_threshold_days"],
    },
    "CRYPTO_DCA": {
        "section": "crypto_strategy",
        "params": [
            "dca_rsi_threshold",
            "rsi_oversold",
            "rsi_overbought",
            "bb_std",
            "ema_fast",
            "ema_med",
            "ema_long",
        ],
    },
    "CRYPTO_TREND": {
        "section": "crypto_strategy",
        "params": [
            "rsi_oversold",
            "rsi_overbought",
            "bb_std",
            "ema_fast",
            "ema_med",
            "ema_long",
        ],
    },
    "CRYPTO_MEAN_REVERSION": {
        "section": "crypto_strategy",
        "params": [
            "rsi_oversold",
            "rsi_overbought",
            "bb_std",
            "ema_fast",
            "ema_med",
            "ema_long",
        ],
    },
}

# Fallback bounds for params that lack explicit _bounds in the config.
# Only used when strategy_params.json does not have a <param>_bounds field.
DEFAULT_BOUNDS = {
    "loss_pct": (3.0, 15.0),
    "trail_pct": (2.0, 10.0),
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
    "ema_long": (40, 70),
}


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
    """Stage relevant dirs and commit."""
    subprocess.run(
        ["git", "add", "config/", "state/", "self-improvement/", "logs/"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "commit", "-m", f"[SELF_IMPROVEMENT] {message}"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log(f"Git commit: {message}")
    else:
        # Nothing to commit is fine (no changes)
        log(f"Git commit skipped (no changes or error): {result.stderr.strip()}")


def load_experiments():
    data = atomic_read_json(str(EXPERIMENTS_FILE))
    if data is None:
        return {
            "schema_version": "1.0.0",
            "current_experiment": None,
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
    return data


def save_experiments(data):
    atomic_write_json(str(EXPERIMENTS_FILE), data)


def load_strategy_params():
    return atomic_read_json(str(STRATEGY_PARAMS_FILE))


def save_strategy_params(data):
    atomic_write_json(str(STRATEGY_PARAMS_FILE), data)


def load_metrics_history():
    data = atomic_read_json(str(METRICS_HISTORY_FILE))
    if data is None:
        return {
            "schema_version": "1.0.0",
            "daily_metrics": [],
            "experiment_metrics": [],
        }
    return data


def save_metrics_history(data):
    atomic_write_json(str(METRICS_HISTORY_FILE), data)


def get_bounds(params_section, param_name):
    """Get (min, max) bounds for a parameter from the config, falling back to defaults."""
    bounds_key = f"{param_name}_bounds"
    if bounds_key in params_section:
        b = params_section[bounds_key]
        return (b[0], b[1])
    if param_name in DEFAULT_BOUNDS:
        return DEFAULT_BOUNDS[param_name]
    # Last resort: allow +/- 50% of current value
    current = params_section.get(param_name, 1)
    return (current * 0.5, current * 1.5)


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def count_trades():
    """Count actionable trade lines in trades.jsonl (entries with a strategy and an order action)."""
    count = 0
    try:
        with open(str(TRADES_FILE), "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Count entries that represent actual trades (have a strategy field)
                if entry.get("strategy"):
                    count += 1
    except FileNotFoundError:
        pass
    return count


def last_experiment_end_time(experiments):
    """Return the most recent completed experiment's end time, or None."""
    completed = experiments.get("completed_experiments", [])
    if not completed:
        return None
    latest = max(completed, key=lambda e: e.get("completed_at", "2000-01-01"))
    ts = latest.get("completed_at")
    if ts:
        return datetime.fromisoformat(ts)
    return None


def generate_experiment_id():
    now = now_utc()
    return f"exp_{now.strftime('%Y%m%d')}_{random.randint(1, 999):03d}"


# ---------------------------------------------------------------------------
# Phase 1: Pre-checks
# ---------------------------------------------------------------------------

def phase_prechecks(experiments, strategy_params):
    """
    Returns (can_proceed: bool, reason: str).
    If a RUNNING experiment exists and is ready for evaluation, returns
    (True, "EVALUATE") so the caller can jump to phase 5.
    """
    current = experiments.get("current_experiment")
    si_params = strategy_params.get("self_improvement", {})
    min_trades = si_params.get("min_trades_before_commit", 30)
    cooldown_hours = si_params.get("cooldown_between_experiments_hours", 24)
    eval_period_days = si_params.get("eval_period_days", 5)

    # --- Check for in-progress experiment ---
    if current is not None:
        status = current.get("status", "")

        if status == "RUNNING":
            # Check if eval period has elapsed
            eval_until = current.get("eval_until")
            if eval_until:
                eval_dt = datetime.fromisoformat(eval_until)
                if eval_dt.tzinfo is None:
                    eval_dt = eval_dt.replace(tzinfo=timezone.utc)
                if now_utc() >= eval_dt:
                    log(f"Experiment {current['id']} ready for evaluation.")
                    return True, "EVALUATE"
                else:
                    remaining = eval_dt - now_utc()
                    log(f"Experiment {current['id']} still running. "
                        f"Eval in {remaining.days}d {remaining.seconds // 3600}h.")
                    return False, f"Experiment still running, eval at {eval_until}"
            else:
                log("Experiment is RUNNING but has no eval_until. Skipping.")
                return False, "Experiment RUNNING with no eval deadline"

        elif status == "COMPLETED":
            # Should have been moved already, but handle gracefully
            log("Found COMPLETED experiment in current_experiment. Cleaning up.")
            _move_to_completed(experiments, current)
            # Fall through to start new experiment

        else:
            log(f"Experiment {current.get('id', '?')} has status={status}. Skipping.")
            return False, f"Experiment in unexpected state: {status}"

    # --- Minimum trades check ---
    trade_count = count_trades()
    if trade_count < min_trades:
        log(f"Not enough trades: {trade_count}/{min_trades}. Waiting for more data.")
        experiments["engine_status"] = "WAITING_FOR_DATA"
        experiments["engine_status_reason"] = (
            f"{trade_count} trades logged, need {min_trades}+ before experiments can begin"
        )
        experiments["engine_status_updated"] = iso_now()
        save_experiments(experiments)
        return False, f"Only {trade_count} trades, need {min_trades}"

    # --- Cooldown check ---
    last_end = last_experiment_end_time(experiments)
    if last_end is not None:
        if last_end.tzinfo is None:
            last_end = last_end.replace(tzinfo=timezone.utc)
        elapsed = now_utc() - last_end
        if elapsed < timedelta(hours=cooldown_hours):
            remaining = timedelta(hours=cooldown_hours) - elapsed
            log(f"Cooldown active. {remaining.seconds // 3600}h remaining.")
            return False, f"Cooldown: {remaining}"

    return True, "NEW_EXPERIMENT"


def _move_to_completed(experiments, experiment):
    """Move a current_experiment into completed_experiments."""
    experiments["completed_experiments"].append(experiment)
    experiments["current_experiment"] = None


# ---------------------------------------------------------------------------
# Phase 2: Baseline Measurement
# ---------------------------------------------------------------------------

def phase_baseline(experiments, strategy_params):
    """
    Calculate per-strategy Sharpe, identify weakest, log to metrics_history.
    Returns (strategy_metrics: dict, weakest_strategy: str).
    """
    eval_days = strategy_params.get("self_improvement", {}).get("eval_period_days", 5)
    trades = load_trades(str(TRADES_FILE))

    # Try enhanced metrics first, fall back to basic
    try:
        from metrics import calculate_enhanced_metrics_by_strategy
        strategy_metrics = calculate_enhanced_metrics_by_strategy(trades, days=eval_days)
        log("Using enhanced metrics (realized + unrealized P&L)")
    except (ImportError, AttributeError, Exception) as e:
        strategy_metrics = calculate_metrics_by_strategy(trades, days=eval_days)
        log(f"Using basic metrics (realized only): {e}")

    if not strategy_metrics:
        log("No strategy metrics available (no recent trades by strategy).")
        return {}, None

    log("Baseline metrics:")
    for strat, m in strategy_metrics.items():
        log(f"  {strat}: sharpe={m['sharpe']}, pnl={m['total_pnl']}, trades={m['trade_count']}")

    # Filter to only strategies with tunable parameters
    tunable_strategies = {s: m for s, m in strategy_metrics.items() if s in STRATEGY_PARAM_MAP}
    if not tunable_strategies:
        log("No tunable strategies found in recent trades. Cannot run experiment.")
        return strategy_metrics, None

    # Identify weakest: lowest Sharpe, break ties by most negative PnL
    weakest = min(
        tunable_strategies.keys(),
        key=lambda s: (tunable_strategies[s]["sharpe"], tunable_strategies[s]["total_pnl"]),
    )
    log(f"Weakest strategy: {weakest} (sharpe={strategy_metrics[weakest]['sharpe']})")

    # Log to metrics history
    exp_id = generate_experiment_id()
    history = load_metrics_history()
    history["experiment_metrics"].append({
        "timestamp": iso_now(),
        "type": "baseline",
        "strategies": {
            s: {
                "sharpe": m["sharpe"],
                "trade_count": m["trade_count"],
                "total_pnl": m["total_pnl"],
            }
            for s, m in strategy_metrics.items()
        },
        "weakest": weakest,
        "experiment_id": exp_id,
    })
    save_metrics_history(history)

    git_commit(
        f"Baseline measurement: weakest={weakest} "
        f"sharpe={strategy_metrics[weakest]['sharpe']}"
    )

    return strategy_metrics, weakest, exp_id


# ---------------------------------------------------------------------------
# Directional learning helpers (used by Phase 3)
# ---------------------------------------------------------------------------

def get_mutation_direction(param_name, strategy, completed_experiments):
    """
    Learn from past experiments which direction to push a parameter.
    Returns a bias factor: positive means increase worked, negative means decrease worked.

    Logic:
    - Look at all completed experiments for this param+strategy combo
    - If increasing was ACCEPTED: bias toward increase
    - If decreasing was ACCEPTED: bias toward decrease
    - If both rejected: try the direction not yet tried, or use larger mutation
    - If no history: return 0 (no bias, fully random)

    Returns: float in [-1, 1] where:
        +1 = strongly bias toward increase
        -1 = strongly bias toward decrease
         0 = no bias (random)
    """
    history = []
    for exp in completed_experiments:
        if exp.get("parameter") == param_name and exp.get("strategy") == strategy:
            direction = "increase" if exp.get("test_value", 0) > exp.get("original_value", 0) else "decrease"
            outcome = exp.get("status")  # ACCEPTED or REJECTED
            history.append({"direction": direction, "outcome": outcome,
                          "sharpe_improvement": exp.get("sharpe_improvement", 0)})

    if not history:
        return 0.0  # No data, fully random

    # Score each direction
    increase_score = 0
    decrease_score = 0
    for h in history:
        weight = 1.0
        if h["direction"] == "increase":
            if h["outcome"] == "ACCEPTED":
                increase_score += weight
            else:
                increase_score -= weight * 0.5  # Penalize less than we reward
        else:
            if h["outcome"] == "ACCEPTED":
                decrease_score += weight
            else:
                decrease_score -= weight * 0.5

    total = abs(increase_score) + abs(decrease_score)
    if total == 0:
        return 0.0

    # Normalize to [-1, 1]
    bias = (increase_score - decrease_score) / total
    return round(bias, 3)


def select_parameter(valid_params, strategy, completed_experiments):
    """
    Select which parameter to mutate. Prefer:
    1. Parameters never tested before (exploration)
    2. Parameters with mixed results (worth retrying with different direction)
    3. Parameters that have been consistently rejected (avoid)
    """
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

    # Score each param: higher = more worth testing
    scored = []
    for p in valid_params:
        if p not in tested_params:
            score = 10  # Never tested -- highest priority (exploration)
        else:
            t = tested_params[p]
            if t["accepted"] > t["rejected"]:
                score = 7  # Previously successful -- worth optimizing further
            elif t["accepted"] > 0:
                score = 5  # Mixed results -- might find the right direction
            elif t["rejected"] <= 2:
                score = 3  # A few rejections -- give it another chance
            else:
                score = 1  # Consistently rejected -- low priority
        scored.append((p, score))

    # Weighted random selection (don't purely pick highest -- maintain exploration)
    total_score = sum(s for _, s in scored)
    r = random.uniform(0, total_score)
    cumulative = 0
    for p, s in scored:
        cumulative += s
        if r <= cumulative:
            return p
    return scored[-1][0]  # fallback


# ---------------------------------------------------------------------------
# Phase 3: Experiment Design
# ---------------------------------------------------------------------------

def phase_design(weakest_strategy, strategy_metrics, strategy_params, experiment_id, experiments):
    """
    Select one parameter from the weakest strategy and mutate it.
    Returns experiment record dict, or None if the strategy has no tunable params.
    """
    mapping = STRATEGY_PARAM_MAP.get(weakest_strategy)
    if mapping is None:
        # Strategy not in our map (e.g. CRYPTO_REBALANCE). Skip.
        log(f"No tunable parameters mapped for strategy {weakest_strategy}. Skipping.")
        return None

    section_key = mapping["section"]
    param_candidates = mapping["params"]
    params_section = strategy_params.get(section_key, {})

    # Filter to params that actually exist in the config section
    valid_params = [p for p in param_candidates if p in params_section]
    if not valid_params:
        log(f"No valid tunable params found in config section '{section_key}'.")
        return None

    # Pick parameter using weighted selection based on experiment history
    completed = experiments.get("completed_experiments", [])
    chosen_param = select_parameter(valid_params, weakest_strategy, completed)
    current_value = params_section[chosen_param]
    bounds = get_bounds(params_section, chosen_param)
    lo, hi = bounds

    max_change_pct = strategy_params.get("self_improvement", {}).get("max_param_change_pct", 20)
    eval_days = strategy_params.get("self_improvement", {}).get("eval_period_days", 5)

    # Get directional bias from experiment history
    bias = get_mutation_direction(chosen_param, weakest_strategy, completed)

    if abs(bias) > 0.3:
        # Strong historical signal -- bias the mutation direction
        if bias > 0:
            # Increase worked before -- random in [0, max_change]
            random_change = random.uniform(0, max_change_pct / 100)
            log(f"  Direction bias: INCREASE (bias={bias:+.3f}, based on past experiments)")
        else:
            # Decrease worked before -- random in [-max_change, 0]
            random_change = random.uniform(-max_change_pct / 100, 0)
            log(f"  Direction bias: DECREASE (bias={bias:+.3f}, based on past experiments)")
    else:
        # No strong signal -- explore randomly
        random_change = random.uniform(-max_change_pct / 100, max_change_pct / 100)
        log(f"  Direction bias: NONE (bias={bias:+.3f}, exploring randomly)")

    new_value = current_value * (1 + random_change)

    # Clamp to bounds
    new_value = clamp(new_value, lo, hi)

    # Round appropriately: integers stay integers, floats get 4 decimal places
    if isinstance(current_value, int):
        new_value = int(round(new_value))
        # Ensure the mutation actually changed something
        if new_value == current_value:
            new_value = current_value + (1 if random_change >= 0 else -1)
            new_value = int(clamp(new_value, lo, hi))
    else:
        new_value = round(new_value, 4)

    baseline_sharpe = strategy_metrics.get(weakest_strategy, {}).get("sharpe", 0.0)
    start_time = now_utc()
    eval_until = start_time + timedelta(days=eval_days)

    experiment = {
        "id": experiment_id,
        "strategy": weakest_strategy,
        "parameter": chosen_param,
        "config_section": section_key,
        "original_value": current_value,
        "test_value": new_value,
        "bounds": list(bounds),
        "baseline_sharpe": baseline_sharpe,
        "mutation_direction": "increase" if new_value > current_value else "decrease",
        "direction_bias": bias,
        "status": "DESIGNED",
        "started_at": start_time.isoformat(),
        "eval_until": eval_until.isoformat(),
    }

    log(f"Experiment designed: {experiment_id}")
    log(f"  Strategy:  {weakest_strategy}")
    log(f"  Parameter: {chosen_param}")
    log(f"  Current:   {current_value}")
    log(f"  Test:      {new_value}")
    log(f"  Bounds:    [{lo}, {hi}]")
    log(f"  Evaluate:  {eval_until.isoformat()}")

    return experiment


# ---------------------------------------------------------------------------
# Phase 4: Deploy
# ---------------------------------------------------------------------------

def phase_deploy(experiment, experiments, strategy_params):
    """
    Update strategy_params.json with the test value and mark RUNNING.
    """
    section_key = experiment["config_section"]
    param_name = experiment["parameter"]
    test_value = experiment["test_value"]

    # Update param in config
    if section_key not in strategy_params:
        log(f"ERROR: config section '{section_key}' not found in strategy_params.json")
        return False

    strategy_params[section_key][param_name] = test_value
    strategy_params["last_updated"] = iso_now()
    save_strategy_params(strategy_params)

    # Update experiment status
    experiment["status"] = "RUNNING"
    experiments["current_experiment"] = experiment
    experiments["engine_status"] = "RUNNING_EXPERIMENT"
    experiments["engine_status_reason"] = (
        f"Testing {param_name}={test_value} (was {experiment['original_value']})"
    )
    experiments["engine_status_updated"] = iso_now()
    experiments["statistics"]["total_experiments"] = (
        experiments["statistics"].get("total_experiments", 0) + 1
    )
    save_experiments(experiments)

    log(f"Deployed: {param_name}={test_value} in {section_key}")
    log(f"Experiment {experiment['id']} is now RUNNING. Eval after {experiment['eval_until']}.")

    git_commit(
        f"Experiment {experiment['id']}: testing {param_name}={test_value} "
        f"(was {experiment['original_value']})"
    )

    return True


# ---------------------------------------------------------------------------
# Phase 5: Evaluate
# ---------------------------------------------------------------------------

def phase_evaluate(experiments, strategy_params):
    """
    Evaluate a RUNNING experiment whose eval period has elapsed.
    Accept if Sharpe improved by >= min_sharpe_improvement, else reject and revert.
    """
    current = experiments.get("current_experiment")
    if current is None or current.get("status") != "RUNNING":
        log("No RUNNING experiment to evaluate.")
        return

    exp_id = current["id"]
    strategy = current["strategy"]
    param_name = current["parameter"]
    section_key = current["config_section"]
    original_value = current["original_value"]
    test_value = current["test_value"]
    baseline_sharpe = current["baseline_sharpe"]
    min_improvement = strategy_params.get("self_improvement", {}).get(
        "min_sharpe_improvement", 0.1
    )
    eval_days = strategy_params.get("self_improvement", {}).get("eval_period_days", 5)

    # Calculate new metrics -- try enhanced first, fall back to basic
    trades = load_trades(str(TRADES_FILE))
    try:
        from metrics import calculate_enhanced_metrics_by_strategy
        strategy_metrics = calculate_enhanced_metrics_by_strategy(trades, days=eval_days)
        log("Using enhanced metrics (realized + unrealized P&L)")
    except (ImportError, AttributeError, Exception) as e:
        strategy_metrics = calculate_metrics_by_strategy(trades, days=eval_days)
        log(f"Using basic metrics (realized only): {e}")
    new_metrics = strategy_metrics.get(strategy, {})
    new_sharpe = new_metrics.get("sharpe", 0.0)
    sharpe_diff = new_sharpe - baseline_sharpe

    log(f"Evaluating experiment {exp_id}:")
    log(f"  Strategy:       {strategy}")
    log(f"  Parameter:      {param_name} = {test_value} (was {original_value})")
    log(f"  Baseline Sharpe: {baseline_sharpe}")
    log(f"  New Sharpe:      {new_sharpe}")
    log(f"  Difference:      {sharpe_diff:+.4f}")
    log(f"  Threshold:       {min_improvement}")

    # Decision
    if sharpe_diff >= min_improvement:
        decision = "ACCEPTED"
        log(f"  ACCEPTED -- Sharpe improved by {sharpe_diff:.4f} (>= {min_improvement})")

        # Keep the test value (already in config)
        current["status"] = "ACCEPTED"
        current["result_sharpe"] = new_sharpe
        current["sharpe_improvement"] = round(sharpe_diff, 4)
        current["completed_at"] = iso_now()

        experiments["statistics"]["accepted"] = (
            experiments["statistics"].get("accepted", 0) + 1
        )
    else:
        decision = "REJECTED"
        log(f"  REJECTED -- Sharpe diff {sharpe_diff:.4f} < threshold {min_improvement}")

        # Revert parameter to original value
        if section_key in strategy_params and param_name in strategy_params[section_key]:
            strategy_params[section_key][param_name] = original_value
            strategy_params["last_updated"] = iso_now()
            save_strategy_params(strategy_params)
            log(f"  Reverted {param_name} to {original_value}")
        else:
            log(f"  WARNING: could not revert, section/param not found in config")

        current["status"] = "REJECTED"
        current["result_sharpe"] = new_sharpe
        current["sharpe_improvement"] = round(sharpe_diff, 4)
        current["completed_at"] = iso_now()

        experiments["statistics"]["rejected"] = (
            experiments["statistics"].get("rejected", 0) + 1
        )

    # Update acceptance rate
    accepted = experiments["statistics"].get("accepted", 0)
    rejected = experiments["statistics"].get("rejected", 0)
    total_decided = accepted + rejected
    experiments["statistics"]["acceptance_rate"] = (
        round(accepted / total_decided, 4) if total_decided > 0 else 0
    )

    # Move to completed
    _move_to_completed(experiments, current)
    experiments["engine_status"] = "IDLE"
    experiments["engine_status_reason"] = (
        f"Last experiment {exp_id} {decision}: sharpe {baseline_sharpe} -> {new_sharpe}"
    )
    experiments["engine_status_updated"] = iso_now()
    save_experiments(experiments)

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
        "baseline_sharpe": baseline_sharpe,
        "result_sharpe": new_sharpe,
        "sharpe_improvement": round(sharpe_diff, 4),
        "decision": decision,
        "strategies": {
            s: {
                "sharpe": m.get("sharpe", 0),
                "trade_count": m.get("trade_count", 0),
                "total_pnl": m.get("total_pnl", 0),
            }
            for s, m in strategy_metrics.items()
        },
    })
    save_metrics_history(history)

    # Check exit conditions
    _check_exit_conditions(experiments)

    # Git commit
    if decision == "ACCEPTED":
        git_commit(
            f"Experiment {exp_id} ACCEPTED: {param_name}={test_value} is new baseline, "
            f"sharpe improved {sharpe_diff:+.4f}"
        )
    else:
        git_commit(
            f"Experiment {exp_id} REJECTED: reverted {param_name} to {original_value}, "
            f"sharpe diff {sharpe_diff:+.4f}"
        )


def _check_exit_conditions(experiments):
    """Check if self-improvement should adjust its aggressiveness or pause."""
    stats = experiments.get("statistics", {})
    acceptance_rate = stats.get("acceptance_rate", 0)
    total = stats.get("accepted", 0) + stats.get("rejected", 0)

    if total < 3:
        return  # Not enough history to judge

    # Check for 3 consecutive rejections
    completed = experiments.get("completed_experiments", [])
    if len(completed) >= 3:
        last_3 = completed[-3:]
        if all(e.get("status") == "REJECTED" for e in last_3):
            log("WARNING: 3 consecutive rejections. Consider pausing self-improvement.")
            experiments["engine_status"] = "PAUSED_CONSECUTIVE_REJECTIONS"
            experiments["engine_status_reason"] = (
                "3 consecutive experiments rejected. Manual review recommended."
            )
            experiments["engine_status_updated"] = iso_now()

    if acceptance_rate < 0.20 and total >= 5:
        log(f"NOTE: Acceptance rate {acceptance_rate:.0%} is low. "
            "Consider increasing max_param_change_pct (changes may be too conservative).")
    elif acceptance_rate > 0.80 and total >= 5:
        log(f"NOTE: Acceptance rate {acceptance_rate:.0%} is high. "
            "Consider decreasing max_param_change_pct (changes may be too aggressive).")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    log("=" * 60)
    log(f"Self-improvement engine starting at {iso_now()}")
    log("=" * 60)

    experiments = load_experiments()
    strategy_params = load_strategy_params()

    if strategy_params is None:
        log("FATAL: Cannot read strategy_params.json. Aborting.")
        sys.exit(1)

    # Check if engine is paused
    if experiments.get("engine_status") == "PAUSED_CONSECUTIVE_REJECTIONS":
        log("Engine is PAUSED due to consecutive rejections. Manual review needed.")
        log("To resume, set engine_status to 'IDLE' in state/experiments.json.")
        return

    # Phase 1: Pre-checks
    log("-" * 40)
    log("Phase 1: Pre-checks")
    can_proceed, reason = phase_prechecks(experiments, strategy_params)

    if not can_proceed:
        log(f"Cannot proceed: {reason}")
        return

    # Branch: evaluate existing experiment or start new one
    if reason == "EVALUATE":
        log("-" * 40)
        log("Phase 5: Evaluate running experiment")
        phase_evaluate(experiments, strategy_params)
        return

    # Phase 2: Baseline
    log("-" * 40)
    log("Phase 2: Baseline measurement")
    result = phase_baseline(experiments, strategy_params)
    if len(result) == 3:
        strategy_metrics, weakest, experiment_id = result
    else:
        log("Baseline measurement failed. Aborting.")
        return

    if weakest is None:
        log("No strategy data available. Aborting.")
        return

    # Phase 3: Design
    log("-" * 40)
    log("Phase 3: Experiment design")
    experiment = phase_design(weakest, strategy_metrics, strategy_params, experiment_id, experiments)
    if experiment is None:
        log("Could not design experiment (no tunable params). Aborting.")
        return

    # Phase 4: Deploy
    log("-" * 40)
    log("Phase 4: Deploy experiment")
    success = phase_deploy(experiment, experiments, strategy_params)
    if not success:
        log("Deployment failed. Aborting.")
        return

    log("-" * 40)
    log(f"Done. Experiment {experiment['id']} deployed and RUNNING.")
    log(f"Next evaluation at: {experiment['eval_until']}")
    log("=" * 60)


if __name__ == "__main__":
    main()
