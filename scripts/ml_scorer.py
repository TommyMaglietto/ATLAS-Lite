#!/usr/bin/env python3
"""
ATLAS Lite ML Meta-Scorer -- Stacking ensemble confidence predictions for trade signals.

Trains a stacking ensemble (XGBoost + optional LightGBM/CatBoost, meta-learner LR)
on triple-barrier-labeled historical data with Boruta feature selection and
purged k-fold cross-validation.

Usage:
    python scripts/ml_scorer.py --train              # Generate data + train model
    python scripts/ml_scorer.py --predict            # Test prediction with dummy vector
    python scripts/ml_scorer.py --retrain            # Retrain if stale / missing
"""

import json
import sys
import os
import traceback
import joblib
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
# Feature definition -- 21-element feature vector
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
    # Symbol identifier (1) -- integer-encoded symbol for per-asset bias
    "symbol_id",
    # Total: 21 features
]

MODEL_PATH = STATE_DIR / "ml_model.joblib"
MODEL_PATH_LEGACY = STATE_DIR / "ml_model.json"
TRAINING_LOG_PATH = STATE_DIR / "ml_training_log.json"

# Slippage constant matching backtester
SLIPPAGE = 0.001


# ===========================================================================
# 1. extract_features -- from live indicators dict
# ===========================================================================

def extract_features(indicators, symbol, timestamp=None, cross_asset_data=None,
                     signal_context=None, symbol_id=None):
    """
    Build 21-feature vector from a live signal's indicators dict.

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
        symbol_id: integer-encoded symbol identifier, or None.

    Returns:
        np.ndarray of shape (21,) with np.nan for unavailable values.
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

    # --- Symbol ID (index 20) ---
    if symbol_id is not None:
        features[20] = float(symbol_id)

    return features


# ===========================================================================
# 2. extract_features_from_dataframe -- for backtester training
# ===========================================================================

def extract_features_from_dataframe(df, bar_idx, symbol, cross_asset_df=None,
                                    symbol_id=None):
    """
    Extract 21-feature vector from a DataFrame row at bar_idx.

    Used during training where we have the full indicator DataFrame.

    Args:
        df: DataFrame with indicator columns (output of compute_indicators).
        bar_idx: integer row index to extract.
        symbol: crypto pair string.
        cross_asset_df: optional BTC indicator DataFrame for cross-asset features.
        symbol_id: integer-encoded symbol identifier, or None.

    Returns:
        np.ndarray of shape (21,) with np.nan for missing values.
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

    # --- Symbol ID (20) ---
    if symbol_id is not None:
        features[20] = float(symbol_id)

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
# 3. Triple barrier labeling
# ===========================================================================

def _triple_barrier_label(df, entry_idx, entry_price, atr_at_entry,
                          tp_atr_mult=2.0, sl_atr_mult=2.0, max_bars=12):
    """
    Label a trade entry using the triple-barrier method.

    Barriers:
        - Upper: entry_price + tp_atr_mult * ATR  (take-profit)
        - Lower: entry_price - sl_atr_mult * ATR  (stop-loss)
        - Vertical: max_bars forward                (time exit)

    Returns:
        (label, exit_idx) where label is 2=WIN, 0=LOSS, 1=SCRATCH.
    """
    upper = entry_price + (tp_atr_mult * atr_at_entry)
    lower = entry_price - (sl_atr_mult * atr_at_entry)

    for j in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(df))):
        bar = df.iloc[j]
        if float(bar["high"]) >= upper:
            return 2, j  # WIN
        if float(bar["low"]) <= lower:
            return 0, j  # LOSS

    final_idx = min(entry_idx + max_bars, len(df) - 1)
    final_price = float(df.iloc[final_idx]["close"])
    pnl_pct = (final_price - entry_price) / entry_price
    if pnl_pct > 0.002:
        return 2, final_idx
    elif pnl_pct < -0.002:
        return 0, final_idx
    return 1, final_idx


# ===========================================================================
# 4. generate_training_data
# ===========================================================================

def generate_training_data(days_back=180, symbols=None, params=None,
                           tp_atr_mult=2.0, sl_atr_mult=2.0, max_bars=12):
    """
    Generate labeled training data from historical bars using triple-barrier labeling.

    Walks through historical bars, fires generate_signals in dry_run mode,
    captures features at BUY entries, then labels by triple-barrier outcome.

    Args:
        days_back: calendar days of history to fetch (default 180).
        symbols: list of crypto pairs (default: watchlist from config).
        params: strategy params dict (default: loaded from config).
        tp_atr_mult: ATR multiplier for take-profit barrier (default 2.0).
        sl_atr_mult: ATR multiplier for stop-loss barrier (default 2.0).
        max_bars: maximum bars before vertical barrier (default 12).

    Returns:
        (X, y, metadata) where:
            X: np.ndarray shape (n_samples, 21)
            y: np.ndarray shape (n_samples,) with 0=LOSS, 1=SCRATCH, 2=WIN
            metadata: list of dicts with symbol, timestamp, label, signal_type
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

    # Build symbol-to-id mapping before symbol loop
    symbol_to_id = {s: idx for idx, s in enumerate(sorted(symbols))}

    X_list = []
    y_list = []
    meta_list = []

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

        sym_id = symbol_to_id.get(symbol, 0)
        sym_samples = 0
        i = warmup

        while i < len(df) - max_bars:
            bar = df.iloc[i]
            price = float(bar["close"])
            bar_time = bar.get("timestamp", None)

            # Generate signals on slice up to current bar
            df_slice = df.iloc[:i + 1]
            signals = generate_signals(df_slice, symbol, params, dry_run=True)

            found_buy = False
            for sig in signals:
                if sig.get("action") != "BUY":
                    continue
                # Skip DCA signals (need existing position)
                if sig.get("signal_type") == "dca_technical_trigger":
                    continue

                # Get ATR at entry for barrier sizing
                atr_val = bar.get("atr", np.nan)
                if atr_val is None or (isinstance(atr_val, float) and np.isnan(atr_val)):
                    # Fallback: use atr_pct * price
                    atr_pct = bar.get("atr_pct", 0.02)
                    try:
                        atr_val = float(atr_pct) * price
                    except (TypeError, ValueError):
                        atr_val = price * 0.02
                else:
                    atr_val = float(atr_val)

                if atr_val <= 0:
                    atr_val = price * 0.02

                # Extract features at entry
                feat = extract_features_from_dataframe(
                    df, i, symbol, cross_asset_df=btc_indicator_df,
                    symbol_id=sym_id,
                )

                # Triple barrier label
                label, exit_idx = _triple_barrier_label(
                    df, i, price, atr_val,
                    tp_atr_mult=tp_atr_mult,
                    sl_atr_mult=sl_atr_mult,
                    max_bars=max_bars,
                )

                X_list.append(feat.reshape(1, -1))
                y_list.append(float(label))
                meta_list.append({
                    "symbol": symbol,
                    "timestamp": str(bar_time) if bar_time is not None else "",
                    "label": int(label),
                    "signal_type": sig.get("signal_type", "unknown"),
                    "exit_idx": int(exit_idx),
                    "atr_at_entry": round(atr_val, 6),
                })
                sym_samples += 1

                # Skip to exit_idx to avoid overlapping labels
                i = exit_idx
                found_buy = True
                break  # one entry per bar

            if not found_buy:
                i += 1

        print(f"  [{symbol}] {sym_samples} samples captured from {len(df)} bars")

    if not X_list:
        print("WARNING: No training samples generated")
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0), []

    X = np.vstack(X_list)
    y = np.array(y_list, dtype=np.float32)
    return X, y, meta_list


def _record_sample(X_list, y_list, meta_list, position, pnl_pct, symbol, exit_reason):
    """Helper to record one labeled training sample (legacy, kept for compat)."""
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
# 5. Boruta feature selection
# ===========================================================================

def select_features_boruta(X, y, feature_names=None, max_iter=100):
    """
    Run Boruta feature selection to identify statistically relevant features.

    Args:
        X: np.ndarray shape (n_samples, n_features).
        y: np.ndarray shape (n_samples,) with class labels.
        feature_names: list of feature name strings (default: FEATURE_NAMES).
        max_iter: maximum Boruta iterations (default 100).

    Returns:
        (selected_mask, selected_names, ranking) where:
            selected_mask: boolean np.ndarray or None if too few selected.
            selected_names: list of selected feature name strings.
            ranking: np.ndarray of Boruta ranking per feature.
    """
    from boruta import BorutaPy
    from sklearn.ensemble import RandomForestClassifier

    if feature_names is None:
        feature_names = FEATURE_NAMES

    # Replace NaN with column median (RF can't handle NaN)
    X_clean = X.copy()
    for col_idx in range(X_clean.shape[1]):
        col = X_clean[:, col_idx]
        nan_mask = np.isnan(col)
        if nan_mask.any():
            median_val = np.nanmedian(col)
            X_clean[nan_mask, col_idx] = median_val if np.isfinite(median_val) else 0.0

    rf = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42, n_jobs=-1)
    boruta = BorutaPy(rf, n_estimators='auto', max_iter=max_iter, random_state=42, verbose=0)
    boruta.fit(X_clean, y.astype(int))

    selected_mask = boruta.support_ | boruta.support_weak_
    selected_names = [f for f, s in zip(feature_names, selected_mask) if s]

    if len(selected_names) < 3:
        return None, feature_names, boruta.ranking_  # Too few, use all

    return selected_mask, selected_names, boruta.ranking_


# ===========================================================================
# 6. Purged k-fold cross-validation
# ===========================================================================

def purged_kfold_split(n_samples, n_splits=5, purge_bars=12, embargo_pct=0.01):
    """
    Generate purged k-fold splits that prevent look-ahead bias in time-series.

    Purges samples near the train/test boundary and embargoes samples after
    the test set to prevent information leakage.

    Args:
        n_samples: total number of samples.
        n_splits: number of CV folds (default 5).
        purge_bars: number of bars to purge around test boundaries (default 12).
        embargo_pct: fraction of samples to embargo after test set (default 0.01).

    Yields:
        (train_idx, test_idx) np.ndarray tuples.
    """
    indices = np.arange(n_samples)
    fold_size = n_samples // n_splits
    embargo_size = max(1, int(n_samples * embargo_pct))

    for i in range(n_splits):
        test_start = i * fold_size
        test_end = (i + 1) * fold_size if i < n_splits - 1 else n_samples
        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[test_start:test_end] = False
        purge_start = max(0, test_start - purge_bars)
        purge_end = min(n_samples, test_end + purge_bars)
        train_mask[purge_start:purge_end] = False
        embargo_end = min(n_samples, test_end + embargo_size)
        train_mask[test_end:embargo_end] = False
        train_idx = indices[train_mask]
        test_idx = indices[test_start:test_end]
        if len(train_idx) < 20 or len(test_idx) < 5:
            continue
        yield train_idx, test_idx


from sklearn.model_selection import BaseCrossValidator


class PurgedKFold(BaseCrossValidator):
    """Sklearn-compatible purged k-fold cross-validator."""

    def __init__(self, n_splits=5, purge_bars=12, embargo_pct=0.01):
        self.n_splits = n_splits
        self.purge_bars = purge_bars
        self.embargo_pct = embargo_pct

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_splits

    def split(self, X, y=None, groups=None):
        n_samples = len(X) if hasattr(X, '__len__') else X.shape[0]
        yield from purged_kfold_split(n_samples, self.n_splits, self.purge_bars, self.embargo_pct)


# ===========================================================================
# 7. Stacking ensemble builder
# ===========================================================================

def build_stacking_model(n_classes=3):
    """
    Build a stacking ensemble classifier.

    Base learners: XGBoost (always), LightGBM (if installed), CatBoost (if installed).
    Meta learner: Logistic Regression with purged k-fold CV.

    Args:
        n_classes: number of target classes (default 3).

    Returns:
        StackingClassifier instance.
    """
    import xgboost as xgb
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import StackingClassifier

    base_learners = [
        ('xgb', xgb.XGBClassifier(
            max_depth=3, learning_rate=0.1, n_estimators=100,
            min_child_weight=5, subsample=0.8, colsample_bytree=0.8,
            objective='multi:softprob', num_class=n_classes,
            eval_metric='mlogloss', verbosity=0, random_state=42,
        )),
    ]

    try:
        import lightgbm as lgb
        base_learners.append(('lgbm', lgb.LGBMClassifier(
            max_depth=7, learning_rate=0.05, n_estimators=150,
            objective='multiclass', num_class=n_classes,
            verbosity=-1, random_state=42, n_jobs=-1,
        )))
    except ImportError:
        pass

    try:
        import catboost as cb
        base_learners.append(('catboost', cb.CatBoostClassifier(
            depth=5, learning_rate=0.08, iterations=100,
            loss_function='MultiClass', verbose=0, random_seed=42,
        )))
    except ImportError:
        pass

    meta_learner = LogisticRegression(max_iter=1000, C=1.0, solver='lbfgs',
                                      random_state=42)

    return StackingClassifier(
        estimators=base_learners,
        final_estimator=meta_learner,
        cv=PurgedKFold(n_splits=5, purge_bars=12, embargo_pct=0.01),
        stack_method='predict_proba',
        n_jobs=-1,
    )


# ===========================================================================
# 8. train_model
# ===========================================================================

def train_model(X, y, model_path=None, feature_mask=None,
                selected_feature_names=None):
    """
    Train stacking ensemble classifier and save to disk via joblib.

    Uses purged k-fold cross-validation for evaluation, then trains final
    model on all data.

    Args:
        X: np.ndarray shape (n_samples, n_features).
        y: np.ndarray shape (n_samples,) with 0/1/2 labels.
        model_path: Path to save the model (default: STATE_DIR/ml_model.joblib).
        feature_mask: boolean np.ndarray for Boruta-selected features, or None.
        selected_feature_names: list of selected feature names, or None.

    Returns:
        dict with cv_accuracy, cv_auc, n_samples, training_date, feature_names.
    """
    from sklearn.metrics import accuracy_score, roc_auc_score

    if model_path is None:
        model_path = MODEL_PATH

    # Apply feature mask if provided
    if feature_mask is not None:
        X_train = X[:, feature_mask]
        train_feature_names = selected_feature_names or [
            f for f, m in zip(FEATURE_NAMES, feature_mask) if m
        ]
    else:
        X_train = X
        train_feature_names = selected_feature_names or FEATURE_NAMES

    # 3-class distribution
    n_loss = int((y == 0).sum())
    n_scratch = int((y == 1).sum())
    n_win = int((y == 2).sum())

    # Build stacking model
    model = build_stacking_model(n_classes=3)

    # Purged k-fold cross-validation for evaluation
    cv = PurgedKFold(n_splits=5, purge_bars=12, embargo_pct=0.01)

    cv_accuracies = []
    cv_aucs = []

    for fold_idx, (train_idx, val_idx) in enumerate(cv.split(X_train, y)):
        X_tr, X_val = X_train[train_idx], X_train[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]

        # Need at least 2 classes in training fold
        if len(set(y_tr.astype(int))) < 2:
            continue

        try:
            fold_model = build_stacking_model(n_classes=3)
            fold_model.fit(X_tr, y_tr)

            preds = fold_model.predict(X_val)
            probas = fold_model.predict_proba(X_val)

            cv_accuracies.append(accuracy_score(y_val, preds))
            if len(set(y_val.astype(int))) >= 2:
                try:
                    cv_aucs.append(roc_auc_score(y_val, probas, multi_class="ovr"))
                except ValueError:
                    pass
        except Exception as e:
            print(f"  Fold {fold_idx} failed: {e}")
            continue

    cv_accuracy = float(np.mean(cv_accuracies)) if cv_accuracies else 0.0
    cv_auc = float(np.mean(cv_aucs)) if cv_aucs else 0.5

    # Train final model on all data
    model.fit(X_train, y)

    # Save model via joblib
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, str(model_path))

    # Save training metadata
    training_date = datetime.now(timezone.utc).isoformat()
    metadata = {
        "cv_accuracy": round(cv_accuracy, 4),
        "cv_auc": round(cv_auc, 4),
        "n_samples": int(len(y)),
        "n_loss": n_loss,
        "n_scratch": n_scratch,
        "n_win": n_win,
        "num_classes": 3,
        "training_date": training_date,
        "feature_names": train_feature_names,
        "selected_feature_mask": feature_mask.tolist() if feature_mask is not None else None,
        "model_type": "stacking_ensemble",
        "cv_method": "purged_kfold",
        "model_path": str(model_path),
    }
    atomic_write_json(str(TRAINING_LOG_PATH), metadata)

    result = {
        "cv_accuracy": round(cv_accuracy, 4),
        "cv_auc": round(cv_auc, 4),
        "n_samples": int(len(y)),
        "training_date": training_date,
        "feature_names": train_feature_names,
    }

    if cv_auc < 0.55:
        print(f"WARNING: Low CV AUC ({cv_auc:.3f}) -- model may not be predictive")

    return result


# ===========================================================================
# 9. load_model
# ===========================================================================

def load_model(model_path=None):
    """
    Load a trained model and its metadata.

    Tries joblib first, falls back to legacy XGBoost .json format.

    Args:
        model_path: Path to model file (default: tries .joblib then .json).

    Returns:
        (model, metadata_dict) tuple, or None if model or metadata missing/corrupt.
        metadata includes '_feature_mask' key (np.ndarray or None).
    """
    if model_path is None:
        # Try joblib first, then legacy
        if MODEL_PATH.exists():
            model_path = MODEL_PATH
        elif MODEL_PATH_LEGACY.exists():
            model_path = MODEL_PATH_LEGACY
        else:
            return None
    else:
        model_path = Path(model_path)

    if not model_path.exists():
        return None

    metadata = atomic_read_json(str(TRAINING_LOG_PATH))
    if metadata is None:
        return None

    try:
        if str(model_path).endswith(".joblib"):
            # Ensure PurgedKFold is discoverable during unpickling
            import __main__
            if not hasattr(__main__, 'PurgedKFold'):
                __main__.PurgedKFold = PurgedKFold
            model = joblib.load(str(model_path))
        else:
            # Legacy XGBoost .json format
            import xgboost as xgb
            model = xgb.XGBClassifier()
            model.load_model(str(model_path))

        # Extract feature mask from metadata
        raw_mask = metadata.get("selected_feature_mask", None)
        if raw_mask is not None:
            metadata["_feature_mask"] = np.array(raw_mask, dtype=bool)
        else:
            metadata["_feature_mask"] = None

        return (model, metadata)
    except Exception as e:
        print(f"ERROR: Failed to load ML model: {e}", file=sys.stderr)
        return None


# ===========================================================================
# 10. predict_confidence
# ===========================================================================

def predict_confidence(model, features, feature_mask=None):
    """
    Predict probability that a trade will be profitable.

    Args:
        model: trained classifier (stacking or XGBoost).
        features: np.ndarray of shape (21,) -- single sample.
        feature_mask: boolean np.ndarray for Boruta-selected features, or None.

    Returns:
        float in [0.0, 1.0] representing P(profitable). Returns 0.5 (neutral)
        on any error.
    """
    try:
        X = features.reshape(1, -1)
        if feature_mask is not None:
            X = X[:, feature_mask]
        proba = model.predict_proba(X)
        if proba.shape[1] == 3:
            return float(proba[0, 2])  # P(win class) for 3-class model
        return float(proba[0, 1])  # Backward compat for binary model
    except Exception:
        return 0.5


# ===========================================================================
# 11. retrain_if_needed
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
        if not MODEL_PATH.exists() and not MODEL_PATH_LEGACY.exists():
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
        X, y, meta = generate_training_data(days_back=180, tp_atr_mult=2.0,
                                            sl_atr_mult=2.0, max_bars=12)
        if len(y) < 10:
            print("WARNING: Too few samples for training, skipping")
            return None

        print(f"Samples: {len(y)}, Win: {(y == 2).sum()}/{len(y)} ({(y == 2).mean():.1%}), Scratch: {(y == 1).sum()}, Loss: {(y == 0).sum()}")

        # Boruta feature selection
        selected_mask = None
        selected_names = FEATURE_NAMES
        try:
            selected_mask, selected_names, ranking = select_features_boruta(X, y)
            if selected_mask is not None:
                print(f"Boruta selected {sum(selected_mask)}/{len(FEATURE_NAMES)} features: {selected_names}")
            else:
                print("Boruta: too few features selected, using all")
        except ImportError:
            print("Boruta not installed, using all features")
            selected_mask = None
            selected_names = FEATURE_NAMES

        result = train_model(X, y, feature_mask=selected_mask,
                             selected_feature_names=selected_names)
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
        print("Generating training data (triple-barrier, 180 days)...")
        X, y, meta = generate_training_data(days_back=180, tp_atr_mult=2.0,
                                            sl_atr_mult=2.0, max_bars=12)
        if len(y) == 0:
            print("No training samples generated. Check data availability.")
            sys.exit(1)
        print(f"Samples: {len(y)}, Features: {X.shape[1]}, Win: {(y == 2).sum()}/{len(y)} ({(y == 2).mean():.1%}), Scratch: {(y == 1).sum()}, Loss: {(y == 0).sum()}")

        # Boruta feature selection
        selected_mask = None
        selected_names = FEATURE_NAMES
        try:
            print("Running Boruta feature selection...")
            selected_mask, selected_names, ranking = select_features_boruta(X, y)
            if selected_mask is not None:
                print(f"Boruta selected {sum(selected_mask)}/{len(FEATURE_NAMES)} features: {selected_names}")
            else:
                print("Boruta: too few features selected, using all")
        except ImportError:
            print("Boruta not installed (pip install boruta_py), using all features")
            selected_mask = None
            selected_names = FEATURE_NAMES

        print("Training stacking ensemble...")
        result = train_model(X, y, feature_mask=selected_mask,
                             selected_feature_names=selected_names)
        print(f"CV Accuracy: {result['cv_accuracy']:.3f}")
        print(f"CV AUC: {result['cv_auc']:.3f}")

    elif "--predict" in sys.argv:
        model_result = load_model()
        if model_result:
            model, meta = model_result
            feature_mask = meta.get("_feature_mask", None)
            # Create a dummy feature vector for testing
            test_features = np.full(len(FEATURE_NAMES), 0.5)
            conf = predict_confidence(model, test_features, feature_mask=feature_mask)
            print(f"Test prediction confidence: {conf:.3f}")
            print(f"Model trained on: {meta.get('training_date', 'unknown')}")
            print(f"Model AUC: {meta.get('cv_auc', 'unknown')}")
            print(f"Model type: {meta.get('model_type', 'unknown')}")
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
