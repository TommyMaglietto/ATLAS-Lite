#!/usr/bin/env python3
"""
ATLAS Lite ML Meta-Scorer -- XGBoost confidence predictions for trade signals.

Trains an XGBoost binary classifier on historical simulated trade data and
provides probability-of-profit confidence predictions for live signals.

Usage:
    python scripts/ml_scorer.py --train              # Generate data + train model
    python scripts/ml_scorer.py --predict            # Test prediction with dummy vector
    python scripts/ml_scorer.py --retrain            # Retrain if stale / missing
"""

import json
import sys
import os
import traceback
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
STATE_DIR = PROJECT_ROOT / "state"
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"

from atomic_write import atomic_write_json, atomic_read_json

# ---------------------------------------------------------------------------
# Feature definition -- 22-element feature vector
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    # Core (6)
    "rsi", "adx", "bb_position", "atr_pct", "vwap_slope", "volume_ratio",
    # Derived (4)
    "rsi_roc", "bb_width_pct", "ema_alignment", "vwap_deviation",
    # Cross-asset BTC (4)
    "btc_rsi", "btc_adx", "btc_vwap_slope", "btc_ema_alignment",
    # Temporal (2)
    "hour_of_day", "day_of_week",
    # MACD (3) -- momentum oscillator, research top feature
    "macd", "macd_signal", "macd_histogram",
    # External data (1) -- Binance funding rate, reversal predictor
    "funding_rate",
    # Total: 20 features
]

MODEL_PATH = STATE_DIR / "ml_model.json"
TRAINING_LOG_PATH = STATE_DIR / "ml_training_log.json"

# Slippage constant matching backtester
SLIPPAGE = 0.001


# ===========================================================================
# 1. extract_features -- from live indicators dict
# ===========================================================================

def extract_features(indicators, symbol, timestamp=None, cross_asset_data=None,
                     signal_context=None):
    """
    Build 22-feature vector from a live signal's indicators dict.

    Args:
        indicators: dict with keys from generate_signals (rsi, adx, bb_position,
            ema9, ema21, ema55, atr, vwap, vwap_slope, vol_avg_5, plus derived:
            rsi_roc, bb_width_pct, volume_ratio, ema_alignment, vwap_deviation,
            atr_pct).
        symbol: crypto pair string (e.g. "BTC/USD").
        timestamp: ISO-8601 timestamp string or datetime, or None for now.
        cross_asset_data: dict with BTC indicator values (btc_rsi, btc_adx,
            btc_vwap_slope, btc_ema_alignment), or None.
        signal_context: dict with keys signal_strength ("STRONG"/"MODERATE"/
            "WEAK"), signal_weight (float), regime_composite (float),
            entry_quality_score (float).

    Returns:
        np.ndarray of shape (22,) with np.nan for unavailable values.
    """
    features = np.full(len(FEATURE_NAMES), np.nan)

    def _safe(val):
        """Convert to float, returning np.nan for None / non-finite."""
        if val is None:
            return np.nan
        try:
            f = float(val)
            return f if np.isfinite(f) else np.nan
        except (TypeError, ValueError):
            return np.nan

    # --- Core (indices 0-5) ---
    features[0] = _safe(indicators.get("rsi"))
    features[1] = _safe(indicators.get("adx"))
    features[2] = _safe(indicators.get("bb_position"))
    features[3] = _safe(indicators.get("atr_pct"))
    features[4] = _safe(indicators.get("vwap_slope"))
    features[5] = _safe(indicators.get("volume_ratio"))

    # --- Derived (indices 6-9) ---
    features[6] = _safe(indicators.get("rsi_roc"))
    features[7] = _safe(indicators.get("bb_width_pct"))
    features[8] = _safe(indicators.get("ema_alignment"))
    features[9] = _safe(indicators.get("vwap_deviation"))

    # --- Cross-asset BTC (indices 10-13) ---
    is_btc = symbol.upper().startswith("BTC")
    if is_btc:
        pass  # BTC features are NaN for BTC itself
    elif cross_asset_data is not None:
        features[10] = _safe(cross_asset_data.get("btc_rsi"))
        features[11] = _safe(cross_asset_data.get("btc_adx"))
        features[12] = _safe(cross_asset_data.get("btc_vwap_slope"))
        features[13] = _safe(cross_asset_data.get("btc_ema_alignment"))

    # --- Temporal (indices 14-15) ---
    if timestamp is not None:
        try:
            ts = pd.Timestamp(timestamp)
            features[14] = float(ts.hour)
            features[15] = float(ts.dayofweek)
        except Exception:
            pass
    else:
        now = datetime.now(timezone.utc)
        features[14] = float(now.hour)
        features[15] = float(now.weekday())

    # --- MACD (indices 16-18) ---
    features[16] = _safe(indicators.get("macd"))
    features[17] = _safe(indicators.get("macd_signal"))
    features[18] = _safe(indicators.get("macd_histogram"))

    # --- Funding rate (index 19) ---
    features[19] = _safe(indicators.get("funding_rate"))

    return features


# ===========================================================================
# 2. extract_features_from_dataframe -- for backtester training
# ===========================================================================

def extract_features_from_dataframe(df, bar_idx, symbol, cross_asset_df=None):
    """
    Extract 22-feature vector from a DataFrame row at bar_idx.

    Used during training where we have the full indicator DataFrame.
    Signal context features (indices 18-21) are set to NaN since they
    are not available in the backtester context.

    Args:
        df: DataFrame with indicator columns (output of compute_indicators).
        bar_idx: integer row index to extract.
        symbol: crypto pair string.
        cross_asset_df: optional BTC indicator DataFrame for cross-asset features.

    Returns:
        np.ndarray of shape (22,) with np.nan for missing values.
    """
    features = np.full(len(FEATURE_NAMES), np.nan)

    if bar_idx < 0 or bar_idx >= len(df):
        return features

    row = df.iloc[bar_idx]

    def _safe(col):
        if col not in df.columns:
            return np.nan
        val = row[col]
        try:
            f = float(val)
            return f if np.isfinite(f) else np.nan
        except (TypeError, ValueError):
            return np.nan

    # --- Core (0-5) ---
    features[0] = _safe("rsi")
    features[1] = _safe("adx")
    features[2] = _safe("bb_position")
    features[3] = _safe("atr_pct")
    features[4] = _safe("vwap_slope")
    features[5] = _safe("volume_ratio")

    # --- Derived (6-9) ---
    features[6] = _safe("rsi_roc")
    features[7] = _safe("bb_width_pct")
    features[8] = _safe("ema_alignment")
    features[9] = _safe("vwap_deviation")

    # --- Cross-asset BTC (10-13) ---
    is_btc = symbol.upper().startswith("BTC")
    if not is_btc and cross_asset_df is not None:
        btc_row = _find_btc_row(df, bar_idx, cross_asset_df)
        if btc_row is not None:
            def _btc_safe(col):
                val = btc_row.get(col, np.nan)
                try:
                    f = float(val)
                    return f if np.isfinite(f) else np.nan
                except (TypeError, ValueError):
                    return np.nan

            features[10] = _btc_safe("rsi")
            features[11] = _btc_safe("adx")
            features[12] = _btc_safe("vwap_slope")
            features[13] = _btc_safe("ema_alignment")

    # --- Temporal (14-15) ---
    ts = row.get("timestamp", None)
    if ts is not None:
        try:
            ts_parsed = pd.Timestamp(ts)
            features[14] = float(ts_parsed.hour)
            features[15] = float(ts_parsed.dayofweek)
        except Exception:
            pass

    # --- MACD (16-18) ---
    features[16] = _safe("macd")
    features[17] = _safe("macd_signal")
    features[18] = _safe("macd_histogram")

    # --- Funding rate (19) --- NaN in backtest (only available live from Binance)
    features[19] = _safe("funding_rate")

    return features


def _find_btc_row(source_df, bar_idx, btc_df):
    """
    Find the BTC DataFrame row matching the timestamp of source_df.iloc[bar_idx].

    Falls back to nearest-index alignment if timestamps don't match.
    Returns a pandas Series or None.
    """
    if btc_df is None or btc_df.empty:
        return None

    ts = source_df.iloc[bar_idx].get("timestamp", None)
    if ts is not None and "timestamp" in btc_df.columns:
        try:
            ts_val = pd.Timestamp(ts)
            # Find the closest BTC row by timestamp
            btc_ts = pd.to_datetime(btc_df["timestamp"])
            diffs = (btc_ts - ts_val).abs()
            closest_idx = diffs.idxmin()
            # Only use if within 2 hours
            if diffs.iloc[closest_idx] <= pd.Timedelta(hours=2):
                return btc_df.iloc[closest_idx]
        except Exception:
            pass

    # Fallback: use same proportional index
    if len(btc_df) > 0:
        ratio = bar_idx / max(len(source_df) - 1, 1)
        btc_idx = min(int(ratio * (len(btc_df) - 1)), len(btc_df) - 1)
        return btc_df.iloc[btc_idx]

    return None


# ===========================================================================
# 3. generate_training_data
# ===========================================================================

def generate_training_data(days_back=365, symbols=None, params=None):
    """
    Generate labeled training data from historical bars.

    Walks through historical bars, fires generate_signals in dry_run mode,
    captures features at BUY entries, then labels by simulated PnL.

    Args:
        days_back: calendar days of history to fetch (default 365).
        symbols: list of crypto pairs (default: watchlist from config).
        params: strategy params dict (default: loaded from config).

    Returns:
        (X, y, metadata) where:
            X: np.ndarray shape (n_samples, 22)
            y: np.ndarray shape (n_samples,) with 1=profitable, 0=loss
            metadata: list of dicts with symbol, timestamp, pnl, signal_type
    """
    # Lazy imports to avoid circular dependency at module level
    import crypto_strategy
    crypto_strategy.QUIET = True
    from crypto_strategy import compute_indicators, generate_signals, load_params
    from backtester import fetch_historical_bars

    if params is None:
        params = load_params()

    if symbols is None:
        symbols = params.get(
            "watchlist",
            ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"],
        )

    print(f"Fetching {days_back} days of history for {len(symbols)} symbols...")
    all_bars = fetch_historical_bars(symbols, days_back)

    # Always fetch BTC for cross-asset features
    btc_df = None
    if "BTC/USD" in all_bars:
        btc_df = all_bars["BTC/USD"]
    elif "BTC/USD" not in symbols:
        btc_bars = fetch_historical_bars(["BTC/USD"], days_back)
        btc_df = btc_bars.get("BTC/USD")

    # Compute indicators on BTC for cross-asset
    btc_indicator_df = None
    if btc_df is not None and len(btc_df) > 0:
        btc_indicator_df = compute_indicators(btc_df.copy(), params)

    X_list = []
    y_list = []
    meta_list = []

    # Trailing stop / profit config
    trail_pct = params.get("trail_pct", 5.0)
    loss_pct = params.get("loss_pct", 5.0)
    profit_tiers = [
        {"gain_pct": 3, "sell_pct": 8},
        {"gain_pct": 5, "sell_pct": 10},
        {"gain_pct": 10, "sell_pct": 15},
        {"gain_pct": 20, "sell_pct": 20},
    ]

    ema_long = params.get("shared", params).get("ema_long", 55)
    warmup = ema_long + 10

    for symbol in symbols:
        if symbol not in all_bars:
            print(f"  [{symbol}] No data, skipping")
            continue

        df = all_bars[symbol]
        if len(df) <= warmup + 20:
            print(f"  [{symbol}] Too few bars ({len(df)}), skipping")
            continue

        # Compute indicators on full DataFrame
        df = compute_indicators(df.copy(), params)

        # Lightweight position simulation for labeling
        position = None  # {entry_price, entry_idx, highest_price, floor_price,
                         #  features, signal_type, timestamp}
        sym_trades = 0

        for i in range(warmup, len(df)):
            bar = df.iloc[i]
            price = float(bar["close"])
            bar_time = bar.get("timestamp", None)

            # --- Update position and check exits ---
            if position is not None:
                # Ratchet trailing stop
                if price > position["highest_price"]:
                    position["highest_price"] = price
                    new_floor = price * (1 - trail_pct / 100)
                    if new_floor > position["floor_price"]:
                        position["floor_price"] = new_floor

                # Check stop
                if price <= position["floor_price"]:
                    fill = price * (1 - SLIPPAGE)
                    pnl = (fill - position["entry_price"]) / position["entry_price"]
                    _record_sample(X_list, y_list, meta_list, position, pnl,
                                   symbol, "trailing_stop")
                    sym_trades += 1
                    position = None
                    continue

                # Check profit tiers -- if highest gain ever reached a tier,
                # we approximate: if total weighted gain > 0, it is profitable
                gain_pct = ((price - position["entry_price"]) /
                            position["entry_price"]) * 100
                # Simple exit after 72 bars (~3 days on 1H) if still holding
                bars_held = i - position["entry_idx"]
                if bars_held >= 72:
                    fill = price * (1 - SLIPPAGE)
                    pnl = (fill - position["entry_price"]) / position["entry_price"]
                    _record_sample(X_list, y_list, meta_list, position, pnl,
                                   symbol, "time_exit")
                    sym_trades += 1
                    position = None
                    continue

            # --- Generate signals on slice up to current bar ---
            if position is None:
                df_slice = df.iloc[:i + 1]
                signals = generate_signals(df_slice, symbol, params, dry_run=True)

                for sig in signals:
                    if sig.get("action") != "BUY":
                        continue
                    # Skip DCA signals (need existing position)
                    if sig.get("signal_type") == "dca_technical_trigger":
                        continue

                    # Capture features at entry
                    feat = extract_features_from_dataframe(
                        df, i, symbol, cross_asset_df=btc_indicator_df
                    )

                    fill_price = price * (1 + SLIPPAGE)
                    floor_val = fill_price * (1 - loss_pct / 100)
                    position = {
                        "entry_price": fill_price,
                        "entry_idx": i,
                        "highest_price": fill_price,
                        "floor_price": floor_val,
                        "features": feat,
                        "signal_type": sig.get("signal_type", "unknown"),
                        "timestamp": str(bar_time) if bar_time is not None else "",
                    }
                    break  # one entry per bar

        # Close any remaining position at last bar
        if position is not None:
            last_price = float(df.iloc[-1]["close"])
            fill = last_price * (1 - SLIPPAGE)
            pnl = (fill - position["entry_price"]) / position["entry_price"]
            _record_sample(X_list, y_list, meta_list, position, pnl,
                           symbol, "end_of_data")
            sym_trades += 1

        print(f"  [{symbol}] {sym_trades} trades captured from {len(df)} bars")

    if not X_list:
        print("WARNING: No training samples generated")
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0), []

    X = np.vstack(X_list)
    y = np.array(y_list, dtype=np.float32)
    return X, y, meta_list


def _record_sample(X_list, y_list, meta_list, position, pnl_pct, symbol, exit_reason):
    """Helper to record one labeled training sample."""
    X_list.append(position["features"].reshape(1, -1))
    # 3-class labeling: 0=loss (<-0.5%), 1=scratch (-0.5% to +0.5%), 2=win (>+0.5%)
    if pnl_pct > 0.005:
        y_list.append(2.0)  # Win: genuine profit
    elif pnl_pct < -0.005:
        y_list.append(0.0)  # Loss: real loss
    else:
        y_list.append(1.0)  # Scratch: noise/break-even
    meta_list.append({
        "symbol": symbol,
        "timestamp": position["timestamp"],
        "pnl_pct": round(float(pnl_pct) * 100, 4),
        "signal_type": position["signal_type"],
        "exit_reason": exit_reason,
    })


# ===========================================================================
# 4. train_model
# ===========================================================================

def train_model(X, y, model_path=None):
    """
    Train XGBoost binary classifier and save to disk.

    Performs 5-fold stratified cross-validation to measure true performance,
    then trains final model on all data.

    Args:
        X: np.ndarray shape (n_samples, 22).
        y: np.ndarray shape (n_samples,) with 0/1 labels.
        model_path: Path to save the model (default: STATE_DIR/ml_model.json).

    Returns:
        dict with cv_accuracy, cv_auc, feature_importances, n_samples,
        training_date, feature_names.
    """
    import xgboost as xgb
    from sklearn.model_selection import TimeSeriesSplit, cross_val_predict
    from sklearn.metrics import accuracy_score, roc_auc_score

    if model_path is None:
        model_path = MODEL_PATH

    # 3-class distribution
    n_loss = int((y == 0).sum())
    n_scratch = int((y == 1).sum())
    n_win = int((y == 2).sum())

    xgb_params = {
        "max_depth": 4,
        "learning_rate": 0.1,
        "n_estimators": 100,
        "min_child_weight": 5,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "objective": "multi:softprob",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "use_label_encoder": False,
        "verbosity": 0,
        "random_state": 42,
    }

    model = xgb.XGBClassifier(**xgb_params)

    # 5-fold stratified cross-validation
    cv = TimeSeriesSplit(n_splits=5)  # Time-series aware CV, no look-ahead leakage

    cv_accuracies = []
    cv_aucs = []
    cv_probas = np.zeros((len(y), 3))  # 3-class probabilities

    for fold_idx, (train_idx, val_idx) in enumerate(cv.split(X, y)):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        fold_model = xgb.XGBClassifier(**xgb_params)
        fold_model.fit(X_train, y_train, verbose=False)

        preds = fold_model.predict(X_val)
        probas = fold_model.predict_proba(X_val)  # Shape (n, 3) for 3-class

        cv_accuracies.append(accuracy_score(y_val, preds))
        if len(set(y_val)) >= 2:
            try:
                cv_aucs.append(roc_auc_score(y_val, probas, multi_class="ovr"))
            except ValueError:
                pass
        cv_probas[val_idx] = probas

    cv_accuracy = float(np.mean(cv_accuracies))
    cv_auc = float(np.mean(cv_aucs)) if cv_aucs else 0.5

    # Train final model on all data
    model.fit(X, y, verbose=False)

    # Feature importances
    importances = model.feature_importances_
    feat_imp = {
        name: round(float(imp), 6)
        for name, imp in zip(FEATURE_NAMES, importances)
    }

    # Save model
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))

    # Save training metadata
    training_date = datetime.now(timezone.utc).isoformat()
    metadata = {
        "cv_accuracy": round(cv_accuracy, 4),
        "cv_auc": round(cv_auc, 4),
        "feature_importances": feat_imp,
        "n_samples": int(len(y)),
        "n_loss": n_loss,
        "n_scratch": n_scratch,
        "n_win": n_win,
        "num_classes": 3,
        "training_date": training_date,
        "feature_names": FEATURE_NAMES,
        "xgb_params": {k: v for k, v in xgb_params.items()
                       if k not in ("use_label_encoder",)},
        "model_path": str(model_path),
    }
    atomic_write_json(str(TRAINING_LOG_PATH), metadata)

    result = {
        "cv_accuracy": round(cv_accuracy, 4),
        "cv_auc": round(cv_auc, 4),
        "feature_importances": feat_imp,
        "n_samples": int(len(y)),
        "training_date": training_date,
        "feature_names": FEATURE_NAMES,
    }

    if cv_auc < 0.55:
        print(f"WARNING: Low CV AUC ({cv_auc:.3f}) -- model may not be predictive")

    return result


# ===========================================================================
# 5. load_model
# ===========================================================================

def load_model(model_path=None):
    """
    Load a trained XGBoost model and its metadata.

    Args:
        model_path: Path to model file (default: STATE_DIR/ml_model.json).

    Returns:
        (model, metadata_dict) tuple, or None if model or metadata missing/corrupt.
    """
    import xgboost as xgb

    if model_path is None:
        model_path = MODEL_PATH

    model_path = Path(model_path)

    if not model_path.exists():
        return None

    metadata = atomic_read_json(str(TRAINING_LOG_PATH))
    if metadata is None:
        return None

    try:
        model = xgb.XGBClassifier()
        model.load_model(str(model_path))
        return (model, metadata)
    except Exception as e:
        print(f"ERROR: Failed to load ML model: {e}", file=sys.stderr)
        return None


# ===========================================================================
# 6. predict_confidence
# ===========================================================================

def predict_confidence(model, features):
    """
    Predict probability that a trade will be profitable.

    Args:
        model: trained XGBoost classifier.
        features: np.ndarray of shape (22,) -- single sample.

    Returns:
        float in [0.0, 1.0] representing P(profitable). Returns 0.5 (neutral)
        on any error.
    """
    try:
        X = features.reshape(1, -1)
        proba = model.predict_proba(X)
        if proba.shape[1] == 3:
            return float(proba[0, 2])  # P(win class) for 3-class model
        return float(proba[0, 1])  # Backward compat for binary model
    except Exception:
        return 0.5


# ===========================================================================
# 7. retrain_if_needed
# ===========================================================================

def retrain_if_needed(force=False):
    """
    Retrain the model if conditions are met.

    Conditions checked:
        (a) model file does not exist
        (b) model is older than 7 days
        (c) 50+ new trades since last training

    Args:
        force: if True, retrain regardless of conditions.

    Returns:
        Training metrics dict if retrained, or None if skipped.
    """
    should_retrain = force
    reason = "forced" if force else ""

    if not should_retrain:
        # (a) Model doesn't exist
        if not MODEL_PATH.exists():
            should_retrain = True
            reason = "model not found"

    if not should_retrain:
        # (b) Model older than 7 days
        metadata = atomic_read_json(str(TRAINING_LOG_PATH))
        if metadata and "training_date" in metadata:
            try:
                train_date = datetime.fromisoformat(metadata["training_date"])
                age = datetime.now(timezone.utc) - train_date
                if age > timedelta(days=7):
                    should_retrain = True
                    reason = f"model is {age.days} days old"
            except (ValueError, TypeError):
                should_retrain = True
                reason = "invalid training date"
        else:
            should_retrain = True
            reason = "no training metadata"

    if not should_retrain:
        # (c) 50+ new trades since training
        metadata = atomic_read_json(str(TRAINING_LOG_PATH))
        training_date_str = metadata.get("training_date", "") if metadata else ""
        new_trade_count = _count_trades_since(training_date_str)
        if new_trade_count >= 50:
            should_retrain = True
            reason = f"{new_trade_count} new trades since last training"

    if not should_retrain:
        return None

    print(f"Retraining ML model (reason: {reason})...")

    try:
        X, y, meta = generate_training_data(days_back=90)
        if len(y) < 10:
            print("WARNING: Too few samples for training, skipping")
            return None

        print(f"Samples: {len(y)}, Win: {(y == 2).sum()}/{len(y)} ({(y == 2).mean():.1%}), Scratch: {(y == 1).sum()}, Loss: {(y == 0).sum()}")
        result = train_model(X, y)
        print(f"Training complete -- CV AUC: {result['cv_auc']:.3f}")

        if result["cv_auc"] < 0.55:
            print("WARNING: AUC < 0.55 -- model saved but may not be predictive")

        return result

    except Exception as e:
        print(f"ERROR during retraining: {e}", file=sys.stderr)
        traceback.print_exc()
        return None


def _count_trades_since(date_str):
    """Count trades in trades.jsonl after the given ISO date string."""
    trades_file = LOGS_DIR / "trades.jsonl"
    if not trades_file.exists():
        return 0

    if not date_str:
        return 999  # Force retrain if no date

    count = 0
    try:
        cutoff = datetime.fromisoformat(date_str)
        with open(trades_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trade = json.loads(line)
                    ts = trade.get("timestamp", "")
                    if ts and ts > date_str:
                        count += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        return 0

    return count


# ===========================================================================
# CLI entry point
# ===========================================================================

if __name__ == "__main__":
    if "--train" in sys.argv:
        print("Generating training data...")
        X, y, meta = generate_training_data(days_back=90)
        if len(y) == 0:
            print("No training samples generated. Check data availability.")
            sys.exit(1)
        print(f"Samples: {len(y)}, Features: {X.shape[1]}, Win: {(y == 2).sum()}/{len(y)} ({(y == 2).mean():.1%}), Scratch: {(y == 1).sum()}, Loss: {(y == 0).sum()}")
        print("Training model...")
        result = train_model(X, y)
        print(f"CV Accuracy: {result['cv_accuracy']:.3f}")
        print(f"CV AUC: {result['cv_auc']:.3f}")
        top_feats = sorted(
            result["feature_importances"].items(), key=lambda x: -x[1]
        )[:5]
        print(f"Top features: {top_feats}")

    elif "--predict" in sys.argv:
        model_result = load_model()
        if model_result:
            model, meta = model_result
            # Create a dummy feature vector for testing
            test_features = np.full(len(FEATURE_NAMES), 0.5)
            conf = predict_confidence(model, test_features)
            print(f"Test prediction confidence: {conf:.3f}")
            print(f"Model trained on: {meta.get('training_date', 'unknown')}")
            print(f"Model AUC: {meta.get('cv_auc', 'unknown')}")
        else:
            print("No model available. Run --train first.")

    elif "--retrain" in sys.argv:
        result = retrain_if_needed(force="--force" in sys.argv)
        if result:
            print(f"Retrained successfully. AUC: {result['cv_auc']:.3f}")
        else:
            print("Retrain skipped (conditions not met, or use --force).")

    else:
        print("Usage: python ml_scorer.py --train | --predict | --retrain [--force]")
