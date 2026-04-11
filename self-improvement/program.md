# Self-Improvement Engine — Karpathy Loop Implementation

## Overview
Weekly optimization loop that tests parameter changes, measures their impact on Sharpe ratio over rolling windows, and commits improvements while reverting failures. This is a "Karpathy loop" for algorithmic trading: run experiments, evaluate, learn, repeat.

## Schedule
- Trigger: Every Sunday 8:00 PM ET
- Evaluation period: Rolling 5-day windows
- Minimum data: 30 trades required to begin
- Cool-down: 24 hours between experiments (don't thrash parameters)

## Pre-Execution Checklist
1. Check state/experiments.json — is there a current_experiment in progress?
   - If YES and status != COMPLETED: Skip this run, don't overlap experiments
   - If YES and status == COMPLETED: Move to completed_experiments, clear current_experiment
2. Count total trades in logs/trades.jsonl
   - If total_trades < min_trades_before_commit (30): Log "Not enough data yet", exit gracefully, suggest trying again next week
3. Check time since last_experiment_commit in git log
   - If < cooldown_between_experiments_hours (24): Log "Cooling down", exit gracefully

## Main Loop

### Phase 1: Baseline Measurement
1. Calculate Sharpe ratio for each strategy over last eval_period_days (5 days):
   a. Read logs/trades.jsonl, filter last 5 days
   b. Separate by strategy tag (TRAILING_STOP, WHEEL, POLITICIAN_COPY)
   c. For each strategy:
      - Calculate daily returns: (daily_pnl / account_value)
      - Sharpe = mean_daily_return / std_dev_daily_returns * sqrt(252)
      - If < 0 or no trades: Sharpe = 0
   d. Identify weakest strategy (lowest Sharpe)
2. Log baseline to self-improvement/metrics_history.json
3. Git commit: git add logs/ self-improvement/ && git commit -m "[SELF_IMPROVEMENT] Baseline measurement: weakest={strategy} sharpe={sharpe}"

### Phase 2: Experiment Design
1. Select weakest strategy from Phase 1
2. Available parameters to tweak (by strategy):

   **TRAILING_STOP:**
   - loss_pct: decrease by max_param_change_pct (20%) to reduce early exits, OR increase to be more aggressive
   - trail_pct: decrease to lock in smaller gains faster, OR increase to allow bigger swings

   **WHEEL:**
   - put_delta: increase toward 0.35 to take safer trades (OTM), OR decrease toward 0.20 for more premium
   - call_delta: same as put_delta
   - dte_target: increase to 35-40 for more premium decay time, OR decrease to 25-28 for quicker cycles

   **POLITICIAN_COPY:**
   - max_position_pct: increase to 7-8% to take more conviction bets, OR decrease to 3% for more diversification
   - staleness_threshold_days: extend to 50-60 to trade older filings (more data), OR tighten to 30 for fresher signals

3. Select ONE parameter (randomly or by highest expected impact)
4. Calculate new_value:
   - new_value = current_value * (1 + random_change) where random_change in [-max_param_change_pct/100, +max_param_change_pct/100]
   - Clamp to bounds from strategy_params.json
5. Create experiment record
6. Write to state/experiments.json as current_experiment (atomic write)
7. Git commit: git add config/ state/ && git commit -m "[SELF_IMPROVEMENT] Experiment {id}: testing {param}={value}"

### Phase 3: Deploy Experiment
1. Update strategy_params.json with test_value (atomic write)
2. Set experiment status to RUNNING in state/experiments.json
3. Log: "Experiment deployed, monitoring for {eval_period_days} days"
4. Git commit: git add config/ && git commit -m "[EXPERIMENT_DEPLOY] {param}={value} active"
5. **Wait** until next Sunday (7 days later) — experiment runs for eval_period_days

### Phase 4: Evaluate Results (Following Sunday)
This phase runs ONE WEEK after Phase 3.

1. Calculate new Sharpe for the tested strategy using logs from last eval_period_days (5-day rolling window)
2. Compare: new_sharpe vs baseline_sharpe
3. Decision:
   ```
   if (new_sharpe - baseline_sharpe) >= min_sharpe_improvement (0.1):
     status = ACCEPTED
   else:
     status = REJECTED
   ```
4. Update experiment record with results

### Phase 5: Commit or Revert
1. If ACCEPTED:
   - Keep strategy_params.json at test_value
   - Move experiment to completed_experiments array
   - Update statistics: accepted += 1, acceptance_rate = accepted / (accepted + rejected)
   - Git commit: git add config/ state/ && git commit -m "[EXPERIMENT_ACCEPTED] {param}={value} is new baseline, sharpe improved {improvement}"

2. If REJECTED:
   - Revert strategy_params.json to original_value
   - Move experiment to completed_experiments array
   - Update statistics: rejected += 1, acceptance_rate = accepted / (accepted + rejected)
   - Git commit: git add config/ state/ && git commit -m "[EXPERIMENT_REJECTED] Reverted {param} to {original}, sharpe diff {diff}"

## Constraints (Never Violated)
1. Parameters never exceed bounds from strategy_params.json
2. max_param_change_pct (20%) enforced
3. Cash reserve always >= 20%
4. Cooldown enforced: no two experiments within 24 hours
5. One experiment at a time: can't have overlapping tests
6. Always revert failed experiments: git will show clean history

## Exit Conditions
- If acceptance_rate < 20%: Increase max_param_change_pct (too conservative)
- If acceptance_rate > 80%: Decrease max_param_change_pct (too aggressive)
- If Sharpe ratio of best strategy > 2.0: Consider strategy mature
- If consecutive 3 rejections: Pause self-improvement, notify user

## Daily Summary Task (4:15 PM ET)
Every trading day at 4:15 PM, generate a summary:
1. Today's P&L by strategy
2. Current positions and their unrealized P&L
3. Any open experiments and their status
4. Number of trades executed today
5. Any warnings or anomalies
Save to logs/daily_summaries/{YYYY-MM-DD}.json
