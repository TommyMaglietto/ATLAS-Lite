#!/usr/bin/env python3
"""
ATLAS Lite -- Market Regime Filter
Calculates 5 cross-asset signals and classifies the market regime
as RISK_ON, CAUTIOUS, or RISK_OFF.

Designed to run every 30 minutes during market hours.
Uses Alpaca StockHistoricalDataClient for ETF bar data.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Alpaca SDK imports
# ---------------------------------------------------------------------------
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
REGIME_FILE = STATE_DIR / "regime.json"

# Allow import of sibling package
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.atomic_write import atomic_write_json, atomic_read_json


# ===== Helper functions ====================================================

def get_bars(client: StockHistoricalDataClient, symbol: str, days: int):
    """
    Fetch daily bars for *symbol* going back *days* calendar days.
    Returns a list of Bar objects sorted oldest-first.
    """
    end = datetime.now(timezone.utc)
    # Add padding for weekends / holidays (roughly 1.6x calendar days)
    start = end - timedelta(days=int(days * 1.6) + 10)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    barset = client.get_stock_bars(request)
    bars = barset[symbol]
    return bars


def closes(bars) -> list[float]:
    """Extract closing prices from bar list."""
    return [float(b.close) for b in bars]


def sma(prices: list[float], period: int) -> float:
    """Simple moving average over the last *period* values."""
    if len(prices) < period:
        raise ValueError(f"Need {period} prices, only have {len(prices)}")
    return sum(prices[-period:]) / period


def ratio_series(prices_a: list[float], prices_b: list[float]) -> list[float]:
    """Element-wise ratio A/B."""
    n = min(len(prices_a), len(prices_b))
    return [a / b for a, b in zip(prices_a[-n:], prices_b[-n:])]


def linear_slope(series: list[float]) -> float:
    """
    Simple linear regression slope over the series.
    Positive = rising, negative = falling.
    Normalised by the mean of the series so it is scale-free.
    """
    n = len(series)
    if n < 2:
        return 0.0
    mean_y = sum(series) / n
    if mean_y == 0:
        return 0.0
    x_mean = (n - 1) / 2.0
    numerator = sum((i - x_mean) * (y - mean_y) for i, y in enumerate(series))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    if denominator == 0:
        return 0.0
    raw_slope = numerator / denominator
    # Normalise: slope per bar as a fraction of mean
    return raw_slope / mean_y


# ===== Signal calculators ==================================================

def calc_trend_score(spy_closes: list[float]) -> tuple[float, dict]:
    """
    SPY 50-day SMA vs 200-day SMA.
    >1% above: +1, >1% below: -1, within 1%: 0.
    """
    sma50 = sma(spy_closes, 50)
    sma200 = sma(spy_closes, 200)
    pct_diff = (sma50 - sma200) / sma200

    if pct_diff > 0.01:
        score = 1.0
    elif pct_diff < -0.01:
        score = -1.0
    else:
        score = 0.0

    detail = {
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
        "pct_diff": round(pct_diff * 100, 3),
        "interpretation": "golden cross" if score == 1 else ("death cross" if score == -1 else "transitional"),
    }
    return score, detail


def calc_volatility_score(vixy_closes: list[float]) -> tuple[float, dict]:
    """
    VIXY current price vs 20-day average.
    Below average AND falling: +1.  Above average AND rising: -1.  Else: 0.
    """
    current = vixy_closes[-1]
    avg20 = sma(vixy_closes, 20)
    # 5-day slope to gauge direction
    slope = linear_slope(vixy_closes[-5:])

    below_avg = current < avg20
    falling = slope < 0
    above_avg = current > avg20
    rising = slope > 0

    if below_avg and falling:
        score = 1.0
    elif above_avg and rising:
        score = -1.0
    else:
        score = 0.0

    detail = {
        "current_price": round(current, 2),
        "sma20": round(avg20, 2),
        "pct_vs_avg": round((current - avg20) / avg20 * 100, 2),
        "slope_5d": round(slope, 6),
        "interpretation": "low fear" if score == 1 else ("high fear" if score == -1 else "neutral"),
    }
    return score, detail


def calc_breadth_score(rsp_closes: list[float], spy_closes: list[float]) -> tuple[float, dict]:
    """
    RSP/SPY ratio trend over 20 days.  Rising: +1, Falling: -1, Flat: 0.
    """
    n = min(len(rsp_closes), len(spy_closes), 20)
    ratio = ratio_series(rsp_closes[-n:], spy_closes[-n:])
    slope = linear_slope(ratio)

    # Threshold: slope magnitude > 0.0005 per bar counts as rising/falling
    THRESHOLD = 0.0005
    if slope > THRESHOLD:
        score = 1.0
    elif slope < -THRESHOLD:
        score = -1.0
    else:
        score = 0.0

    detail = {
        "ratio_start": round(ratio[0], 4),
        "ratio_end": round(ratio[-1], 4),
        "slope_20d": round(slope, 6),
        "interpretation": "broad rally" if score == 1 else ("narrow rally" if score == -1 else "flat"),
    }
    return score, detail


def calc_credit_score(hyg_closes: list[float], lqd_closes: list[float]) -> tuple[float, dict]:
    """
    HYG/LQD ratio trend over 20 days.  Rising: +1, Falling: -1, Flat: 0.
    """
    n = min(len(hyg_closes), len(lqd_closes), 20)
    ratio = ratio_series(hyg_closes[-n:], lqd_closes[-n:])
    slope = linear_slope(ratio)

    THRESHOLD = 0.0005
    if slope > THRESHOLD:
        score = 1.0
    elif slope < -THRESHOLD:
        score = -1.0
    else:
        score = 0.0

    detail = {
        "ratio_start": round(ratio[0], 4),
        "ratio_end": round(ratio[-1], 4),
        "slope_20d": round(slope, 6),
        "interpretation": "credit healthy" if score == 1 else ("credit stress" if score == -1 else "neutral"),
    }
    return score, detail


def calc_momentum_score(iwm_closes: list[float], spy_closes: list[float]) -> tuple[float, dict]:
    """
    IWM/SPY ratio trend over 20 days.  Rising: +1, Falling: -1, Flat: 0.
    """
    n = min(len(iwm_closes), len(spy_closes), 20)
    ratio = ratio_series(iwm_closes[-n:], spy_closes[-n:])
    slope = linear_slope(ratio)

    THRESHOLD = 0.0005
    if slope > THRESHOLD:
        score = 1.0
    elif slope < -THRESHOLD:
        score = -1.0
    else:
        score = 0.0

    detail = {
        "ratio_start": round(ratio[0], 4),
        "ratio_end": round(ratio[-1], 4),
        "slope_20d": round(slope, 6),
        "interpretation": "risk appetite strong" if score == 1 else ("risk aversion" if score == -1 else "neutral"),
    }
    return score, detail


# ===== Main ================================================================

def run_regime_filter(api_key: str, secret_key: str) -> dict:
    """
    Calculate all 5 signals, composite score, regime classification.
    Returns the full regime state dict.
    """
    client = StockHistoricalDataClient(api_key, secret_key)

    # -- Fetch data ---------------------------------------------------------
    print("Fetching market data from Alpaca...")
    symbols_needed = {
        "SPY": 200,
        "VIXY": 20,
        "RSP": 20,
        "HYG": 20,
        "LQD": 20,
        "IWM": 20,
    }
    bars_data = {}
    for sym, days in symbols_needed.items():
        print(f"  {sym}: fetching {days} days of daily bars...")
        bars_data[sym] = get_bars(client, sym, days)
        print(f"  {sym}: received {len(bars_data[sym])} bars")

    # Extract closing prices
    spy_c = closes(bars_data["SPY"])
    vixy_c = closes(bars_data["VIXY"])
    rsp_c = closes(bars_data["RSP"])
    hyg_c = closes(bars_data["HYG"])
    lqd_c = closes(bars_data["LQD"])
    iwm_c = closes(bars_data["IWM"])

    # -- Calculate signals --------------------------------------------------
    print("\nCalculating signals...")

    trend_score, trend_detail = calc_trend_score(spy_c)
    vol_score, vol_detail = calc_volatility_score(vixy_c)
    breadth_score, breadth_detail = calc_breadth_score(rsp_c, spy_c[-20:])
    credit_score, credit_detail = calc_credit_score(hyg_c, lqd_c)
    momentum_score, momentum_detail = calc_momentum_score(iwm_c, spy_c[-20:])

    # -- Composite score ----------------------------------------------------
    weights = {"trend": 0.30, "volatility": 0.25, "breadth": 0.20, "credit": 0.15, "momentum": 0.10}
    composite = (
        trend_score * weights["trend"]
        + vol_score * weights["volatility"]
        + breadth_score * weights["breadth"]
        + credit_score * weights["credit"]
        + momentum_score * weights["momentum"]
    )
    composite = round(composite, 4)

    # -- Classify regime ----------------------------------------------------
    if composite > 0.3:
        regime = "RISK_ON"
    elif composite < -0.3:
        regime = "RISK_OFF"
    else:
        regime = "CAUTIOUS"

    now_iso = datetime.now(timezone.utc).isoformat()

    # -- Load existing state for history ------------------------------------
    existing = atomic_read_json(str(REGIME_FILE))
    history = existing.get("regime_history", []) if existing else []
    history.append({
        "timestamp": now_iso,
        "regime": regime,
        "composite": composite,
    })
    # Keep last 500 entries
    history = history[-500:]

    # -- Build result dict --------------------------------------------------
    result = {
        "schema_version": "1.0.0",
        "current_regime": regime,
        "last_updated": now_iso,
        "signals": {
            "trend_score": trend_score,
            "volatility_score": vol_score,
            "breadth_score": breadth_score,
            "credit_score": credit_score,
            "momentum_score": momentum_score,
        },
        "signal_details": {
            "trend": trend_detail,
            "volatility": vol_detail,
            "breadth": breadth_detail,
            "credit": credit_detail,
            "momentum": momentum_detail,
        },
        "composite_score": composite,
        "weights": weights,
        "regime_history": history,
    }

    # -- Write state --------------------------------------------------------
    ok = atomic_write_json(str(REGIME_FILE), result)
    if ok:
        print(f"\nState written to {REGIME_FILE}")
    else:
        print(f"\nERROR: Failed to write state to {REGIME_FILE}", file=sys.stderr)

    # -- Print summary ------------------------------------------------------
    print("\n" + "=" * 60)
    print("  ATLAS Lite -- Market Regime Filter")
    print("=" * 60)
    print(f"\n  Timestamp:  {now_iso}")
    print(f"\n  {'Signal':<14} {'Score':>6}   Details")
    print(f"  {'-'*14} {'-'*6}   {'-'*36}")
    print(f"  {'Trend':<14} {trend_score:>+5.1f}   SPY 50d SMA={trend_detail['sma50']}, 200d SMA={trend_detail['sma200']} ({trend_detail['pct_diff']:+.3f}%) -> {trend_detail['interpretation']}")
    print(f"  {'Volatility':<14} {vol_score:>+5.1f}   VIXY={vol_detail['current_price']}, 20d avg={vol_detail['sma20']} ({vol_detail['pct_vs_avg']:+.2f}%) -> {vol_detail['interpretation']}")
    print(f"  {'Breadth':<14} {breadth_score:>+5.1f}   RSP/SPY slope={breadth_detail['slope_20d']:+.6f} -> {breadth_detail['interpretation']}")
    print(f"  {'Credit':<14} {credit_score:>+5.1f}   HYG/LQD slope={credit_detail['slope_20d']:+.6f} -> {credit_detail['interpretation']}")
    print(f"  {'Momentum':<14} {momentum_score:>+5.1f}   IWM/SPY slope={momentum_detail['slope_20d']:+.6f} -> {momentum_detail['interpretation']}")
    print()
    print(f"  Composite:  {composite:>+.4f}  (thresholds: >+0.3 RISK_ON, <-0.3 RISK_OFF)")
    print()
    print(f"  >>> REGIME:  {regime}  <<<")
    print()

    # Strategy implications
    if regime == "RISK_ON":
        print("  Monday implications:")
        print("    - Trailing stops: normal parameters, full position sizes")
        print("    - Politician copy: copy all qualifying trades at normal size")
        print("    - Options wheel: normal delta (0.25), 30 DTE target")
        print("    - Self-improvement: experiments run normally")
    elif regime == "CAUTIOUS":
        print("  Monday implications:")
        print("    - Trailing stops: reduce position size by 50%, tighten trail by 25%")
        print("    - Politician copy: BUY only if bipartisan, size cut 50%")
        print("    - Options wheel: lower delta (0.20), shorter DTE (14-21d), higher score_min (0.7)")
        print("    - Self-improvement: require higher Sharpe improvement (0.2)")
    else:  # RISK_OFF
        print("  Monday implications:")
        print("    - Trailing stops: NO new entries, tighten all stops to 2%, exit positions down >3%")
        print("    - Politician copy: PAUSED -- no new copy trades")
        print("    - Options wheel: NO new put sales, only covered calls on existing shares")
        print("    - Self-improvement: PAUSED -- data not representative during chaos")

    print("\n" + "=" * 60)

    return result


if __name__ == "__main__":
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("ERROR: Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.", file=sys.stderr)
        sys.exit(1)

    run_regime_filter(api_key, secret_key)
