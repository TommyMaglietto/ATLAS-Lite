# ATLAS Lite V2 Architecture Plan
## "Sealed Loop" — Making the AI Agent Truly Self-Improving

Generated: 2026-04-13
Status: PLANNING (not yet implemented)

---

## The Problem

ATLAS Lite v1.x can tune numeric knobs within fixed bounds. It cannot:
- Isolate signal-level performance within a shared strategy
- Create, disable, or restructure signals autonomously
- Adapt to structural market regime changes (not just parameter drift)
- Learn from blocked trades (filter impact is invisible to scoring)

The loop is "sealed" at the parameter level but **open** at the strategy level.

---

## Phase 1: Per-Signal Parameter Isolation (Week 1)

### What
Split shared `crypto_strategy` config into per-signal parameter namespaces so mutations to one signal don't silently degrade another.

### Architecture

```
# BEFORE (v1): shared params
crypto_strategy:
  rsi_oversold: 38          # shared by MR, Trend, DCA
  ema_fast: 9               # shared by all

# AFTER (v2): per-signal namespaces
crypto_strategy:
  shared:                    # truly shared (BB period, ATR period)
    bb_period: 20
    atr_period: 14
  signal.mean_reversion_oversold:
    rsi_oversold: 38
    bb_std: 2.0
  signal.ema_crossover_bullish:
    rsi_oversold: 35         # can diverge from MR's value
    ema_fast: 9
    ema_med: 21
    ema_long: 55
  signal.volume_engulfing_bullish:
    engulf_vol_multiplier: 1.5
    vwap_period: 24
  signal.body_momentum_bullish:
    body_momentum_period: 5
```

### How It Works
1. `load_params()` returns a nested dict: `params["signal"]["ema_crossover_bullish"]["ema_fast"]`
2. `generate_signals()` reads per-signal params when generating each signal type
3. `self_improvement.py` STRATEGY_PARAM_MAP maps to signal namespaces, not strategy
4. Experiment evaluation scores only the TARGET signal's trades, not the whole strategy
5. Mutations to one signal's `rsi_oversold` do NOT affect other signals

### Migration
- Write a `_migrate_v1_to_v2()` in strategy_params.json that clones shared values into each signal namespace
- `load_params()` supports both formats during transition
- Old experiments in experiments.json get mapped to the closest signal namespace

### Impact on Self-Improvement
- The engine can now run 6+ parallel experiments (one per signal type) instead of 1 per strategy
- Evaluation is precise: mutating engulf_vol_multiplier only measures volume_engulfing_bullish trades
- No more cross-contamination

---

## Phase 2: Globals Refactor — Params-as-Argument (Week 1)

### What
Refactor `compute_indicators()` and `generate_signals()` from reading module-level globals to accepting a `params` dict argument.

### Why
- Eliminates the "globals set before main()" dependency
- Makes functions unit-testable with arbitrary params
- Required for Phase 1 (per-signal params need to be passed, not global)

### How
```python
# BEFORE
def generate_signals(df, symbol):
    if rsi < RSI_OVERSOLD:  # module global
        ...

# AFTER
def generate_signals(df, symbol, params):
    signal_params = params["signal"].get("mean_reversion_oversold", params.get("shared", {}))
    if rsi < signal_params.get("rsi_oversold", 35):
        ...
```

### Scope
- `compute_indicators(df)` -> `compute_indicators(df, params)` — ~15 globals to convert
- `generate_signals(df, symbol)` -> `generate_signals(df, symbol, params)` — ~20 globals to convert
- Remove all `global` declarations from `main()` except watchlist
- All callers updated (main loop, 4H signal generation)

---

## Phase 3: Resilience Hardening (Week 2)

### 3A: API Retry with Exponential Backoff

```python
# New utility in atomic_write.py or new resilience.py
import functools, time

def retry_api(max_retries=3, base_delay=1.0, max_delay=30.0):
    """Decorator for Alpaca API calls with exponential backoff."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    print(f"  API retry {attempt+1}/{max_retries} after {delay}s: {e}")
                    time.sleep(delay)
        return wrapper
    return decorator
```

Apply to: `place_crypto_buy()`, `place_crypto_sell()`, `trading_client.get_account()`, `trading_client.get_all_positions()`, all data client calls.

### 3B: Socket Timeouts

```python
# Configure on client init in both crypto_strategy.py and trailing_stop_monitor.py
from httpx import Timeout
timeout = Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
trading_client._http_client._timeout = timeout
```

### 3C: Spread Validation Before Market Orders

```python
def validate_spread(data_client, symbol, max_spread_pct=0.5):
    """Check bid-ask spread before placing market order."""
    quote = data_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(
        symbol_or_symbols=[symbol]))
    q = quote[symbol]
    spread_pct = (float(q.ask_price) - float(q.bid_price)) / float(q.bid_price) * 100
    if spread_pct > max_spread_pct:
        return False, spread_pct
    return True, spread_pct
```

### 3D: PID Lock for Overlap Prevention

```python
# At script startup
import sys
PIDFILE = PROJECT_ROOT / "state" / f"{Path(__file__).stem}.pid"

def acquire_singleton():
    if PIDFILE.exists():
        old_pid = int(PIDFILE.read_text().strip())
        try:
            os.kill(old_pid, 0)  # Check if process alive
            print(f"Another instance (PID {old_pid}) already running. Exiting.")
            sys.exit(0)
        except OSError:
            pass  # Stale PID file
    PIDFILE.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: PIDFILE.unlink(missing_ok=True))
```

### 3E: Disk-Full Error Handling

Wrap all `locked_read_modify_write` calls in crypto_strategy.py and trailing_stop_monitor.py:

```python
try:
    locked_read_modify_write(str(TRAILING_STOPS_FILE), _modify_stops)
except (IOError, OSError) as e:
    print(f"  CRITICAL: State write failed (disk full?): {e}")
    log_trade({"action": "STATE_WRITE_FAILURE", "reason": str(e), ...})
```

### 3F: Minimum Quantity Validation

```python
# Before sell orders
MIN_QTY = {"crypto": 0.001, "us_equity": 1}
if sell_qty < MIN_QTY.get(asset_class, 0.001):
    print(f"  SKIPPED: {symbol} sell qty {sell_qty} below minimum")
    continue
```

---

## Phase 4: Adaptive Strategy Weighting (Week 3)

### What
Instead of binary enable/disable (F-grade gate), implement a continuous signal weight system that the self-improvement engine can tune.

### Architecture

```json
// state/signal_weights.json
{
  "ema_crossover_bullish": {"weight": 1.0, "auto_tuned": true},
  "mean_reversion_oversold": {"weight": 0.8, "auto_tuned": true},
  "volume_engulfing_bullish": {"weight": 1.2, "auto_tuned": true},
  "body_momentum_bullish": {"weight": 0.5, "auto_tuned": true},
  "borderline_dca": {"weight": 0.3, "auto_tuned": true}
}
```

### How It Works
1. Each signal generates a raw score (STRONG=3, MODERATE=2, WEAK=1)
2. Weighted score = raw_score * signal_weight
3. The de-duplication picks highest WEIGHTED score per symbol
4. Position sizing scales with weight: `size = base_size * weight`
5. Self-improvement tunes weights as continuous params (bounds: 0.0 to 2.0)
6. Weight < 0.1 = effectively disabled (replaces binary F-grade gate)
7. Weight > 1.5 = high-conviction signal, gets larger position sizes

### Why This Matters
This creates a GRADIENT for the self-improvement engine. Instead of:
- "Is volume_engulfing good or bad?" (binary)

It learns:
- "volume_engulfing deserves 1.3x sizing because it has 68% win rate"
- "borderline_dca deserves 0.2x sizing because it's marginal but generates data"

---

## Phase 5: Blocked-Trade Intelligence (Week 3)

### What
Turn blocked-trade logs into actionable intelligence for filter threshold optimization.

### Architecture

```python
# New function in self_improvement.py
def analyze_blocked_trades(trades, since=None):
    """Compute filter efficiency: how many blocks were correct vs missed opportunities."""
    blocked = [t for t in trades if t.get("action") == "BLOCKED" and (not since or t["timestamp"] >= since)]

    by_filter = defaultdict(lambda: {"blocked": 0, "would_have_won": 0, "would_have_lost": 0})

    for b in blocked:
        filter_name = b.get("filter", "unknown")
        symbol = b.get("symbol")
        block_time = b.get("timestamp")
        by_filter[filter_name]["blocked"] += 1

        # Look ahead: what happened to the price after the block?
        # Use 1H bars to check if price went up (missed opportunity) or down (correct block)
        future_return = compute_future_return(symbol, block_time, horizon_hours=24)
        if future_return > 0.02:  # Would have been 2%+ profitable
            by_filter[filter_name]["would_have_won"] += 1
        elif future_return < -0.02:  # Would have lost 2%+
            by_filter[filter_name]["would_have_lost"] += 1

    # Compute filter accuracy
    for f, stats in by_filter.items():
        total = stats["would_have_won"] + stats["would_have_lost"]
        if total > 0:
            stats["accuracy"] = stats["would_have_lost"] / total  # % of blocks that were correct
        else:
            stats["accuracy"] = None

    return dict(by_filter)
```

### How Self-Improvement Uses This
- If `vwap_slope_chop` filter accuracy < 50%: loosen threshold (too many false blocks)
- If `vwap_slope_chop` filter accuracy > 80%: tighten threshold (catching bad trades well)
- If `adr_exhaustion` blocks 30 trades and accuracy is 90%: keep tight, it's saving money
- This gives the engine DIRECTIONAL intelligence about filter thresholds

---

## Phase 6: Slippage Model (Week 4)

### What
Replace hardcoded 0.1% slippage with a per-asset adaptive model built from actual fill data.

### Architecture

```python
# state/slippage_model.json
{
  "BTC/USD": {"avg_slippage_pct": 0.03, "samples": 45, "last_updated": "..."},
  "DOGE/USD": {"avg_slippage_pct": 0.28, "samples": 12, "last_updated": "..."},
  "SOL/USD": {"avg_slippage_pct": 0.08, "samples": 23, "last_updated": "..."}
}
```

### How It Works
1. After every fill, compute `actual_slippage = |fill_price - quote_midpoint| / quote_midpoint * 100`
2. Update exponential moving average per symbol
3. Use the model in PnL calculations instead of hardcoded 0.001
4. Self-improvement uses accurate PnL -> better scoring -> better parameter decisions

---

## Execution Order

| Phase | Week | Effort | Impact |
|-------|------|--------|--------|
| Phase 2: Globals refactor | 1 | Medium | Prerequisite for Phase 1 |
| Phase 1: Per-signal params | 1 | High | Eliminates cross-contamination |
| Phase 3: Resilience | 2 | Medium | Prevents data loss and hangs |
| Phase 4: Adaptive weights | 3 | Medium | Continuous optimization gradient |
| Phase 5: Blocked-trade intel | 3 | Low | Filter threshold optimization |
| Phase 6: Slippage model | 4 | Low | Accurate PnL for scoring |

---

## Success Criteria

After all 6 phases:
1. Self-improvement runs 6+ parallel experiments (one per signal) vs 1 per strategy
2. Filter thresholds optimize from blocked-trade accuracy data
3. Signal weights create a continuous performance gradient
4. No cross-contamination between signals sharing a config section
5. Zero silent data loss from races, corruption, or API failures
6. Actual slippage in PnL calculations within 5bps of reality
7. System survives 100/100 adversarial scenarios (currently 84/100)

## What This Does NOT Cover (Possible Future Discussion)

- Offloading analysis to other LLMs (considered but deferred)
- Multi-timeframe signal fusion (combining 1H + 4H + 1D signals)
- Options wheel integration with crypto signals
- Cross-asset correlation signals (BTC strength -> altcoin rotation)
- Regime-adaptive signal weights (different weights for RISK_ON vs CAUTIOUS)
