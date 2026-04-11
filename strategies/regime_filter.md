# Market Regime Filter — The Gate

## Purpose
This is the FIRST thing that runs before ANY strategy executes. It classifies the current market environment as RISK_ON, CAUTIOUS, or RISK_OFF, and adjusts how all other strategies behave. No strategy should open new positions without consulting the regime filter first.

## When This Runs
- Every 30 minutes during market hours (9:30 AM - 4:00 PM ET)
- Results cached in state/regime.json
- Other strategies read regime.json before executing

## Data Sources (All Available via Alpaca MCP)
We use 5 cross-asset signals that are all tradeable ETFs available through Alpaca:

| Signal | ETFs Used | What It Measures |
|--------|-----------|-----------------|
| Trend | SPY 50-day vs 200-day SMA | Bull or bear market structure |
| Volatility | VIXY (VIX proxy ETF) price level | Fear/complacency in the market |
| Breadth | RSP vs SPY ratio (equal-weight vs cap-weight) | Is the rally broad or narrow? |
| Credit | HYG vs LQD ratio (high-yield vs investment-grade bonds) | Credit stress in the economy |
| Momentum | IWM vs SPY ratio (small-cap vs large-cap) | Risk appetite of investors |

## Scoring System

### Step 1: Calculate Each Signal (scored -1 to +1)

**Trend Score:**
1. Get SPY bars for last 200 days: `get_stock_bars("SPY", timeframe="1Day", limit=200)`
2. Calculate 50-day SMA and 200-day SMA
3. If 50 SMA > 200 SMA (golden cross): +1
4. If 50 SMA < 200 SMA (death cross): -1
5. If within 1% of each other: 0 (transitional)

**Volatility Score:**
1. Get VIXY current price: `get_stock_latest_quote("VIXY")`
2. Get VIXY 20-day average: `get_stock_bars("VIXY", timeframe="1Day", limit=20)`
3. If VIXY below 20-day average and falling: +1 (low fear = risk on)
4. If VIXY above 20-day average and rising: -1 (high fear = risk off)
5. If VIXY near average: 0

**Breadth Score:**
1. Get RSP price and SPY price (last 20 days)
2. Calculate RSP/SPY ratio today vs 20 days ago
3. If ratio is rising (equal-weight outperforming): +1 (broad participation = healthy)
4. If ratio is falling (cap-weight outperforming): -1 (narrow rally = fragile)
5. If flat: 0

**Credit Score:**
1. Get HYG and LQD prices (last 20 days)
2. Calculate HYG/LQD ratio trend
3. If ratio rising (high-yield outperforming): +1 (credit markets healthy)
4. If ratio falling (flight to quality): -1 (credit stress)
5. If flat: 0

**Momentum Score:**
1. Get IWM and SPY prices (last 20 days)
2. Calculate IWM/SPY ratio trend
3. If ratio rising (small-caps outperforming): +1 (risk appetite strong)
4. If ratio falling (large-cap safety): -1 (risk aversion)
5. If flat: 0

### Step 2: Calculate Composite Score

```
composite = (trend * 0.30) + (volatility * 0.25) + (breadth * 0.20) + (credit * 0.15) + (momentum * 0.10)
```

Weights reflect importance: trend is most critical (30%), volatility next (25%), breadth for confirmation (20%).

### Step 3: Classify Regime

| Composite Score | Regime | Meaning |
|----------------|--------|---------|
| > +0.3 | RISK_ON | All clear — trade normally, full position sizes |
| -0.3 to +0.3 | CAUTIOUS | Mixed signals — reduce position sizes by 50%, tighten stops |
| < -0.3 | RISK_OFF | Danger — no new positions, tighten all stops to minimum, close weak positions |

## How Each Strategy Uses the Regime

### Trailing Stop Strategy
| Regime | Behavior |
|--------|----------|
| RISK_ON | Normal parameters from strategy_params.json |
| CAUTIOUS | Reduce position size by 50%, tighten trail_pct by 25% |
| RISK_OFF | NO new entries. Tighten all existing stops to minimum trail_pct (2%). Exit any position down more than 3%. |

### Politician Copy Trading
| Regime | Behavior |
|--------|----------|
| RISK_ON | Copy all qualifying trades at normal position size |
| CAUTIOUS | Copy only BUY trades where BOTH parties are buying (bipartisan). Reduce size by 50%. |
| RISK_OFF | PAUSE all new copy trades. Hold existing positions but do NOT add. |

### Options Wheel Strategy
| Regime | Behavior |
|--------|----------|
| RISK_ON | Normal parameters. Target 0.25 delta, 30 DTE. |
| CAUTIOUS | Widen to lower delta (0.20), shorter DTE (14-21 days), require higher score_min (0.7). |
| RISK_OFF | NO new put sales (you don't want to be assigned in a crash). Continue selling covered calls on existing shares only. |

### Self-Improvement Loop
| Regime | Behavior |
|--------|----------|
| RISK_ON | Run experiments normally |
| CAUTIOUS | Run experiments but require higher min_sharpe_improvement (0.2 instead of 0.1) |
| RISK_OFF | PAUSE experiments. Don't optimize during chaos — the data is not representative. |

## State File Format (state/regime.json)
Updated every 30 minutes. Contains current regime, all 5 signal scores, composite score, and history.

## Edge Cases
- Market just opened (not enough intraday data): Use previous day's closing regime
- One signal fails (ETF data unavailable): Score that signal as 0, flag in logs, continue with 4 signals
- Regime changes mid-day: Apply new regime immediately to next strategy execution
- Weekend: Carry Friday's regime forward to Monday open

## Parameters (from strategy_params.json)
| Parameter | Default | Description |
|-----------|---------|-------------|
| regime_risk_on_threshold | 0.3 | Composite above this = RISK_ON |
| regime_risk_off_threshold | -0.3 | Composite below this = RISK_OFF |
| regime_update_interval_min | 30 | How often to recalculate |
| cautious_size_multiplier | 0.5 | Position size multiplier in CAUTIOUS mode |
| risk_off_exit_threshold_pct | -3.0 | Exit positions down more than this % in RISK_OFF |
