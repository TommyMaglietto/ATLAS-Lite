#!/usr/bin/env python3
"""
ATLAS Lite - Crypto Strategy Engine
Implements Bollinger Band + RSI Mean Reversion, EMA Crossover Trend Following,
and Smart DCA with Technical Triggers for crypto assets.

Runs against Alpaca paper trading API. Crypto trades 24/7.

Usage:
    python scripts/crypto_strategy.py [--quiet]
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests as _http  # For Binance funding rate API (free, no auth)

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ---------- paths ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"
CONFIG_DIR = PROJECT_ROOT / "config"
TRADES_LOG = LOGS_DIR / "trades.jsonl"
POSITIONS_FILE = STATE_DIR / "positions.json"
TRAILING_STOPS_FILE = STATE_DIR / "trailing_stops.json"

# ---------- add scripts to path for atomic_write ----------
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json, locked_read_modify_write, file_lock
from resilience import acquire_pid_lock, configure_client_timeouts, validate_spread, validate_min_qty

# ---------- Quiet mode: suppress verbose output when nothing actionable happens ----------
QUIET = "--quiet" in sys.argv

# ---------- API credentials ----------
API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")
PAPER = True  # ALWAYS paper trading per ATLAS rules

# ---------- strategy parameters ----------
CRYPTO_WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]
POSITION_SIZE_USD = 2500  # $2,000-$3,000 per trade, use midpoint
MIN_POSITION_USD = 2000
MAX_POSITION_USD = 3000

# Bollinger Band + RSI Mean Reversion
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 35    # slightly relaxed from 30 for more signals
RSI_OVERBOUGHT = 70

# EMA Crossover
EMA_FAST = 9
EMA_MED = 21
EMA_LONG = 55

# ATR
ATR_PERIOD = 14

# ADX — trend strength (YouTube finding: mean reversion LOSES money without this)
ADX_PERIOD = 14
ADX_RANGING_THRESHOLD = 25  # ADX < 25 = ranging market = mean reversion safe
ADX_TRENDING_THRESHOLD = 20  # ADX > 20 = trending = good for EMA crossover

# DCA triggers
DCA_RSI_THRESHOLD = 42
DCA_AGGRESSIVE_RSI = 20
DCA_NORMAL_SIZE_PCT = 0.01    # 1% of account
DCA_AGGRESSIVE_SIZE_PCT = 0.02  # 2% at RSI<20

# Stop loss
STOP_ATR_MULTIPLIER = 2.0

# Cash reserve
CASH_RESERVE_PCT = 0.20
ACCOUNT_VALUE_APPROX = 98952  # from current state

# VWAP filter (video strategy: institutional trend benchmark)
VWAP_PERIOD = 24               # Rolling VWAP window (24 bars = 24h on 1H timeframe)
VWAP_SLOPE_PERIOD = 5          # Bars to measure VWAP slope
VWAP_SLOPE_CHOP_THRESHOLD = 0.05  # Slope < this % = choppy market, skip trades

# ADR exhaustion filter (video strategy: don't chase extended moves)
ADR_LOOKBACK_DAYS = 10
ADR_EXHAUSTION_PCT = 85        # Today's range > 85% of ADR = exhausted

# EMA dynamic trailing stop (video strategy: ride winners with EMA floor)
EMA_TRAIL_BUFFER_PCT = 0.5     # Buffer below EMA9 for trailing floor

# Body momentum (video strategy: bullish vs bearish candle body pressure)
BODY_MOMENTUM_PERIOD = 5

# Volume-confirmed engulfing (video strategy: institutional reversal pattern)
ENGULF_VOL_MULTIPLIER = 1.5    # Volume must be 1.5x of 5-bar average

# Feature enable flags (can be toggled in config)
VWAP_FILTER_ENABLED = True
ADR_FILTER_ENABLED = True
EMA_TRAIL_ENABLED = True
BODY_MOMENTUM_ENABLED = True
ENGULF_ENABLED = True


# ---------------------------------------------------------------------------
# Adaptive slippage model (Phase 6)
# ---------------------------------------------------------------------------

def update_slippage_model(symbol, fill_price, quote_midpoint):
    """Update the EMA slippage model for a symbol."""
    if not quote_midpoint or quote_midpoint <= 0 or fill_price <= 0:
        return

    actual_slippage = abs(fill_price - quote_midpoint) / quote_midpoint * 100

    model_file = STATE_DIR / "slippage_model.json"
    model = atomic_read_json(str(model_file))
    if not model:
        model = {"schema_version": "1.0.0", "assets": {}, "default_slippage_pct": 0.10}

    asset = model.setdefault("assets", {}).setdefault(symbol, {
        "ema_slippage_pct": 0.10, "samples": 0, "last_updated": None
    })

    alpha = 0.1  # Slow EMA for robustness
    old_ema = asset.get("ema_slippage_pct", 0.10)
    new_ema = alpha * actual_slippage + (1 - alpha) * old_ema

    asset["ema_slippage_pct"] = round(new_ema, 4)
    asset["samples"] = asset.get("samples", 0) + 1
    asset["last_updated"] = datetime.now(timezone.utc).isoformat()
    model["last_updated"] = asset["last_updated"]

    atomic_write_json(str(model_file), model)


def _default_params():
    """Hardcoded fallback defaults (v1 schema)."""
    return {
        "schema_version": "1.0.0",
        "watchlist": ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"],
        "position_size_usd": 2500,
        "position_size_bounds": [1000, 5000],
        "bb_period": 20, "bb_std": 2.0,
        "rsi_period": 14, "rsi_oversold": 35, "rsi_overbought": 70,
        "ema_fast": 9, "ema_med": 21, "ema_long": 55,
        "atr_period": 14,
        "dca_rsi_threshold": 42, "dca_aggressive_rsi": 20,
        "stop_atr_multiplier": 2.0,
        "adx_period": 14, "adx_ranging_threshold": 25, "adx_trending_threshold": 20,
        "trail_pct": 5.0, "loss_pct": 5.0,
        "vwap_filter_enabled": True, "vwap_period": 24,
        "vwap_slope_period": 5, "vwap_slope_chop_threshold": 0.05,
        "adr_filter_enabled": True, "adr_lookback_days": 10, "adr_exhaustion_pct": 85,
        "ema_trail_enabled": True, "ema_trail_buffer_pct": 0.5,
        "body_momentum_enabled": True, "body_momentum_period": 5,
        "engulf_enabled": True, "engulf_vol_multiplier": 1.5,
        "cash_reserve_pct": 0.20,
    }


def _load_v1_params(cs, config):
    """Load v1 flat-format crypto_strategy config."""
    return {
        "schema_version": "1.0.0",
        "watchlist": cs.get("watchlist", ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]),
        "position_size_usd": cs.get("position_size_usd", 2500),
        "position_size_bounds": cs.get("position_size_bounds", [1000, 5000]),
        "bb_period": cs.get("bb_period", 20),
        "bb_std": cs.get("bb_std", 2.0),
        "rsi_period": cs.get("rsi_period", 14),
        "rsi_oversold": cs.get("rsi_oversold", 35),
        "rsi_overbought": cs.get("rsi_overbought", 70),
        "ema_fast": cs.get("ema_fast", 9),
        "ema_med": cs.get("ema_med", 21),
        "ema_long": cs.get("ema_long", 55),
        "atr_period": cs.get("atr_period", 14),
        "dca_rsi_threshold": cs.get("dca_rsi_threshold", 42),
        "dca_aggressive_rsi": cs.get("dca_aggressive_rsi", 20),
        "stop_atr_multiplier": cs.get("stop_atr_multiplier", 2.0),
        "adx_period": cs.get("adx_period", 14),
        "adx_ranging_threshold": cs.get("adx_ranging_threshold", 25),
        "adx_trending_threshold": cs.get("adx_trending_threshold", 20),
        "trail_pct": config.get("trailing_stop", {}).get("trail_pct", 5.0),
        "loss_pct": config.get("trailing_stop", {}).get("loss_pct", 5.0),
        "vwap_filter_enabled": cs.get("vwap_filter_enabled", True),
        "vwap_period": cs.get("vwap_period", 24),
        "vwap_slope_period": cs.get("vwap_slope_period", 5),
        "vwap_slope_chop_threshold": cs.get("vwap_slope_chop_threshold", 0.05),
        "adr_filter_enabled": cs.get("adr_filter_enabled", True),
        "adr_lookback_days": cs.get("adr_lookback_days", 10),
        "adr_exhaustion_pct": cs.get("adr_exhaustion_pct", 85),
        "ema_trail_enabled": cs.get("ema_trail_enabled", True),
        "ema_trail_buffer_pct": cs.get("ema_trail_buffer_pct", 0.5),
        "body_momentum_enabled": cs.get("body_momentum_enabled", True),
        "body_momentum_period": cs.get("body_momentum_period", 5),
        "engulf_enabled": cs.get("engulf_enabled", True),
        "engulf_vol_multiplier": cs.get("engulf_vol_multiplier", 1.5),
        "cash_reserve_pct": cs.get("cash_reserve_pct", 0.20),
    }


def _load_v2_params(cs, config):
    """Load v2 nested-format crypto_strategy config.

    Returns a dict that:
    - stores 'shared' and 'signal' sub-dicts for structured access
    - ALSO merges all shared + first-seen signal values as flat top-level keys
      so existing callers (params.get("rsi_oversold", 35)) keep working
    """
    shared = cs.get("shared", {})
    signal = cs.get("signal", {})

    # Start with flat backward-compat keys from shared
    flat = dict(shared)

    # Overlay signal-specific values so flat keys pick up per-signal values
    # (first signal that defines each key wins -- gives backward compat)
    for sig_name, sig_vals in signal.items():
        for k, v in sig_vals.items():
            if k not in flat and k != "enabled" and not k.endswith("_bounds"):
                flat[k] = v

    # Build the result dict
    result = {
        "schema_version": "2.0.0",
        "watchlist": cs.get("watchlist", ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]),
        "position_size_usd": cs.get("position_size_usd", 4000),
        "position_size_bounds": cs.get("position_size_bounds", [1000, 5000]),
        # Structured sub-dicts for v2-aware code
        "shared": shared,
        "signal": signal,
        # Trailing stop params from their own section
        "trail_pct": config.get("trailing_stop", {}).get("trail_pct", 5.0),
        "loss_pct": config.get("trailing_stop", {}).get("loss_pct", 5.0),
    }

    # Merge all flat keys for backward compatibility
    # (result keys take precedence over flat to preserve watchlist, position_size, etc.)
    for k, v in flat.items():
        if k not in result:
            result[k] = v

    # Ensure key feature flags and params always exist at top level
    # (some callers check these directly)
    result.setdefault("bb_period", shared.get("bb_period", 20))
    result.setdefault("bb_std", shared.get("bb_std", 2.0))
    result.setdefault("rsi_period", shared.get("rsi_period", 14))
    result.setdefault("rsi_oversold", 35)
    result.setdefault("rsi_overbought", 70)
    result.setdefault("ema_fast", shared.get("ema_fast", 9))
    result.setdefault("ema_med", shared.get("ema_med", 21))
    result.setdefault("ema_long", shared.get("ema_long", 55))
    result.setdefault("atr_period", shared.get("atr_period", 14))
    result.setdefault("adx_period", shared.get("adx_period", 14))
    result.setdefault("adx_ranging_threshold", shared.get("adx_ranging_threshold", 25))
    result.setdefault("adx_trending_threshold", shared.get("adx_trending_threshold", 20))
    result.setdefault("dca_rsi_threshold", 42)
    result.setdefault("dca_aggressive_rsi", shared.get("dca_aggressive_rsi", 20))
    result.setdefault("stop_atr_multiplier", shared.get("stop_atr_multiplier", 2.0))
    result.setdefault("vwap_filter_enabled", shared.get("vwap_filter_enabled", True))
    result.setdefault("vwap_period", shared.get("vwap_period", 24))
    result.setdefault("vwap_slope_period", shared.get("vwap_slope_period", 5))
    result.setdefault("vwap_slope_chop_threshold", 0.05)
    result.setdefault("adr_filter_enabled", shared.get("adr_filter_enabled", True))
    result.setdefault("adr_lookback_days", shared.get("adr_lookback_days", 10))
    result.setdefault("adr_exhaustion_pct", shared.get("adr_exhaustion_pct", 85))
    result.setdefault("ema_trail_enabled", shared.get("ema_trail_enabled", True))
    result.setdefault("ema_trail_buffer_pct", shared.get("ema_trail_buffer_pct", 0.5))
    result.setdefault("body_momentum_enabled", True)
    result.setdefault("body_momentum_period", shared.get("body_momentum_period", 5))
    result.setdefault("engulf_enabled", True)
    result.setdefault("engulf_vol_multiplier", 1.5)
    result.setdefault("cash_reserve_pct", shared.get("cash_reserve_pct", 0.20))

    return result


def load_params():
    """Load strategy parameters from config file (self-improvement can modify these).
    Supports both v1 (flat) and v2 (nested shared/signal) config schemas."""
    config_file = CONFIG_DIR / "strategy_params.json"
    config = atomic_read_json(str(config_file))
    if not config or "crypto_strategy" not in config:
        print("  WARNING: Could not load strategy_params.json, using defaults")
        return _default_params()
    cs = config["crypto_strategy"]
    # Detect v2: has both 'signal' and 'shared' keys
    if "signal" in cs and "shared" in cs:
        return _load_v2_params(cs, config)
    return _load_v1_params(cs, config)


def load_params_15m():
    """Load 15-minute strategy parameters from crypto_strategy_15m config section."""
    config_file = CONFIG_DIR / "strategy_params.json"
    config = atomic_read_json(str(config_file))
    if not config or "crypto_strategy_15m" not in config:
        return None  # 15-min strategy not configured

    cs = config["crypto_strategy_15m"]
    if not cs.get("enabled", False):
        return None

    # Reuse the same v2 loading logic
    if "signal" in cs and "shared" in cs:
        return _load_v2_params(cs, config)
    return _load_v1_params(cs, config)


def _get_signal_params(params, signal_type):
    """Get merged params for a signal: shared + signal-specific overrides.
    In v2, each signal gets its own tunable params layered on top of shared.
    In v1, everything is flat so just return params as-is."""
    if params.get("schema_version") == "2.0.0":
        shared = params.get("shared", {})
        sig = params.get("signal", {}).get(signal_type, {})
        merged = dict(shared)
        merged.update(sig)
        return merged
    return params  # v1: everything flat


# ============================================================
# ENTRY ATTRIBUTION HELPERS
# ============================================================

def _score_entry_quality(signal, regime_data):
    """Score entry quality 0-100 based on signal strength, regime, and indicators."""
    score = 50  # base

    # Signal strength
    strength = signal.get("strength", "WEAK")
    if strength == "STRONG":
        score += 20
    elif strength == "MODERATE":
        score += 10
    elif strength == "WEAK":
        score -= 10

    # Regime bonus
    if regime_data:
        regime = regime_data.get("current_regime", "UNKNOWN")
        if regime == "RISK_ON":
            score += 10
        elif regime == "CAUTIOUS":
            score -= 5

    # ADX confirmation
    indicators = signal.get("indicators", {})
    adx = indicators.get("adx")
    if adx is not None:
        strategy = signal.get("strategy", "")
        if "TREND" in strategy and adx > 25:
            score += 10  # Trending + trend strategy = good
        elif "MEAN_REVERSION" in strategy and adx < 20:
            score += 10  # Ranging + mean reversion = good
        elif "MEAN_REVERSION" in strategy and adx > 30:
            score -= 15  # Trending + mean reversion = bad

    # RSI positioning
    rsi = indicators.get("rsi")
    if rsi is not None:
        if 30 < rsi < 70:
            score += 5  # Not extreme — healthy entry
        elif rsi < 25 or rsi > 75:
            score -= 5  # Extreme — risky

    return max(0, min(100, score))


def _get_signal_grade(signal_type, signal_scores):
    """Look up the grade for this signal type from the scoreboard."""
    if not signal_scores or "signal_scoreboard" not in signal_scores:
        return "UNGRADED"
    return signal_scores.get("signal_scoreboard", {}).get(signal_type, {}).get("grade", "UNGRADED")


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def calc_sma(series, period):
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def calc_ema(series, period):
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series, period=14):
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_bollinger_bands(series, period=20, std_dev=2.0):
    """Bollinger Bands: returns (middle, upper, lower)."""
    middle = calc_sma(series, period)
    rolling_std = series.rolling(window=period).std()
    upper = middle + (rolling_std * std_dev)
    lower = middle - (rolling_std * std_dev)
    return middle, upper, lower


def calc_atr(high, low, close, period=14):
    """Average True Range."""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


def calc_adx(high, low, close, period=14):
    """Average Directional Index — measures trend strength.
    ADX > 20-25 = trending market (mean reversion risky).
    ADX < 20 = ranging market (mean reversion ideal).
    """
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_smooth = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_smooth)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx


def calc_vwap(high, low, close, volume, period=24):
    """Rolling Volume Weighted Average Price -- institutional benchmark.
    For 1H crypto bars, period=24 gives a rolling 24-hour VWAP."""
    typical_price = (high + low + close) / 3
    tp_vol = typical_price * volume
    vol_sum = volume.rolling(window=period).sum()
    vwap = tp_vol.rolling(window=period).sum() / vol_sum.replace(0, np.nan)
    return vwap


_BINANCE_SYMBOL_MAP = {
    "SHIB": "1000SHIBUSDT",  # Binance uses 1000SHIB for perpetuals
}

# OI cache file for computing OI change % over time
_OI_CACHE_FILE = STATE_DIR / "oi_cache.json"


def fetch_derivatives_data(symbols, timeout=10):
    """Fetch derivatives data from CoinGecko (free, no auth, no geo-block).

    Single API call returns ALL perpetual contracts: funding rate, open interest,
    24h futures volume. Replaces the broken Binance Futures API (451 geo-blocked).

    Returns dict[symbol] -> {
        "funding_rate": float,          # 8-hour funding rate (+ = longs pay)
        "open_interest": float,         # Total OI in USD
        "futures_volume_24h": float,    # 24h futures volume
    }
    """
    result = {}
    try:
        resp = _http.get("https://api.coingecko.com/api/v3/derivatives", timeout=timeout)
        if resp.status_code != 200:
            return {s: {"funding_rate": None, "open_interest": None, "futures_volume_24h": None} for s in symbols}
        data = resp.json()

        # Build lookup: base symbol -> first matching perpetual contract
        # CoinGecko returns many contracts; we pick the one with highest OI per base
        best_by_base = {}
        for d in data:
            if d.get("contract_type") != "perpetual":
                continue
            idx = d.get("index_id", "")
            oi = float(d.get("open_interest") or 0)
            if idx not in best_by_base or oi > best_by_base[idx].get("_oi", 0):
                best_by_base[idx] = {
                    "funding_rate": float(d.get("funding_rate") or 0) / 100,  # CoinGecko returns %, normalize
                    "open_interest": oi,
                    "futures_volume_24h": float(d.get("volume_24h") or 0),
                    "_oi": oi,
                }

        for symbol in symbols:
            base = symbol.split("/")[0]
            entry = best_by_base.get(base)
            if entry:
                result[symbol] = {
                    "funding_rate": entry["funding_rate"],
                    "open_interest": entry["open_interest"],
                    "futures_volume_24h": entry["futures_volume_24h"],
                }
            else:
                result[symbol] = {"funding_rate": None, "open_interest": None, "futures_volume_24h": None}
    except Exception as e:
        if not QUIET:
            print(f"  WARNING: fetch_derivatives_data failed: {e}")
        result = {s: {"funding_rate": None, "open_interest": None, "futures_volume_24h": None} for s in symbols}
    return result


def compute_oi_changes(derivatives_data):
    """Compute OI % change from cached values. Updates the cache.

    Returns dict[symbol] -> float (% change), or None if no previous cache.
    The first call seeds the cache; change data starts on the second call.
    """
    changes = {}
    # Load previous cache
    prev_cache = {}
    try:
        if _OI_CACHE_FILE.exists():
            with open(_OI_CACHE_FILE, "r") as f:
                prev_cache = json.load(f)
    except Exception:
        pass

    new_cache = {}
    for symbol, ddata in derivatives_data.items():
        oi = ddata.get("open_interest")
        if oi and oi > 0:
            new_cache[symbol] = oi
            prev_oi = prev_cache.get(symbol)
            if prev_oi and prev_oi > 0:
                changes[symbol] = (oi - prev_oi) / prev_oi * 100
            else:
                changes[symbol] = None  # No previous data
        else:
            changes[symbol] = None

    # Save updated cache
    try:
        _OI_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_OI_CACHE_FILE, "w") as f:
            json.dump(new_cache, f)
    except Exception:
        pass

    return changes


def fetch_funding_rates(symbols, timeout=5):
    """Legacy wrapper — now delegates to fetch_derivatives_data().
    Returns dict mapping symbol -> float funding rate, or None for failures."""
    deriv = fetch_derivatives_data(symbols, timeout=timeout)
    return {sym: d.get("funding_rate") for sym, d in deriv.items()}


def compute_indicators(df, params):
    """Compute all technical indicators on an OHLCV DataFrame.
    Uses shared params in v2, flat params in v1."""
    # v2: read from shared sub-dict; v1: read flat params directly
    ip = params.get("shared", params)

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Sanitize: clip negative volume from data anomalies
    df["volume"] = df["volume"].clip(lower=0)

    # Bollinger Bands
    bb_mid, bb_upper, bb_lower = calc_bollinger_bands(close, ip.get("bb_period", 20), ip.get("bb_std", 2.0))
    df["bb_mid"] = bb_mid
    df["bb_upper"] = bb_upper
    df["bb_lower"] = bb_lower

    # RSI
    df["rsi"] = calc_rsi(close, ip.get("rsi_period", 14))

    # EMAs
    df["ema9"] = calc_ema(close, ip.get("ema_fast", 9))
    df["ema21"] = calc_ema(close, ip.get("ema_med", 21))
    df["ema55"] = calc_ema(close, ip.get("ema_long", 55))

    # ATR
    df["atr"] = calc_atr(high, low, close, ip.get("atr_period", 14))

    # ADX — trend strength filter (YouTube research: MUST have this for mean reversion)
    df["adx"] = calc_adx(high, low, close, ip.get("adx_period", 14))

    # BB position (0 = lower band, 1 = upper band)
    bb_range = bb_upper - bb_lower
    df["bb_position"] = (close - bb_lower) / bb_range.replace(0, np.nan)

    # VWAP -- rolling institutional benchmark
    df["vwap"] = calc_vwap(high, low, close, df["volume"], ip.get("vwap_period", 24))

    # VWAP slope -- % change over N bars, flat = choppy market
    df["vwap_slope"] = df["vwap"].pct_change(periods=ip.get("vwap_slope_period", 5)) * 100

    # Volume average (5-bar) for engulfing confirmation
    df["vol_avg_5"] = df["volume"].rolling(window=5).mean()

    # Body momentum -- rolling bullish vs bearish candle body sums
    body = close - df["open"]
    df["bull_body_sum"] = body.where(body > 0, 0).rolling(window=ip.get("body_momentum_period", 5)).sum()
    df["bear_body_sum"] = (-body).where(body < 0, 0).rolling(window=ip.get("body_momentum_period", 5)).sum()

    # Derived features for ML meta-scorer
    df["rsi_roc"] = df["rsi"].diff()
    bb_range_safe = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_width_pct"] = bb_range_safe / close * 100
    df["volume_ratio"] = df["volume"] / df["vol_avg_5"].replace(0, np.nan)
    df["ema_alignment"] = (df["ema9"] - df["ema55"]) / close.replace(0, np.nan) * 100
    vwap_safe = df["vwap"].replace(0, np.nan)
    df["vwap_deviation"] = (close - vwap_safe) / close.replace(0, np.nan) * 100
    df["atr_pct"] = df["atr"] / close.replace(0, np.nan) * 100

    # MACD -- momentum oscillator (standard 12/26/9, research top feature)
    df["macd"] = calc_ema(close, 12) - calc_ema(close, 26)
    df["macd_signal"] = calc_ema(df["macd"], 9)
    df["macd_histogram"] = df["macd"] - df["macd_signal"]

    return df


# ============================================================
# SIGNAL GENERATION
# ============================================================

def generate_signals(df, symbol, params, dry_run=False, existing_positions=None):
    """
    Generate trading signals for a single crypto asset.
    Each signal block reads its OWN per-signal params via _get_signal_params().

    Args:
        dry_run: When True, skip all log_blocked_signal calls (for backtesting).
        existing_positions: When provided, use for stale position check instead of reading file.
    Returns a list of signal dicts.
    """
    signals = []
    if df.empty or len(df) < params.get("ema_long", 55) + 5:
        if not QUIET:
            print(f"  [{symbol}] Not enough data ({len(df)} bars), skipping")
        return signals

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    price = latest["close"]

    rsi = latest["rsi"]
    bb_lower = latest["bb_lower"]
    bb_upper = latest["bb_upper"]
    bb_mid = latest["bb_mid"]
    bb_pos = latest["bb_position"]
    ema9 = latest["ema9"]
    ema21 = latest["ema21"]
    ema55 = latest["ema55"]
    atr = latest["atr"]

    adx = latest["adx"]

    # New indicator values (video strategy features)
    vwap_val = latest.get("vwap", np.nan)
    vwap_slope_val = latest.get("vwap_slope", np.nan)
    vol_avg_5 = latest.get("vol_avg_5", np.nan)
    bull_body_sum = latest.get("bull_body_sum", np.nan)
    bear_body_sum = latest.get("bear_body_sum", np.nan)

    indicators = {
        "price": round(float(price), 4),
        "rsi": round(float(rsi), 2),
        "adx": round(float(adx), 2) if not np.isnan(adx) else None,
        "bb_lower": round(float(bb_lower), 4),
        "bb_mid": round(float(bb_mid), 4),
        "bb_upper": round(float(bb_upper), 4),
        "bb_position": round(float(bb_pos), 4) if not np.isnan(bb_pos) else None,
        "ema9": round(float(ema9), 4),
        "ema21": round(float(ema21), 4),
        "ema55": round(float(ema55), 4),
        "atr": round(float(atr), 4),
        "vwap": round(float(vwap_val), 4) if not np.isnan(vwap_val) else None,
        "vwap_slope": round(float(vwap_slope_val), 4) if not np.isnan(vwap_slope_val) else None,
        # Derived features for ML scorer
        "rsi_roc": round(float(latest.get("rsi_roc", np.nan)), 4) if not np.isnan(latest.get("rsi_roc", np.nan)) else None,
        "bb_width_pct": round(float(latest.get("bb_width_pct", np.nan)), 4) if not np.isnan(latest.get("bb_width_pct", np.nan)) else None,
        "volume_ratio": round(float(latest.get("volume_ratio", np.nan)), 4) if not np.isnan(latest.get("volume_ratio", np.nan)) else None,
        "ema_alignment": round(float(latest.get("ema_alignment", np.nan)), 4) if not np.isnan(latest.get("ema_alignment", np.nan)) else None,
        "vwap_deviation": round(float(latest.get("vwap_deviation", np.nan)), 4) if not np.isnan(latest.get("vwap_deviation", np.nan)) else None,
        "atr_pct": round(float(latest.get("atr_pct", np.nan)), 4) if not np.isnan(latest.get("atr_pct", np.nan)) else None,
        # MACD
        "macd": round(float(latest.get("macd", np.nan)), 4) if not np.isnan(latest.get("macd", np.nan)) else None,
        "macd_signal": round(float(latest.get("macd_signal", np.nan)), 4) if not np.isnan(latest.get("macd_signal", np.nan)) else None,
        "macd_histogram": round(float(latest.get("macd_histogram", np.nan)), 4) if not np.isnan(latest.get("macd_histogram", np.nan)) else None,
    }

    is_ranging = adx < params.get("adx_ranging_threshold", 25) if not np.isnan(adx) else True
    is_trending = adx > params.get("adx_trending_threshold", 20) if not np.isnan(adx) else False

    # VWAP trend filter -- institutional benchmark from video analysis
    # When VWAP filter is disabled, always pass the gate
    vwap_filter_on = params.get("vwap_filter_enabled", params.get("shared", {}).get("vwap_filter_enabled", True))
    above_vwap = (price >= vwap_val if not np.isnan(vwap_val) else True) if vwap_filter_on else True

    # Choppy market detection: each signal can have its own threshold (Feature H fix)
    # Default choppy from shared params, signals can override via _get_signal_params
    def _is_choppy(signal_params):
        """Check if market is choppy using signal-specific or shared threshold."""
        if not vwap_filter_on:
            return False
        thresh = signal_params.get("vwap_slope_chop_threshold",
                                   params.get("shared", params).get("vwap_slope_chop_threshold", 0.05))
        return (abs(vwap_slope_val) < thresh if not np.isnan(vwap_slope_val) else False)

    # Default shared-level choppy (for backward compat / signals without own threshold)
    is_choppy = _is_choppy(params.get("shared", params))
    if is_choppy and not QUIET:
        print(f"  [{symbol}] VWAP slope flat ({vwap_slope_val:.4f}%), choppy market detected")

    # --- Signal 1: Mean Reversion BUY ---
    # Price below lower BB AND RSI oversold, GATED by ADX (ranging market)
    mr_p = _get_signal_params(params, "mean_reversion_oversold")
    if mr_p.get("enabled", True):
        rsi_oversold = mr_p.get("rsi_oversold", 35)
        if price < bb_lower and rsi < rsi_oversold:
            mr_choppy = _is_choppy(mr_p)  # Per-signal threshold (Feature H)
            if mr_choppy:
                if not QUIET:
                    print(f"  [{symbol}] Mean reversion BLOCKED -- VWAP slope flat (choppy market)")
                if not dry_run:
                    mr_thresh = mr_p.get("vwap_slope_chop_threshold",
                                         params.get("shared", params).get("vwap_slope_chop_threshold", 0.05))
                    log_blocked_signal(symbol, "CRYPTO_MEAN_REVERSION", "mean_reversion_oversold",
                                   "vwap_slope_chop", f"VWAP slope {vwap_slope_val:.4f}% < {mr_thresh}%", indicators)
            elif is_ranging:
                signals.append({
                    "symbol": symbol,
                    "strategy": "CRYPTO_MEAN_REVERSION",
                    "action": "BUY",
                    "signal_type": "mean_reversion_oversold",
                    "strength": "STRONG",
                    "reason": f"Price ({price:.2f}) < lower BB ({bb_lower:.2f}), RSI={rsi:.1f}, ADX={adx:.1f} (ranging)",
                    "indicators": indicators,
                    "stop_distance": round(float(atr * mr_p.get("stop_atr_multiplier", 2.0)), 4),
                })
            else:
                if not QUIET:
                    print(f"  [{symbol}] Mean reversion BLOCKED by ADX={adx:.1f} (trending market, would lose money)")

    # --- Signal 2: EMA Crossover BUY ---
    # 9 EMA > 21 EMA, both above 55 EMA, RSI > 50
    # VWAP gate: trend buys only above VWAP (institutional buyers in control)
    ema_p = _get_signal_params(params, "ema_crossover_bullish")
    if ema_p.get("enabled", True):
        ec_choppy = _is_choppy(ema_p)  # Uses signal-specific threshold
        if ema9 > ema21 and ema21 > ema55 and rsi > 50 and above_vwap and not ec_choppy:
            # Check for recent crossover (within last 3 bars)
            cross_recent = False
            for i in range(-3, 0):
                if len(df) > abs(i):
                    row = df.iloc[i]
                    prev_row = df.iloc[i-1] if len(df) > abs(i-1) else None
                    if prev_row is not None:
                        if row["ema9"] > row["ema21"] and prev_row["ema9"] <= prev_row["ema21"]:
                            cross_recent = True
                            break

            # ADX confirms trend = stronger signal
            adx_confirms = is_trending
            strength = "STRONG" if (cross_recent or adx_confirms) else "MODERATE"
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_TREND",
                "action": "BUY",
                "signal_type": "ema_crossover_bullish",
                "strength": strength,
                "reason": f"EMA9({ema9:.2f}) > EMA21({ema21:.2f}) > EMA55({ema55:.2f}), RSI={rsi:.1f}, ADX={adx:.1f}",
                "indicators": indicators,
                "recent_crossover": cross_recent,
                "adx_confirms_trend": adx_confirms,
            })
        elif ema9 > ema21 and ema21 > ema55 and rsi > 50:
            # EMA crossover conditions met but blocked by VWAP filter
            filter_name = "vwap_trend" if not above_vwap else "vwap_slope_chop"
            reason = "below VWAP" if not above_vwap else "choppy market (VWAP slope flat)"
            if not QUIET:
                print(f"  [{symbol}] EMA crossover BLOCKED -- {reason}")
            if not dry_run:
                log_blocked_signal(symbol, "CRYPTO_TREND", "ema_crossover_bullish", filter_name, reason, indicators)

    # --- Signal 3: DCA Trigger BUY ---
    # RSI < threshold AND price in lower half of BB range (skip in choppy markets)
    dca_p = _get_signal_params(params, "dca_technical_trigger")
    if dca_p.get("enabled", True):
        dca_rsi_thresh = dca_p.get("dca_rsi_threshold", 42)
        dca_aggressive_rsi = dca_p.get("dca_aggressive_rsi", 20)
        if rsi < dca_rsi_thresh and bb_pos is not None and not np.isnan(bb_pos) and bb_pos < 0.5 and not is_choppy:
            aggressive = rsi < dca_aggressive_rsi
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_DCA",
                "action": "BUY",
                "signal_type": "dca_technical_trigger",
                "strength": "STRONG" if aggressive else "MODERATE",
                "reason": f"RSI={rsi:.1f} < {dca_rsi_thresh}, BB position={bb_pos:.2f} (lower half)",
                "indicators": indicators,
                "aggressive": aggressive,
            })

    # --- Signal 4: SELL signals for existing positions ---
    # RSI overbought or price above upper BB
    ob_p = _get_signal_params(params, "overbought_exit")
    if ob_p.get("enabled", True):
        rsi_ob = ob_p.get("rsi_overbought", 70)
        if rsi > rsi_ob or price > bb_upper:
            sell_reason = []
            if rsi > rsi_ob:
                sell_reason.append(f"RSI={rsi:.1f} > {rsi_ob}")
            if price > bb_upper:
                sell_reason.append(f"Price ({price:.2f}) above upper BB ({bb_upper:.2f})")
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_MEAN_REVERSION",
                "action": "SELL",
                "signal_type": "overbought_exit",
                "strength": "STRONG" if (rsi > 75 and price > bb_upper) else "MODERATE",
                "reason": "; ".join(sell_reason),
                "indicators": indicators,
            })

    # --- Signal 4B: Time-based stale position exit ---
    # Positions open > 7 days with < 2% gain should be trimmed to free capital
    # In dry_run (backtest) mode, skip file reads entirely
    stale_days = params.get("shared", params).get("stale_position_days", 7)
    stale_min_gain = params.get("shared", params).get("stale_min_gain_pct", 2.0)
    if dry_run:
        stops_data = existing_positions  # None in backtest unless simulator provides
    else:
        stops_data = existing_positions if existing_positions is not None else atomic_read_json(str(TRAILING_STOPS_FILE))
    if stops_data:
        for stop in stops_data.get("active_stops", []):
            if stop.get("symbol") == symbol and stop.get("status") == "ACTIVE":
                opened_at = stop.get("opened_at", "")
                if opened_at:
                    try:
                        open_dt = datetime.fromisoformat(opened_at)
                        if open_dt.tzinfo is None:
                            open_dt = open_dt.replace(tzinfo=timezone.utc)
                        age_days = (datetime.now(timezone.utc) - open_dt).total_seconds() / 86400
                        entry_p = stop.get("entry_price", 0)
                        gain_pct = ((price - entry_p) / entry_p * 100) if entry_p > 0 else 0
                        if age_days > stale_days and gain_pct < stale_min_gain:
                            signals.append({
                                "symbol": symbol,
                                "strategy": stop.get("strategy_source", "CRYPTO_STRATEGY"),
                                "action": "SELL",
                                "signal_type": "stale_position_exit",
                                "full_close": True,
                                "strength": "MODERATE",
                                "reason": f"Position open {age_days:.0f}d with only {gain_pct:+.1f}% gain (threshold: {stale_days}d, {stale_min_gain}%)",
                                "indicators": indicators,
                            })
                    except Exception:
                        pass

    # --- Signal 5: Volume-Confirmed Bullish Engulfing ---
    # Current candle engulfs previous bearish candle with above-average volume
    ve_p = _get_signal_params(params, "volume_engulfing_bullish")
    if ve_p.get("enabled", True) and len(df) >= 3 and above_vwap and not is_choppy:
        curr = df.iloc[-1]
        prev_c = df.iloc[-2]
        curr_bullish = curr["close"] > curr["open"]
        prev_bearish = prev_c["close"] < prev_c["open"]
        body_engulfs = curr["close"] > prev_c["open"] and curr["open"] < prev_c["close"]
        va5 = curr.get("vol_avg_5", np.nan)
        engulf_vol_mult = ve_p.get("engulf_vol_multiplier", 1.5)
        vol_confirms = curr["volume"] >= va5 * engulf_vol_mult if not np.isnan(va5) and va5 > 0 else False

        if curr_bullish and prev_bearish and body_engulfs and vol_confirms:
            vol_ratio = curr["volume"] / va5 if va5 > 0 else 0
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_TREND",
                "action": "BUY",
                "signal_type": "volume_engulfing_bullish",
                "strength": "STRONG",
                "reason": f"Bullish engulfing with {vol_ratio:.1f}x avg volume, above VWAP",
                "indicators": indicators,
            })

    # --- Signal 6: Body Momentum Crossover ---
    # Bullish body sum overtakes bearish body sum while price > EMA55
    bm_p = _get_signal_params(params, "body_momentum_bullish")
    if bm_p.get("enabled", True) and not np.isnan(bull_body_sum) and not np.isnan(bear_body_sum) and above_vwap and not is_choppy:
        prev_bull = prev.get("bull_body_sum", 0) if not np.isnan(prev.get("bull_body_sum", np.nan)) else 0
        prev_bear = prev.get("bear_body_sum", 0) if not np.isnan(prev.get("bear_body_sum", np.nan)) else 0

        # Guard: skip crossover if previous bars had no body data (both 0)
        if bull_body_sum > bear_body_sum and prev_bull <= prev_bear and price > ema55 and not (prev_bull == 0 and prev_bear == 0):
            signals.append({
                "symbol": symbol,
                "strategy": "CRYPTO_TREND",
                "action": "BUY",
                "signal_type": "body_momentum_bullish",
                "strength": "MODERATE",
                "reason": f"Bullish body momentum crossover (bull={bull_body_sum:.2f} > bear={bear_body_sum:.2f}), price > EMA55",
                "indicators": indicators,
            })

    # --- Signal 7: Break of Structure (BOS) BUY ---
    # Bullish BOS: price closes above the most recent confirmed swing high (higher high)
    bos_p = _get_signal_params(params, "break_of_structure")
    if bos_p.get("enabled", True) and len(df) > 30:
        bos_lookback = bos_p.get("bos_lookback", 10)
        bos_confirm = bos_p.get("bos_confirmation_bars", 2)
        bos_atr_filter = bos_p.get("bos_atr_filter", 0.5)
        atr_pct_val = float(latest.get("atr_pct", 0))

        # Only fire if ATR is significant (filters noise in flat markets)
        if atr_pct_val >= bos_atr_filter:
            # Find the most recent swing high: bar whose high is the highest
            # in [i-bos_lookback, i], confirmed bos_confirm bars ago
            search_start = max(0, len(df) - bos_lookback * 3)
            search_end = len(df) - bos_confirm  # Must be confirmed (not the current bar)
            swing_high_price = None
            for i in range(search_end - 1, search_start, -1):
                window_start = max(0, i - bos_lookback)
                window_end = min(len(df), i + bos_lookback + 1)
                bar_high = float(df.iloc[i]["high"])
                window_max = float(df.iloc[window_start:window_end]["high"].max())
                if bar_high >= window_max:
                    swing_high_price = bar_high
                    break  # Found most recent swing high

            if swing_high_price and price > swing_high_price and above_vwap:
                signals.append({
                    "symbol": symbol,
                    "strategy": "CRYPTO_TREND",
                    "action": "BUY",
                    "signal_type": "break_of_structure",
                    "strength": "STRONG" if is_trending else "MODERATE",
                    "reason": f"BOS: price ${price:.2f} broke above swing high ${swing_high_price:.2f}, ATR%={atr_pct_val:.2f}",
                    "indicators": indicators,
                })

    # --- Signal 8: Swing Failure Pattern (SFP) BUY ---
    # Bullish SFP: current bar wicks below previous swing low but closes above it
    # This is a bear trap — shorts get caught, price reverses up
    sfp_p = _get_signal_params(params, "swing_failure_pattern")
    if sfp_p.get("enabled", True) and len(df) > 30:
        sfp_lookback = sfp_p.get("sfp_lookback", 15)
        sfp_wick_min = sfp_p.get("sfp_wick_min_pct", 0.3)
        sfp_vol_confirm = sfp_p.get("sfp_volume_confirm", True)

        # Find the most recent swing low
        search_start = max(0, len(df) - sfp_lookback * 3)
        search_end = len(df) - 2  # Must be a previous swing, not current bar
        swing_low_price = None
        for i in range(search_end - 1, search_start, -1):
            window_start = max(0, i - sfp_lookback)
            window_end = min(len(df), i + sfp_lookback + 1)
            bar_low = float(df.iloc[i]["low"])
            window_min = float(df.iloc[window_start:window_end]["low"].min())
            if bar_low <= window_min:
                swing_low_price = bar_low
                break

        if swing_low_price:
            cur_low = float(latest["low"])
            cur_close = price
            wick_below = swing_low_price - cur_low
            wick_pct = (wick_below / swing_low_price * 100) if swing_low_price > 0 else 0

            # Conditions: wicked below swing low, closed above it, wick is meaningful
            if cur_low < swing_low_price and cur_close > swing_low_price and wick_pct >= sfp_wick_min:
                vol_ok = True
                if sfp_vol_confirm:
                    vol_avg = float(df["volume"].iloc[-6:-1].mean()) if len(df) > 5 else 0
                    cur_vol = float(latest.get("volume", 0))
                    vol_ok = cur_vol > vol_avg * 1.2 if vol_avg > 0 else True

                if vol_ok:
                    signals.append({
                        "symbol": symbol,
                        "strategy": "CRYPTO_MEAN_REVERSION",
                        "action": "BUY",
                        "signal_type": "swing_failure_pattern",
                        "strength": "STRONG",
                        "reason": f"SFP: wicked below swing low ${swing_low_price:.2f} (wick {wick_pct:.1f}%) but closed above at ${cur_close:.2f}",
                        "indicators": indicators,
                    })

    # --- Borderline signals: lean toward trading ---
    # If no buy signals yet, check for near-signals
    # We intentionally lean aggressive to generate trade data for self-improvement
    buy_signals = [s for s in signals if s["action"] == "BUY"]
    if not buy_signals:
        # Near mean-reversion: price within 2% of lower BB or RSI < 43
        bdca_p = _get_signal_params(params, "borderline_dca")
        if bdca_p.get("enabled", True):
            near_bb = price < bb_lower * 1.02
            near_oversold = rsi < 38
            if (near_bb or near_oversold) and not is_choppy:
                signals.append({
                    "symbol": symbol,
                    "strategy": "CRYPTO_DCA",
                    "action": "BUY",
                    "signal_type": "borderline_dca",
                    "strength": "WEAK",
                    "reason": f"Borderline DCA: price near lower BB ({price:.2f} vs {bb_lower:.2f}), RSI={rsi:.1f}",
                    "indicators": indicators,
                    "aggressive": False,
                })

        # Near trend: EMAs partially aligned or RSI borderline (VWAP gated)
        bt_p = _get_signal_params(params, "borderline_trend")
        if bt_p.get("enabled", True):
            if above_vwap and not is_choppy and ((ema9 > ema21 and ema9 > ema55 and rsi > 45 and is_trending) or (ema9 > ema55 and rsi > 50 and is_trending)):
                signals.append({
                    "symbol": symbol,
                    "strategy": "CRYPTO_TREND",
                    "action": "BUY",
                    "signal_type": "borderline_trend",
                    "strength": "WEAK",
                    "reason": f"Near-trend: EMA9>EMA55={ema9>ema55}, EMA9>EMA21={ema9>ema21}, RSI={rsi:.1f}",
                    "indicators": indicators,
                    "recent_crossover": False,
                })

        # BB lower-half accumulation: price in lower 45% of BB with moderate RSI
        ba_p = _get_signal_params(params, "bb_accumulation")
        if ba_p.get("enabled", True):
            if bb_pos is not None and not np.isnan(bb_pos) and bb_pos < 0.30 and rsi < 40 and not is_choppy:
                signals.append({
                    "symbol": symbol,
                    "strategy": "CRYPTO_DCA",
                    "action": "BUY",
                    "signal_type": "bb_accumulation",
                    "strength": "WEAK",
                    "reason": f"BB accumulation zone: BB_pos={bb_pos:.2f} (lower 45%), RSI={rsi:.1f}",
                    "indicators": indicators,
                    "aggressive": False,
                })

    return signals


# ============================================================
# BLOCKED-TRADE LOGGING (feeds self-improvement visibility)
# ============================================================

def log_blocked_signal(symbol, strategy, signal_type, filter_name, reason, indicators=None):
    """Log a blocked signal to trades.jsonl so self-improvement can see filter impact."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "action": "BLOCKED",
        "symbol": symbol,
        "signal_type": signal_type,
        "filter": filter_name,
        "reason": reason,
        "pnl": 0,
    }
    if indicators:
        record["indicators"] = indicators
    log_trade(record)


# ============================================================
# ORDER EXECUTION
# ============================================================

def _cancel_sell_stops_for_symbol(trading_client, symbol):
    """Cancel all server-side sell stop/stop-limit orders for a symbol.

    Alpaca rejects buys when a sell stop-limit exists for the same symbol
    ('potential wash trade detected').  We cancel them before buying; the
    trailing_stop_monitor re-creates them on its next 5-minute cycle.

    Returns list of cancelled order IDs (for logging/debug).
    """
    cancelled = []
    flat_symbol = symbol.replace("/", "")
    try:
        open_orders = trading_client.get_orders()
        for o in open_orders:
            if o.symbol == symbol or o.symbol == flat_symbol:
                if str(o.side).lower().endswith("sell"):
                    if "stop" in o.order_type.value.lower() or "limit" in o.order_type.value.lower():
                        try:
                            trading_client.cancel_order_by_id(str(o.id))
                            cancelled.append(str(o.id))
                        except Exception:
                            pass
    except Exception:
        pass
    if cancelled:
        # Clear the stale order ID from trailing_stops.json so the monitor
        # re-creates the server stop with updated qty on its next run.
        try:
            from atomic_write import locked_read_modify_write
            state_file = str(Path(__file__).resolve().parent.parent / "state" / "trailing_stops.json")
            def clear_ids(state):
                for stop in state.get("active_stops", []):
                    if stop.get("trailing_stop_order_id") in cancelled:
                        stop["trailing_stop_order_id"] = None
                return state
            locked_read_modify_write(state_file, clear_ids)
        except Exception:
            pass  # Non-fatal — monitor will detect stale ID on next check
        print(f"    Cancelled {len(cancelled)} sell stop(s) for {symbol} (wash-trade prevention)")
    return cancelled


def place_crypto_buy(trading_client, symbol, notional_usd, strategy_name, signal):
    """Place a fractional crypto market buy order.

    Cancels any existing server-side sell stop-limit orders for this symbol
    first, because Alpaca rejects opposite-side orders as potential wash trades.
    The trailing_stop_monitor re-creates the server stop on its next 5-min run.
    """
    # Cancel sell stops to avoid wash-trade rejection
    _cancel_sell_stops_for_symbol(trading_client, symbol)

    try:
        order_request = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional_usd, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        order = trading_client.submit_order(order_request)

        result = {
            "order_id": str(order.id),
            "status": str(order.status),
            "symbol": symbol,
            "side": "BUY",
            "notional": round(notional_usd, 2),
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "filled_qty": float(order.filled_qty) if order.filled_qty else None,
        }
        print(f"    ORDER PLACED: {symbol} BUY ${notional_usd:.2f} | ID: {order.id} | Status: {order.status}")
        return result

    except Exception as e:
        print(f"    ORDER FAILED: {symbol} BUY ${notional_usd:.2f} | Error: {e}")
        return {"error": str(e), "symbol": symbol, "side": "BUY", "notional": round(notional_usd, 2)}


def place_crypto_sell(trading_client, symbol, qty, strategy_name, signal):
    """Place a fractional crypto market sell order."""
    try:
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        )
        order = trading_client.submit_order(order_request)

        result = {
            "order_id": str(order.id),
            "status": str(order.status),
            "symbol": symbol,
            "side": "SELL",
            "qty": qty,
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "filled_qty": float(order.filled_qty) if order.filled_qty else None,
        }
        print(f"    ORDER PLACED: {symbol} SELL {qty} | ID: {order.id} | Status: {order.status}")
        return result

    except Exception as e:
        print(f"    ORDER FAILED: {symbol} SELL {qty} | Error: {e}")
        return {"error": str(e), "symbol": symbol, "side": "SELL", "qty": qty}


# ============================================================
# LOGGING
# ============================================================

def log_trade(trade_record):
    """Append a trade record to logs/trades.jsonl with file locking."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(trade_record, default=str) + "\n"
    try:
        with file_lock(str(TRADES_LOG)):
            with open(TRADES_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # Fallback: write without lock rather than lose the trade record
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(line)


# ============================================================
# STATE UPDATES
# ============================================================

def update_positions(new_trades, trading_client):
    """Update state/positions.json with new crypto positions.
    Uses locked_read_modify_write to prevent race with reconcile.py."""

    # Pre-fetch from Alpaca outside the lock (network I/O shouldn't hold lock)
    cash = ACCOUNT_VALUE_APPROX * CASH_RESERVE_PCT
    equity = ACCOUNT_VALUE_APPROX
    alpaca_positions_list = None
    try:
        account = trading_client.get_account()
        cash = float(account.cash)
        equity = float(account.equity)
    except Exception as e:
        if not QUIET:
            print(f"  Warning: Could not fetch account info: {e}")

    try:
        alpaca_positions_list = trading_client.get_all_positions()
    except Exception as e:
        if not QUIET:
            print(f"  Warning: Could not fetch positions from Alpaca: {e}")

    def _modify_positions(positions_data):
        if positions_data is None:
            positions_data = {
                "schema_version": "1.0.0",
                "last_reconciled": datetime.now(timezone.utc).isoformat(),
                "positions": [],
                "pending_orders": [],
                "totals": {}
            }

        nonlocal cash, equity
        if cash == 0:
            cash = positions_data.get("totals", {}).get("cash_available", 0)

        if alpaca_positions_list is not None:
            updated_positions = []
            for pos in alpaca_positions_list:
                updated_positions.append({
                    "symbol": pos.symbol,
                    "asset_class": str(pos.asset_class),
                    "strategy": _find_strategy_for_position(pos.symbol, positions_data),
                    "qty": float(pos.qty),
                    "entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "cost_basis": float(pos.cost_basis),
                    "unrealized_pnl": float(pos.unrealized_pl),
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                })
            positions_data["positions"] = updated_positions

        total_value = sum(p.get("market_value", 0) for p in positions_data.get("positions", []))
        pending_value = sum(p.get("estimated_value", 0) for p in positions_data.get("pending_orders", []))
        positions_data["last_reconciled"] = datetime.now(timezone.utc).isoformat()
        positions_data["totals"] = {
            "total_positions": len(positions_data.get("positions", [])),
            "total_pending": len(positions_data.get("pending_orders", [])),
            "total_value": round(total_value + pending_value, 2),
            "cash_available": round(cash, 2),
            "cash_reserved_pct": round((cash / equity) * 100, 1) if equity > 0 else 0,
            "deployed_pct": round(((equity - cash) / equity) * 100, 1) if equity > 0 else 0,
        }
        return positions_data

    locked_read_modify_write(str(POSITIONS_FILE), _modify_positions)
    if not QUIET:
        print(f"  Positions updated: {len(positions_data.get('positions', []))} active, ${cash:.2f} cash")


def _find_strategy_for_position(symbol, positions_data):
    """Find the strategy label for an existing position."""
    for pos in positions_data.get("positions", []):
        if pos.get("symbol") == symbol:
            return pos.get("strategy", "UNKNOWN")
    # New positions from this script
    if "/" in symbol:
        return "CRYPTO_STRATEGY"
    return "TRAILING_STOP"


def update_trailing_stops(new_trades, params=None):
    """Add new crypto positions to trailing stops state, or update existing ones for DCA buys.
    Uses locked_read_modify_write to prevent race conditions with trailing_stop_monitor."""

    def _modify_stops(stops_data):
        if stops_data is None:
            stops_data = {"schema_version": "1.0.0", "active_stops": [], "closed_stops": []}

        # Build lookup by symbol+timeframe so 15M and 1H stops are independent
        stops_by_key = {}
        for i, stop in enumerate(stops_data.get("active_stops", [])):
            stop_key = f"{stop.get('symbol')}_{stop.get('timeframe', '1H')}"
            stops_by_key[stop_key] = i

        for trade in new_trades:
            if trade.get("action") != "BUY" or "error" in trade:
                continue

            symbol = trade.get("symbol")
            trade_tf = trade.get("timeframe", "1H")
            trade_key = f"{symbol}_{trade_tf}"
            price = trade.get("fill_price") or trade.get("indicators", {}).get("price", 0)
            qty = trade.get("fill_qty", 0)
            if price <= 0 or qty <= 0:
                continue

            if trade_key in stops_by_key:
                idx = stops_by_key[trade_key]
                stop = stops_data["active_stops"][idx]
                old_qty = stop.get("qty", 0)
                old_entry = stop.get("entry_price", price)
                new_total_qty = old_qty + qty
                new_entry = ((old_entry * old_qty) + (price * qty)) / new_total_qty if new_total_qty > 0 else price
                stop["qty"] = round(new_total_qty, 9)
                stop["entry_price"] = round(new_entry, 6)
                stop["last_checked"] = datetime.now(timezone.utc).isoformat()
                if not QUIET:
                    print(f"  Updated trailing stop for {symbol}: qty {old_qty:.6f} -> {new_total_qty:.6f}, avg entry ${old_entry:.4f} -> ${new_entry:.4f}")
            else:
                loss_pct = (params or {}).get("loss_pct", 5.0)
                fixed_floor = price * (1 - loss_pct / 100)

                atr_value = trade.get("indicators", {}).get("atr")
                stop_atr_mult = (params or {}).get("stop_atr_multiplier", 2.0)
                atr_floor = None
                if atr_value is not None and atr_value > 0:
                    atr_floor = price - (atr_value * stop_atr_mult)

                ema_value = trade.get("indicators", {}).get("ema9")
                ema_floor = None
                if ema_value is not None and ema_value > 0:
                    ema_floor = ema_value * (1 - (params or {}).get("ema_trail_buffer_pct", 0.5) / 100)

                floor = fixed_floor
                if atr_floor is not None and atr_floor > floor:
                    floor = atr_floor
                if ema_floor is not None and ema_floor > floor:
                    floor = ema_floor

                # Guard: floor can NEVER be within 1% of entry price
                # Prevents EMA trail floor from setting floor above entry (AVAX bug)
                floor = min(floor, price * 0.99)

                # ATR-based dynamic trail: 2 * ATR / price * 100, clamped to [2%, 15%]
                atr_for_trail = trade.get("indicators", {}).get("atr")
                if atr_for_trail and price > 0:
                    dynamic_trail = round((2.0 * float(atr_for_trail)) / price * 100, 2)
                    dynamic_trail = max(2.0, min(15.0, dynamic_trail))
                else:
                    dynamic_trail = (params or {}).get("trail_pct", 5.0)

                stop_entry = {
                    "symbol": symbol,
                    "asset_class": "crypto",
                    "entry_order_id": trade.get("order_id", ""),
                    "qty": round(qty, 9),
                    "entry_price": round(price, 6),
                    "highest_price": round(price, 6),
                    "floor_price": round(floor, 4),
                    "trail_pct": dynamic_trail,
                    "loss_pct": loss_pct,
                    "status": "ACTIVE",
                    "trailing_stop_order_id": None,
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "strategy_source": trade.get("strategy", "CRYPTO_STRATEGY"),
                    "entry_signal_type": trade.get("signal_type", "unknown"),
                    "timeframe": trade.get("timeframe", "1H"),
                }
                if atr_floor is not None:
                    stop_entry["atr_floor"] = round(atr_floor, 4)
                if ema_floor is not None:
                    stop_entry["ema_trail_floor"] = round(ema_floor, 4)
                stops_data["active_stops"].append(stop_entry)
                stops_by_key[trade_key] = len(stops_data["active_stops"]) - 1
                if not QUIET:
                    atr_info = f", atr_floor=${atr_floor:.4f}" if atr_floor is not None else ""
                    print(f"  New trailing stop for {symbol}: qty={qty:.6f}, entry=${price:.4f}, floor=${floor:.4f}{atr_info}")

        return stops_data

    locked_read_modify_write(str(TRAILING_STOPS_FILE), _modify_stops)
    if not QUIET:
        stops_data = atomic_read_json(str(TRAILING_STOPS_FILE)) or {}
        print(f"  Trailing stops updated: {len(stops_data.get('active_stops', []))} active")


# ============================================================
# MAIN
# ============================================================

def update_ema_trail_floors(all_data, params):
    """Update EMA-based trailing floors for all active crypto stops.
    Uses EMA9 as a dynamic floor -- ride winners while trend holds.
    Uses locked_read_modify_write to prevent race conditions."""
    if not params.get("ema_trail_enabled", True):
        return

    buffer_pct = params.get("ema_trail_buffer_pct", 0.5)

    def _modify_ema_floors(stops_data):
        if not stops_data:
            return stops_data or {}

        for stop in stops_data.get("active_stops", []):
            symbol = stop.get("symbol", "")
            if stop.get("asset_class") != "crypto" or stop.get("status") != "ACTIVE":
                continue

            key_1h = f"{symbol}_1H"
            if key_1h not in all_data:
                continue

            df = all_data[key_1h]
            if df.empty or "ema9" not in df.columns:
                continue

            ema9_val = float(df.iloc[-1]["ema9"])
            if ema9_val <= 0 or np.isnan(ema9_val):
                continue

            ema_floor = round(ema9_val * (1 - buffer_pct / 100), 4)
            old_floor = stop.get("floor_price", 0)

            stop["ema_trail_floor"] = ema_floor

            entry_price = stop.get("entry_price", float("inf"))
            if ema_floor > old_floor and ema_floor < entry_price * 0.99:
                stop["floor_price"] = ema_floor
                if not QUIET:
                    print(f"  {symbol}: EMA floor raised ${old_floor:.4f} -> ${ema_floor:.4f} (EMA9=${ema9_val:.4f})")

        return stops_data

    locked_read_modify_write(str(TRAILING_STOPS_FILE), _modify_ema_floors)


def main():
    now = datetime.now(timezone.utc)
    if not QUIET:
        print("=" * 70)
        print(f"ATLAS Lite Crypto Strategy Engine")
        print(f"Run time: {now.isoformat()}")
        print("=" * 70)

    if not acquire_pid_lock():
        return

    # ---- Load dynamic parameters from config ----
    params = load_params()
    if not QUIET:
        print(f"\n[0/6] Loaded parameters from config:")
        print(f"  RSI oversold: {params['rsi_oversold']}, overbought: {params['rsi_overbought']}")
        print(f"  DCA RSI threshold: {params['dca_rsi_threshold']}")
        print(f"  Position size: ${params['position_size_usd']}")
        print(f"  BB period: {params['bb_period']}, std: {params['bb_std']}")

    # Extract watchlist for the for-loop iteration
    watchlist = params["watchlist"]
    symbol_to_id = {sym: idx for idx, sym in enumerate(watchlist)}

    if not QUIET:
        print(f"Watchlist: {', '.join(watchlist)}")

    # Load signal quality scores (if available)
    SIGNAL_SCORES_FILE = STATE_DIR / "signal_scores.json"
    signal_scores = atomic_read_json(str(SIGNAL_SCORES_FILE))
    disabled_signals = set()
    probation_signals = set()  # F-grade signals with improving recent performance
    if signal_scores and "signal_scoreboard" in signal_scores:
        for sig_name, score in signal_scores["signal_scoreboard"].items():
            if score.get("grade") == "F" and score.get("closed_trades", 0) >= 5:
                # Check if recent performance shows recovery (win_rate > 30%)
                recent_win_rate = score.get("recent_win_rate")
                if recent_win_rate is None:
                    # Approximate from overall stats if recent_win_rate not tracked yet
                    recent_win_rate = score.get("win_rate", 0.0)
                if recent_win_rate > 0.30:
                    probation_signals.add(sig_name)
                    if not QUIET:
                        print(f"  Signal '{sig_name}' on PROBATION — F grade but recent win rate "
                              f"{recent_win_rate:.0%} > 30%, using 50% position size")
                else:
                    disabled_signals.add(sig_name)
                    if not QUIET:
                        print(f"  Signal '{sig_name}' DISABLED — F grade ({score.get('win_rate', 0):.0%} win rate)")
        if not disabled_signals and not probation_signals and not QUIET:
            print(f"  All signals active (scores loaded for {len(signal_scores['signal_scoreboard'])} signals)")
    elif not QUIET:
        print(f"  Signal scores not available yet — all signals active")

    # Load adaptive signal weights (Phase 4)
    SIGNAL_WEIGHTS_FILE = STATE_DIR / "signal_weights.json"
    signal_weights_data = atomic_read_json(str(SIGNAL_WEIGHTS_FILE))
    signal_weights = {}
    if signal_weights_data and "weights" in signal_weights_data:
        signal_weights = {k: v.get("weight", 1.0) for k, v in signal_weights_data["weights"].items()}
        if not QUIET:
            low = [k for k, v in signal_weights.items() if v < 0.5]
            high = [k for k, v in signal_weights.items() if v > 1.2]
            print(f"  Signal weights loaded: {len(signal_weights)} signals")
            if low:
                print(f"    Low-weight signals: {', '.join(low)}")
            if high:
                print(f"    High-conviction signals: {', '.join(high)}")
    elif not QUIET:
        print(f"  Signal weights not available yet — all signals at default weight 1.0")

    # ---- Initialize clients ----
    if not QUIET:
        print("\n[1/6] Initializing Alpaca clients...")
    data_client = CryptoHistoricalDataClient()  # No keys needed for crypto data
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    configure_client_timeouts(trading_client)

    # Verify account
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        if not QUIET:
            print(f"  Account: equity=${equity:,.2f}, cash=${cash:,.2f}")
            print(f"  Cash reserve needed (20%): ${equity * params.get('cash_reserve_pct', 0.20):,.2f}")
            available = cash - (equity * params.get("cash_reserve_pct", 0.20))
            print(f"  Available for new trades: ${available:,.2f}")
    except Exception as e:
        print(f"  ERROR connecting to Alpaca: {e}")
        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": "SYSTEM",
            "action": "API_FAILURE",
            "symbol": "ALPACA",
            "reason": str(e),
            "pnl": 0,
        })
        return

    # ---- Check regime filter ----
    if not QUIET:
        print("\n[1.5/6] Checking regime filter...")
    REGIME_FILE = STATE_DIR / "regime.json"
    regime_data = atomic_read_json(str(REGIME_FILE))
    if regime_data:
        regime = regime_data.get("current_regime", "UNKNOWN")
        composite = regime_data.get("composite_score", 0)
        if not QUIET:
            print(f"  Current regime: {regime} (composite: {composite})")

        if regime == "RISK_OFF":
            if QUIET:
                print(f"[QUIET] {len(watchlist)} pairs scanned, 0 signals triggered (RISK_OFF)")
            else:
                print("  RISK_OFF regime — NO new entries allowed. Exiting.")
                print("  (Existing positions will be managed by trailing stop monitor)")
            return []
        elif regime == "CAUTIOUS":
            if not QUIET:
                print("  CAUTIOUS regime — reducing position sizes by 50%")
            # Apply cautious multiplier from strategy_params
            CAUTIOUS_MULTIPLIER = 0.5
        else:
            CAUTIOUS_MULTIPLIER = 1.0
            if not QUIET:
                print(f"  RISK_ON — full position sizes")
    else:
        if not QUIET:
            print("  WARNING: Could not read regime.json, proceeding with caution")
        CAUTIOUS_MULTIPLIER = 0.75
        regime_data = None
        regime = "UNKNOWN"
        composite = 0

    # Check existing positions
    try:
        existing_positions = {p.symbol: p for p in trading_client.get_all_positions()}
        if not QUIET:
            print(f"  Existing positions: {list(existing_positions.keys())}")
    except Exception as e:
        print(f"  Warning: Could not fetch positions: {e}")
        existing_positions = {}

    # ---- Fetch historical data ----
    if not QUIET:
        print("\n[2/6] Fetching crypto bar data...")
    all_data = {}

    for timeframe_name, timeframe, bars_needed in [
        ("15M", TimeFrame(15, TimeFrame.Minute.unit), 200),
        ("1H", TimeFrame.Hour, 200),
        ("4H", TimeFrame(4, TimeFrame.Hour.unit), 200),
    ]:
        if "15M" in timeframe_name:
            tf_hours = 0.25  # 15 min = 0.25 hours
        elif "4H" in timeframe_name:
            tf_hours = 4
        else:
            tf_hours = 1
        start_time = now - timedelta(hours=bars_needed * tf_hours)
        try:
            request = CryptoBarsRequest(
                symbol_or_symbols=watchlist,
                timeframe=timeframe,
                start=start_time,
                end=now,
            )
            bars = data_client.get_crypto_bars(request)

            for symbol in watchlist:
                key = f"{symbol}_{timeframe_name}"
                try:
                    symbol_bars = bars[symbol]
                    rows = []
                    for bar in symbol_bars:
                        rows.append({
                            "timestamp": bar.timestamp,
                            "open": float(bar.open),
                            "high": float(bar.high),
                            "low": float(bar.low),
                            "close": float(bar.close),
                            "volume": float(bar.volume),
                        })
                    df = pd.DataFrame(rows)
                    if not df.empty:
                        df = df.sort_values("timestamp").reset_index(drop=True)
                        df = compute_indicators(df, params)
                        all_data[key] = df
                        if not QUIET:
                            print(f"  {key}: {len(df)} bars loaded, latest close=${df.iloc[-1]['close']:.4f}")
                    else:
                        if not QUIET:
                            print(f"  {key}: NO DATA")
                except (KeyError, IndexError) as e:
                    if not QUIET:
                        print(f"  {key}: No data available ({e})")
        except Exception as e:
            if not QUIET:
                print(f"  Error fetching {timeframe_name} bars: {e}")

    if not all_data:
        print("\nFATAL: No data fetched. Exiting.")
        return

    # ---- Fetch derivatives data (CoinGecko: funding rate + OI + futures volume) ----
    derivatives_data = {}
    funding_rates = {}
    oi_changes = {}
    try:
        if not QUIET:
            print("\n[2.7/6] Fetching derivatives data (CoinGecko)...")
        derivatives_data = fetch_derivatives_data(watchlist)
        # Extract funding rates for backward compatibility
        funding_rates = {sym: d.get("funding_rate") for sym, d in derivatives_data.items()}
        # Compute OI % changes from cached values
        oi_changes = compute_oi_changes(derivatives_data)
        if not QUIET:
            funded = {k: v for k, v in funding_rates.items() if v is not None}
            oi_valid = {k: v for k, v in oi_changes.items() if v is not None}
            print(f"  Funding rates: {len(funded)}/{len(watchlist)} symbols")
            print(f"  OI changes: {len(oi_valid)}/{len(watchlist)} symbols")
    except Exception as e:
        if not QUIET:
            print(f"  Derivatives data fetch failed: {e}")

    # ---- Cross-asset intelligence: BTC features for altcoin signals ----
    btc_features = None
    btc_key = "BTC/USD_1H"
    if btc_key in all_data:
        btc_df = all_data[btc_key]
        if not btc_df.empty and len(btc_df) > 60:
            btc_latest = btc_df.iloc[-1]
            btc_close = float(btc_latest["close"])
            if btc_close > 0:
                btc_features = {
                    "btc_rsi": round(float(btc_latest.get("rsi", np.nan)), 2) if not np.isnan(btc_latest.get("rsi", np.nan)) else None,
                    "btc_adx": round(float(btc_latest.get("adx", np.nan)), 2) if not np.isnan(btc_latest.get("adx", np.nan)) else None,
                    "btc_vwap_slope": round(float(btc_latest.get("vwap_slope", np.nan)), 4) if not np.isnan(btc_latest.get("vwap_slope", np.nan)) else None,
                    "btc_ema_alignment": round((float(btc_latest.get("ema9", 0)) - float(btc_latest.get("ema55", 0))) / btc_close * 100, 4),
                    "btc_oi_change_pct": oi_changes.get("BTC/USD"),
                }
                if not QUIET:
                    print(f"\n  BTC cross-asset: RSI={btc_features['btc_rsi']}, ADX={btc_features['btc_adx']}")

    # ---- Load ML meta-scorer model (if available) ----
    ml_model = None
    ml_metadata = None
    try:
        from ml_scorer import load_model, extract_features, predict_confidence
        result = load_model()
        if result:
            ml_model, ml_metadata = result
            if not QUIET:
                print(f"  ML model: AUC={ml_metadata.get('cv_auc', 0):.3f}, trained={ml_metadata.get('training_date', '?')[:10]}")
        elif not QUIET:
            print(f"  ML model not available -- rule-based mode")
    except ImportError:
        if not QUIET:
            print(f"  ML scorer not installed (pip install xgboost scikit-learn)")
    except Exception as e:
        if not QUIET:
            print(f"  ML model error: {e} -- rule-based fallback")

    # ---- Compute ADR exhaustion filter ----
    # Uses daily bars for ADR baseline + 1H bars for intraday range
    # (1H bars avoid the UTC-midnight gap where daily "today" bar has near-zero range)
    adr_data = {}
    if params.get("adr_filter_enabled", True):
        if not QUIET:
            print("\n[2.5/6] Computing Average Daily Range...")
        try:
            adr_lookback = params.get("adr_lookback_days", 10)
            adr_exhaust_pct = params.get("adr_exhaustion_pct", 85)
            daily_start = now - timedelta(days=adr_lookback + 2)
            daily_request = CryptoBarsRequest(
                symbol_or_symbols=watchlist,
                timeframe=TimeFrame.Day,
                start=daily_start,
                end=now,
            )
            daily_bars = data_client.get_crypto_bars(daily_request)
            for symbol in watchlist:
                try:
                    bars_list = list(daily_bars[symbol])
                    if len(bars_list) >= 2:
                        # ADR from completed days (exclude today's incomplete bar)
                        completed = bars_list[:-1][-adr_lookback:]
                        if len(completed) < 3:
                            if not QUIET:
                                print(f"  {symbol}: insufficient daily bars ({len(completed)}), skipping ADR")
                            continue
                        ranges = [float(b.high) - float(b.low) for b in completed]
                        adr = np.mean(ranges) if ranges else 0

                        # Intraday range: use last 24 1H bars for crypto 24/7 accuracy
                        # This avoids the UTC midnight gap where daily bar has near-zero range
                        key_1h = f"{symbol}_1H"
                        if key_1h in all_data and len(all_data[key_1h]) >= 24:
                            last_24h = all_data[key_1h].tail(24)
                            today_range = float(last_24h["high"].max() - last_24h["low"].min())
                        else:
                            # Fallback to daily bar
                            today = bars_list[-1]
                            today_range = float(today.high) - float(today.low)

                        exhaustion_pct = (today_range / adr * 100) if adr > 0 else 0
                        exhausted = exhaustion_pct >= adr_exhaust_pct
                        adr_data[symbol] = {
                            "adr": round(float(adr), 4),
                            "today_range": round(float(today_range), 4),
                            "exhaustion_pct": round(float(exhaustion_pct), 1),
                            "exhausted": exhausted,
                        }
                        if not QUIET:
                            status = "EXHAUSTED" if exhausted else "OK"
                            print(f"  {symbol}: ADR=${adr:.2f}, today=${today_range:.2f} ({exhaustion_pct:.0f}%) [{status}]")
                except (KeyError, IndexError):
                    pass
        except Exception as e:
            if not QUIET:
                print(f"  ADR fetch error: {e}")

    # ---- Generate signals ----
    if not QUIET:
        print("\n[3/6] Generating signals...")
    all_signals = []

    # Load 15m params once outside the symbol loop
    params_15m = load_params_15m()

    for symbol in watchlist:
        if not QUIET:
            print(f"\n  --- {symbol} ---")

        # Generate from 15M (if enabled, with 15m-specific params)
        key_15m = f"{symbol}_15M"
        if params_15m and key_15m in all_data:
            sigs_15m = generate_signals(all_data[key_15m], symbol, params_15m, dry_run=False)
            for s in sigs_15m:
                s["timeframe"] = "15M"
                s["signal_type"] = s.get("signal_type", "") + "_15m"  # Tag as 15-min signal
                ind = s.setdefault("indicators", {})
                fr = funding_rates.get(symbol)
                if fr is not None:
                    ind["funding_rate"] = round(fr, 8)
                oi_ch = oi_changes.get(symbol)
                if oi_ch is not None:
                    ind["open_interest_change_pct"] = round(oi_ch, 4)
                btc_oi = oi_changes.get("BTC/USD") if symbol != "BTC/USD" else oi_ch
                if btc_oi is not None:
                    ind["btc_open_interest_change_pct"] = round(btc_oi, 4)
            all_signals.extend(sigs_15m)
            if not QUIET:
                for s in sigs_15m:
                    print(f"    [15M] {s['strategy']} {s['action']} ({s['strength']}): {s['reason']}")

        # Prefer 1H data for mean reversion/DCA, 4H for trend following
        key_1h = f"{symbol}_1H"
        key_4h = f"{symbol}_4H"

        # Generate from 1H
        if key_1h in all_data:
            sigs = generate_signals(all_data[key_1h], symbol, params)
            for s in sigs:
                s["timeframe"] = "1H"
                ind = s.setdefault("indicators", {})
                fr = funding_rates.get(symbol)
                if fr is not None:
                    ind["funding_rate"] = round(fr, 8)
                oi_ch = oi_changes.get(symbol)
                if oi_ch is not None:
                    ind["open_interest_change_pct"] = round(oi_ch, 4)
                btc_oi = oi_changes.get("BTC/USD") if symbol != "BTC/USD" else oi_ch
                if btc_oi is not None:
                    ind["btc_open_interest_change_pct"] = round(btc_oi, 4)
            all_signals.extend(sigs)
            if not QUIET:
                if sigs:
                    for s in sigs:
                        print(f"    [1H] {s['strategy']} {s['action']} ({s['strength']}): {s['reason']}")
                else:
                    print(f"    [1H] No signals")

        # Generate from 4H (only trend signals)
        if key_4h in all_data:
            sigs_4h = generate_signals(all_data[key_4h], symbol, params)
            trend_sigs = [s for s in sigs_4h if "TREND" in s.get("strategy", "")]
            for s in trend_sigs:
                s["timeframe"] = "4H"
                ind = s.setdefault("indicators", {})
                fr = funding_rates.get(symbol)
                if fr is not None:
                    ind["funding_rate"] = round(fr, 8)
                oi_ch = oi_changes.get(symbol)
                if oi_ch is not None:
                    ind["open_interest_change_pct"] = round(oi_ch, 4)
                btc_oi = oi_changes.get("BTC/USD") if symbol != "BTC/USD" else oi_ch
                if btc_oi is not None:
                    ind["btc_open_interest_change_pct"] = round(btc_oi, 4)
            # Avoid duplicate trend signals if already generated on 1H
            existing_trend = any(
                s["symbol"] == symbol and "TREND" in s["strategy"] and s["action"] == "BUY"
                for s in all_signals
            )
            if not existing_trend:
                all_signals.extend(trend_sigs)
                if not QUIET:
                    for s in trend_sigs:
                        print(f"    [4H] {s['strategy']} {s['action']} ({s['strength']}): {s['reason']}")

    # ---- Summary of signals ----
    buy_signals = [s for s in all_signals if s["action"] == "BUY"]
    sell_signals = [s for s in all_signals if s["action"] == "SELL"]

    # Multi-timeframe confirmation: 4H trend as position size modifier (not hard gate)
    # Bearish 4H = reduced position, not blocked (generates learning data)
    tf_4h_discount = params.get("shared", params).get("tf_4h_discount_factor", 0.5)
    for sig in buy_signals:
        sym = sig["symbol"]
        key_4h = f"{sym}_4H"
        if key_4h in all_data and len(all_data[key_4h]) > 60:
            df_4h = all_data[key_4h]
            latest_4h = df_4h.iloc[-1]
            ema9_4h = float(latest_4h.get("ema9", 0))
            ema55_4h = float(latest_4h.get("ema55", 0))
            if ema9_4h > ema55_4h:
                sig["tf_4h_multiplier"] = 1.0
            else:
                sig["tf_4h_multiplier"] = tf_4h_discount
                if not QUIET:
                    print(f"  4H DISCOUNT: {sym} {sig.get('signal_type','')} -- 4H bearish, {tf_4h_discount:.0%} size")
        else:
            sig["tf_4h_multiplier"] = 1.0

    if not QUIET:
        n_discounted = sum(1 for s in buy_signals if s.get("tf_4h_multiplier", 1.0) < 1.0)
        print(f"\n  TOTAL: {len(buy_signals)} BUY signals ({n_discounted} 4H-discounted), {len(sell_signals)} SELL signals")

    # ---- Execute trades ----
    if not QUIET:
        print("\n[4/6] Executing trades...")
    trades_placed = []

    # De-duplicate: pick the highest weighted-score signal per symbol PER TIMEFRAME for buys
    best_buy_per_symbol = {}
    strength_rank = {"STRONG": 3, "MODERATE": 2, "WEAK": 1}
    for sig in buy_signals:
        sym = sig["symbol"]
        tf = sig.get("timeframe", "1H")
        dedup_key = f"{sym}_{tf}"
        raw_score = strength_rank.get(sig.get("strength", "WEAK"), 0)
        weight = signal_weights.get(sig.get("signal_type", ""), 1.0)
        weighted_score = raw_score * weight
        sig["_weighted_score"] = weighted_score

        # Weight < 0.1 = effectively disabled (replaces binary F-grade gate)
        if weight < 0.1:
            if not QUIET:
                print(f"  SKIPPED: {sym} -- signal '{sig['signal_type']}' weight {weight:.2f} (effectively disabled)")
            continue

        # ML meta-scorer confidence gate
        if ml_model is not None:
            try:
                signal_context = {
                    "signal_strength": sig.get("strength", "WEAK"),
                    "signal_weight": weight,
                    "regime_composite": composite if regime_data else 0,
                    "entry_quality_score": _score_entry_quality(sig, regime_data),
                }
                ml_features = extract_features(
                    sig.get("indicators", {}), sym,
                    timestamp=now.isoformat(),
                    cross_asset_data=btc_features,
                    signal_context=signal_context,
                    symbol_id=symbol_to_id.get(sym, 0),
                )
                feature_mask = ml_metadata.get("_feature_mask") if ml_metadata else None
                confidence = predict_confidence(ml_model, ml_features, feature_mask=feature_mask)
                sig["ml_confidence"] = round(confidence, 4)

                ml_threshold = params.get("shared", params).get("ml_confidence_threshold", 0.0)
                if confidence < ml_threshold:
                    if not QUIET:
                        print(f"  ML BLOCKED: {sym} -- confidence {confidence:.3f} < {ml_threshold}")
                    log_blocked_signal(sym, sig.get("strategy", ""), sig.get("signal_type", ""),
                                       "ml_low_confidence", f"confidence={confidence:.3f}", sig.get("indicators"))
                    continue
            except Exception as e:
                sig["ml_confidence"] = 0.5
                if not QUIET:
                    print(f"  ML error for {sym}: {e} -- proceeding rule-based")

        if dedup_key not in best_buy_per_symbol or weighted_score > best_buy_per_symbol[dedup_key].get("_weighted_score", 0):
            best_buy_per_symbol[dedup_key] = sig

    # Also include lower-strength signals for symbols without strong signals
    # to ensure we get enough trades
    additional_buys = []
    for sig in buy_signals:
        sym = sig["symbol"]
        tf = sig.get("timeframe", "1H")
        dedup_key = f"{sym}_{tf}"
        if dedup_key in best_buy_per_symbol and sig is not best_buy_per_symbol[dedup_key]:
            # Different strategy for same symbol+timeframe, keep for diversity
            if sig["strategy"] != best_buy_per_symbol[dedup_key]["strategy"]:
                additional_buys.append(sig)

    # Calculate available cash for new crypto
    position_size_usd = params.get("position_size_usd", 2500)
    pos_bounds = params.get("position_size_bounds", [1000, 5000])
    min_position_usd = pos_bounds[0]
    max_position_usd = pos_bounds[1]
    cash_reserve = equity * params.get("cash_reserve_pct", 0.20)
    available_cash = cash - cash_reserve
    if not QUIET:
        print(f"  Cash: ${cash:,.2f} | Reserve: ${cash_reserve:,.2f} | Available: ${available_cash:,.2f}")

    if available_cash <= 0:
        if not QUIET:
            print(f"  NO AVAILABLE CASH after reserve -- skipping all buys")
        best_buy_per_symbol = {}  # Skip all buys
        effective_size = 0
    elif available_cash < min_position_usd:
        if not QUIET:
            print(f"  WARNING: Available cash (${available_cash:,.2f}) below minimum position size (${min_position_usd})")
            print(f"  Will attempt smaller trades to generate data...")
        # Reduce position size for data generation
        effective_size = max(500, available_cash / max(len(best_buy_per_symbol), 1))
    else:
        effective_size = min(position_size_usd, available_cash / max(len(best_buy_per_symbol), 1))
        effective_size = max(min_position_usd, min(max_position_usd, effective_size))

    effective_size = effective_size * CAUTIOUS_MULTIPLIER  # Regime adjustment
    if not QUIET:
        print(f"  Position size per trade: ${effective_size:,.2f} (regime multiplier: {CAUTIOUS_MULTIPLIER})")
        print(f"  Trades to execute: {len(best_buy_per_symbol)} primary buys")

    # Execute BUY orders
    budget_used = 0
    for dedup_key, signal in sorted(best_buy_per_symbol.items()):
        symbol = signal["symbol"]  # Extract actual symbol from signal (dedup_key is symbol_timeframe)
        remaining = available_cash - budget_used
        if remaining < 500:
            if not QUIET:
                print(f"\n  BUDGET EXHAUSTED: ${remaining:.2f} remaining, skipping {symbol}")
            break

        # Check signal quality gate
        if signal.get("signal_type") in disabled_signals:
            if not QUIET:
                print(f"  SKIPPED: {symbol} -- signal '{signal['signal_type']}' is F-grade disabled")
            continue

        # ADR exhaustion filter -- don't chase extended daily moves
        adr_info = adr_data.get(symbol)
        if adr_info and adr_info.get("exhausted"):
            if not QUIET:
                print(f"  SKIPPED: {symbol} -- ADR exhausted ({adr_info['exhaustion_pct']:.0f}% of daily range used)")
            log_blocked_signal(symbol, signal.get("strategy", "CRYPTO_STRATEGY"), signal.get("signal_type", "unknown"),
                               "adr_exhaustion", f"ADR {adr_info['exhaustion_pct']:.0f}% > {params.get('adr_exhaustion_pct', 85)}%")
            continue

        # Use 15-min position size for 15M signals
        if signal.get("timeframe") == "15M" and params_15m:
            effective_size_for_signal = params_15m.get("position_size_usd", 500)
        else:
            effective_size_for_signal = effective_size  # existing 1H size

        # Probation signals get 50% position size; weight also scales size
        probation_multiplier = 0.5 if signal.get("signal_type") in probation_signals else 1.0
        weight = signal_weights.get(signal.get("signal_type", ""), 1.0)
        ml_conf = signal.get("ml_confidence", 0.5)
        confidence_mult = 0.5 + ml_conf  # [0.5, 1.5] range based on ML confidence
        tf_4h_mult = signal.get("tf_4h_multiplier", 1.0)
        trade_size = min(effective_size_for_signal * probation_multiplier * weight * confidence_mult * tf_4h_mult, remaining)
        if probation_multiplier < 1.0 and not QUIET:
            print(f"  PROBATION: {symbol} — signal '{signal['signal_type']}' using 50% size (${trade_size:,.2f})")
        print(f"\n  >>> Executing BUY: {symbol} (${trade_size:,.2f})")
        print(f"      Strategy: {signal['strategy']} | Signal: {signal['signal_type']}")
        print(f"      Reason: {signal['reason']}")

        # Spread validation -- skip if bid-ask spread is too wide
        spread_ok, spread_pct, midpoint = validate_spread(data_client, symbol)
        if not spread_ok:
            if not QUIET:
                print(f"  SKIPPED: {symbol} -- spread too wide ({spread_pct:.2f}%)")
            log_blocked_signal(symbol, signal.get("strategy", ""), signal.get("signal_type", ""),
                               "spread_too_wide", f"spread {spread_pct:.2f}%")
            continue

        order_result = place_crypto_buy(trading_client, symbol, trade_size, signal["strategy"], signal)

        if "error" not in order_result:
            budget_used += trade_size
            # Wait a moment for fill
            time.sleep(2)

            # Try to get fill info
            try:
                order_detail = trading_client.get_order_by_id(order_result["order_id"])
                fill_price = float(order_detail.filled_avg_price) if order_detail.filled_avg_price else signal["indicators"]["price"]
                fill_qty = float(order_detail.filled_qty) if order_detail.filled_qty else 0
                status = str(order_detail.status)
            except Exception:
                fill_price = signal["indicators"]["price"]
                fill_qty = round(trade_size / fill_price, 8)
                status = order_result.get("status", "UNKNOWN")

            trade_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": signal["strategy"],
                "action": "BUY",
                "symbol": symbol,
                "asset_class": "crypto",
                "qty": fill_qty,
                "price": fill_price,
                "notional": trade_size,
                "order_type": "market",
                "order_id": order_result.get("order_id", ""),
                "status": status,
                "indicators": signal["indicators"],
                "signal_type": signal["signal_type"],
                "signal_strength": signal.get("strength", "UNKNOWN"),
                "timeframe": signal.get("timeframe", "1H"),
                "pnl": 0,
                # Entry attribution
                "entry_quality": _score_entry_quality(signal, regime_data),
                "regime_at_entry": regime if regime_data else "UNKNOWN",
                "regime_composite_at_entry": composite if regime_data else 0,
                "signal_grade": _get_signal_grade(signal.get("signal_type"), signal_scores),
                "signal_weight": weight,
                "ml_confidence": signal.get("ml_confidence"),
            }
            trade_record["fill_price"] = fill_price
            trade_record["fill_qty"] = fill_qty
            trade_record["quote_midpoint"] = midpoint

            # Update adaptive slippage model with actual fill vs midpoint
            if midpoint and midpoint > 0:
                update_slippage_model(symbol, fill_price, midpoint)

            log_trade(trade_record)
            trades_placed.append(trade_record)
            print(f"      FILLED: {fill_qty} @ ${fill_price:,.4f} | Status: {status}")
        else:
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": signal["strategy"],
                "action": "BUY_FAILED",
                "symbol": symbol,
                "error": order_result["error"],
                "signal_type": signal["signal_type"],
                "pnl": 0,
            })

    # Execute SELL orders for existing positions
    for signal in sell_signals:
        symbol = signal["symbol"]
        if symbol in existing_positions:
            pos = existing_positions[symbol]
            # Determine sell quantity based on signal type
            if signal.get("full_close"):
                sell_qty = round(float(pos.qty), 8)  # Sell 100%
            else:
                sell_pct = 0.50 if signal.get("signal_type") == "overbought_exit" else 0.25
                sell_qty = round(float(pos.qty) * sell_pct, 8)
            if sell_qty > 0:
                sell_label = "100%" if signal.get("full_close") else ("50%" if signal.get("signal_type") == "overbought_exit" else "25%")
                print(f"\n  >>> Executing SELL: {symbol} ({sell_qty} units, {sell_label} of position)")
                print(f"      Reason: {signal['reason']}")

                order_result = place_crypto_sell(trading_client, symbol, sell_qty, signal["strategy"], signal)

                if "error" not in order_result:
                    time.sleep(2)
                    try:
                        order_detail = trading_client.get_order_by_id(order_result["order_id"])
                        fill_price = float(order_detail.filled_avg_price) if order_detail.filled_avg_price else signal["indicators"]["price"]
                        fill_qty = float(order_detail.filled_qty) if order_detail.filled_qty else sell_qty
                        status = str(order_detail.status)
                    except Exception:
                        fill_price = signal["indicators"]["price"]
                        fill_qty = sell_qty
                        status = order_result.get("status", "UNKNOWN")

                    entry_price = float(pos.avg_entry_price)
                    pnl = round((fill_price - entry_price) * fill_qty, 2)

                    trade_record = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "strategy": signal["strategy"],
                        "action": "SELL",
                        "symbol": symbol,
                        "asset_class": "crypto",
                        "qty": fill_qty,
                        "price": fill_price,
                        "order_type": "market",
                        "order_id": order_result.get("order_id", ""),
                        "status": status,
                        "indicators": signal["indicators"],
                        "signal_type": signal["signal_type"],
                        "entry_price": entry_price,
                        "pnl": pnl,
                        # Exit attribution
                        "exit_reason": "overbought_signal",
                        "entry_quality": _score_entry_quality(signal, regime_data),
                        "regime_at_exit": regime if regime_data else "UNKNOWN",
                    }
                    log_trade(trade_record)
                    trades_placed.append(trade_record)
                    print(f"      FILLED: {fill_qty} @ ${fill_price:,.4f} | PnL: ${pnl:+.2f}")

    # ---- Update state files ----
    if not QUIET:
        print("\n[5/6] Updating state files...")
    update_positions(trades_placed, trading_client)
    update_trailing_stops(trades_placed, params)

    # Update EMA dynamic trailing floors for all active crypto stops
    if not QUIET:
        print("\n[5.5/6] Updating EMA trailing floors...")
    update_ema_trail_floors(all_data, params)

    # ---- Final summary ----
    buy_trades = [t for t in trades_placed if t["action"] == "BUY"]
    sell_trades = [t for t in trades_placed if t["action"] == "SELL"]

    if QUIET and len(trades_placed) == 0:
        print(f"[QUIET] {len(watchlist)} pairs scanned, 0 signals triggered")
    else:
        print("\n" + "=" * 70)
        print("[6/6] EXECUTION SUMMARY")
        print("=" * 70)
        print(f"  Signals found:  {len(all_signals)} total ({len(buy_signals)} BUY, {len(sell_signals)} SELL)")
        print(f"  Trades placed:  {len(trades_placed)}")
        print(f"    - Buys:  {len(buy_trades)}")
        print(f"    - Sells: {len(sell_trades)}")

        total_bought = sum(t.get("notional", 0) for t in buy_trades)
        total_pnl = sum(t.get("pnl", 0) for t in sell_trades)
        print(f"  Total bought: ${total_bought:,.2f}")
        if sell_trades:
            print(f"  Total realized PnL: ${total_pnl:+,.2f}")

        print(f"\n  Trade details:")
        for t in trades_placed:
            pnl_str = f" | PnL: ${t.get('pnl', 0):+.2f}" if t.get("pnl", 0) != 0 else ""
            print(f"    {t['action']:4s} {t['symbol']:10s} | {t.get('strategy', '?'):25s} | "
                  f"qty={t.get('qty', 0):.6f} @ ${t.get('price', 0):>10,.4f} | "
                  f"{t.get('signal_type', '?')}{pnl_str}")

        # Count total trades in log
        try:
            with open(TRADES_LOG, "r") as f:
                total_log_entries = sum(1 for line in f if line.strip())
            print(f"\n  Total trades in log: {total_log_entries}")
            trade_entries = sum(1 for line in open(TRADES_LOG) if '"action"' in line and '"BUY"' in line or '"SELL"' in line)
            print(f"  Self-improvement needs: 30 trades (working toward it)")
        except Exception:
            pass

        regime_str = f"{regime} (composite: {composite})" if regime_data else "UNKNOWN"
        print(f"\n  Regime: {regime_str}")
        print(f"  Next run: Schedule this script to run every 1-4 hours for continuous crypto signals")
        print("=" * 70)

    return trades_placed


if __name__ == "__main__":
    trades = main()
