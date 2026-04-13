# Self-Improvement Engine v2 — Accelerated Karpathy Loop

## Overview
Optimization engine that runs parallel experiments across independent strategy groups, evaluates using adaptive composite scoring, and triggers evaluations based on trade count rather than just calendar time. This is a Karpathy loop on steroids: multiple simultaneous experiments, data-driven triggers, and metrics that work even with small sample sizes.

## Schedule
- Trigger: Every 3 days (or on demand via `python scripts/self_improvement.py`)
- Evaluation triggers: 8+ new closed trades in the slot, OR 7 days elapsed (whichever first)
- Minimum eval wait: 2 days (prevent noise-driven decisions)
- Cooldown per slot: 12 hours between experiments in the same config section

## Key Changes from v1
1. **Parallel experiments**: One experiment per config section (up to 4 simultaneous)
2. **Composite scoring**: Win rate (small samples) → Profit factor (medium) → Sharpe (large)
3. **Trade-count triggers**: Evaluate when enough closed trades exist, not just calendar
4. **Active-only targeting**: Only experiment on strategies that have recent trades
5. **Faster iteration**: 3-day cycles, 12h cooldown, not weekly

## Experiment Slots (Parallel)
| Slot | Strategies | Independent? |
|------|-----------|-------------|
| trailing_stop | TRAILING_STOP | Yes |
| wheel | WHEEL | Yes |
| politician_copy | POLITICIAN_COPY | Yes |
| crypto_strategy | CRYPTO_DCA, CRYPTO_TREND, CRYPTO_MEAN_REVERSION | Shared params |

Each slot can run one experiment at a time. Different slots run in parallel.

## Composite Scoring
| Closed Trades | Metric | Acceptance Threshold | Why |
|--------------|--------|---------------------|-----|
| < 5 | INSUFFICIENT | N/A (auto-reject) | Too noisy to measure |
| 5-14 | WIN_RATE + PnL bonus | +3% win rate | Stable with few data points |
| 15-49 | PROFIT_FACTOR | +0.15 PF | Captures magnitude, not just direction |
| 50+ | SHARPE | +0.1 Sharpe | Statistically meaningful |

## Main Loop (runs every invocation)

### Step 1: Evaluate Active Experiments
For each running experiment:
1. Check if ready to evaluate:
   - 8+ new closed trades in the slot since start? → Evaluate
   - 7+ days elapsed? → Evaluate (with whatever data exists)
   - < 2 days elapsed? → Skip (too early)
2. Score experiment period using composite_score()
3. Compare to baseline score using the appropriate threshold
4. Accept (keep param) or Reject (revert param)
5. Move to completed_experiments, update stats

### Step 2: Launch New Experiments
For each open slot (no active experiment, cooldown elapsed):
1. Find active strategies in slot (3+ trades in last 14 days)
2. Score each active strategy — target the weakest
3. Select parameter using weighted random (prefer untested)
4. Apply directional bias from past experiments
5. Mutate, clamp to bounds, deploy to strategy_params.json
6. Record in active_experiments with baseline score and trade count

### Step 3: Status Update
- Update engine_status with active experiment count and descriptions
- Check exit conditions (consecutive rejections, acceptance rate)
- Save all state

## Constraints
1. Parameters never exceed bounds
2. max_param_change_pct (20%) enforced per mutation
3. One experiment per config section at a time
4. Cooldown: 12 hours between experiments in same slot
5. Rejected experiments are always reverted
6. Every change is git-committed

## Exit Conditions
- 5 consecutive rejections: log warning (no auto-pause in v2)
- Acceptance rate < 15%: suggest larger mutations
- Acceptance rate > 75%: system is learning fast

## experiments.json Schema (v2)
```json
{
  "schema_version": "2.0.0",
  "active_experiments": [
    {
      "id": "exp_20260413_001",
      "strategy": "TRAILING_STOP",
      "parameter": "loss_pct",
      "config_section": "trailing_stop",
      "original_value": 5.0,
      "test_value": 5.46,
      "baseline_score": 0.55,
      "baseline_method": "WIN_RATE",
      "trades_at_start": 5,
      "status": "RUNNING",
      "started_at": "2026-04-13T..."
    }
  ],
  "completed_experiments": [],
  "statistics": { "total_experiments": 1, "accepted": 0, "rejected": 0, "acceptance_rate": 0 }
}
```
