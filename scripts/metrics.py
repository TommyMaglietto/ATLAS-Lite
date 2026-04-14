#!/usr/bin/env python3
"""
ATLAS Lite metrics calculation utility.
Computes Sharpe ratio, win rate, drawdown, and other performance metrics from trade logs.

Enhanced with unrealized P&L from live Alpaca positions so that the self-improvement
engine can see total return (realized + unrealized) instead of only closed-trade P&L.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import statistics


def load_trades(logfile='logs/trades.jsonl'):
    """
    Load trades from JSONL file.

    Args:
        logfile (str): Path to trades.jsonl

    Returns:
        list: List of trade dictionaries
    """
    trades = []
    try:
        with open(logfile, 'r') as f:
            for line_num, line in enumerate(f, 1):
                if line.strip():
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        print(f"Warning: malformed JSON on line {line_num} of {logfile}, skipping")
    except FileNotFoundError:
        print(f"Warning: {logfile} not found")
    return trades


def filter_trades_by_days(trades, days=5):
    """
    Filter trades from last N days.

    Args:
        trades (list): List of trade dicts (must have 'timestamp' field)
        days (int): Number of days to lookback

    Returns:
        list: Filtered trades
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered = []
    for t in trades:
        ts_str = t.get('timestamp', '2000-01-01')
        ts = datetime.fromisoformat(ts_str)
        # Make offset-naive timestamps timezone-aware (assume UTC)
        if ts.tzinfo is None:
            from datetime import timezone
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            filtered.append(t)
    return filtered


def _load_config_risk_free_rate():
    """Load risk_free_rate from config/strategy_params.json if available."""
    try:
        config_path = Path(__file__).resolve().parent.parent / 'config' / 'strategy_params.json'
        with open(config_path, 'r') as f:
            params = json.load(f)
        return params.get('risk', {}).get('risk_free_rate', 0.045)
    except Exception:
        return 0.045


def calculate_sharpe(trades, risk_free_rate=None):
    """
    Calculate Sharpe ratio from trades.

    Args:
        trades (list): Trade records with 'pnl' field
        risk_free_rate (float): Annual risk-free rate. If None, reads from
            config/strategy_params.json (defaults to 0.045).

    Returns:
        float: Sharpe ratio
    """
    if risk_free_rate is None:
        risk_free_rate = _load_config_risk_free_rate()
    if not trades:
        return 0.0

    daily_pnls = defaultdict(float)
    for trade in trades:
        ts = trade.get('timestamp', '')
        date = ts[:10] if ts else '2000-01-01'
        daily_pnls[date] += trade.get('pnl', 0)

    pnl_values = list(daily_pnls.values())
    if len(pnl_values) < 2:
        return 0.0

    mean_return = statistics.mean(pnl_values)
    std_dev = statistics.stdev(pnl_values) if len(pnl_values) > 1 else 0.001

    if std_dev == 0:
        return 0.0

    # Assuming ~252 trading days per year
    # Subtract daily risk-free rate from daily returns BEFORE annualizing
    risk_free_daily = risk_free_rate / 252
    sharpe = ((mean_return - risk_free_daily) / std_dev) * (252 ** 0.5)
    return round(sharpe, 2)


def calculate_win_rate(trades):
    """
    Calculate percentage of profitable trades.

    Args:
        trades (list): Trade records with 'pnl' field

    Returns:
        float: Win rate (0.0 to 1.0)
    """
    if not trades:
        return 0.0

    winners = sum(1 for t in trades if t.get('pnl', 0) > 0)
    return round(winners / len(trades), 3)


def calculate_drawdown(trades):
    """
    Calculate maximum drawdown.

    Args:
        trades (list): Trade records with 'pnl' field

    Returns:
        float: Maximum drawdown (negative percentage)
    """
    if not trades:
        return 0.0

    cumulative = 0
    peak = 0
    max_dd = 0

    for trade in trades:
        cumulative += trade.get('pnl', 0)
        if cumulative > peak:
            peak = cumulative
        drawdown = cumulative - peak
        if drawdown < max_dd:
            max_dd = drawdown

    return max_dd


def calculate_metrics_by_strategy(trades, days=5):
    """
    Calculate metrics grouped by strategy.

    Args:
        trades (list): All trades
        days (int): Lookback period

    Returns:
        dict: Metrics by strategy name
    """
    recent_trades = filter_trades_by_days(trades, days)

    by_strategy = defaultdict(list)
    for trade in recent_trades:
        strategy = trade.get('strategy', 'UNKNOWN')
        by_strategy[strategy].append(trade)

    results = {}
    for strategy, strat_trades in by_strategy.items():
        total_pnl = sum(t.get('pnl', 0) for t in strat_trades)
        results[strategy] = {
            'trade_count': len(strat_trades),
            'total_pnl': round(total_pnl, 2),
            'sharpe': calculate_sharpe(strat_trades),
            'win_rate': calculate_win_rate(strat_trades),
            'max_drawdown': round(calculate_drawdown(strat_trades), 2)
        }

    return results


def generate_summary(logfile='logs/trades.jsonl', lookback_days=5):
    """
    Generate full metrics summary.

    Args:
        logfile (str): Path to trades.jsonl
        lookback_days (int): Days to analyze

    Returns:
        dict: Complete metrics summary
    """
    trades = load_trades(logfile)
    recent = filter_trades_by_days(trades, lookback_days)

    total_pnl = sum(t.get('pnl', 0) for t in recent)

    # Fetch unrealized P&L (gracefully returns empty on failure)
    unrealized = get_unrealized_pnl()

    return {
        'timestamp': datetime.now().isoformat(),
        'lookback_days': lookback_days,
        'total_trades': len(recent),
        'total_pnl': round(total_pnl, 2),
        'unrealized_pnl': unrealized.get('total_unrealized', 0.0),
        'combined_pnl': round(total_pnl + unrealized.get('total_unrealized', 0.0), 2),
        'position_count': unrealized.get('position_count', 0),
        'overall_sharpe': calculate_sharpe(recent),
        'overall_win_rate': calculate_win_rate(recent),
        'overall_max_drawdown': round(calculate_drawdown(recent), 2),
        'by_strategy': calculate_metrics_by_strategy(trades, lookback_days),
        'unrealized_by_symbol': unrealized.get('by_symbol', {})
    }


# ---------------------------------------------------------------------------
# Enhanced metrics: unrealized P&L + total-return Sharpe
# ---------------------------------------------------------------------------

def get_unrealized_pnl():
    """
    Fetch unrealized P&L from live Alpaca positions.

    Returns a dict with per-symbol unrealized P&L, the total, and a timestamp.
    Falls back gracefully to the local state/positions.json snapshot when the
    Alpaca API is unavailable, and returns zeroes if neither source works.
    """
    # Try live Alpaca API first
    try:
        from alpaca.trading.client import TradingClient

        api_key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

        if api_key and secret_key:
            trading_client = TradingClient(api_key, secret_key, paper=True)
            positions = trading_client.get_all_positions()

            by_symbol = {}
            total = 0.0
            for pos in positions:
                sym = pos.symbol
                upl = float(pos.unrealized_pl)
                by_symbol[sym] = round(upl, 2)
                total += upl

            return {
                "by_symbol": by_symbol,
                "total_unrealized": round(total, 2),
                "position_count": len(positions),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "alpaca_api"
            }
    except Exception as e:
        print(f"  Warning: Alpaca API unreachable for unrealized PnL: {e}")
        # fall through to local state file

    # Fallback: read from local state/positions.json
    try:
        state_path = Path(__file__).resolve().parent.parent / 'state' / 'positions.json'
        with open(state_path, 'r') as f:
            data = json.load(f)

        by_symbol = {}
        total = 0.0
        for p in data.get('positions', []):
            sym = p['symbol']
            upl = float(p.get('unrealized_pnl', 0))
            by_symbol[sym] = round(upl, 2)
            total += upl

        return {
            "by_symbol": by_symbol,
            "total_unrealized": round(total, 2),
            "position_count": len(by_symbol),
            "timestamp": data.get('last_reconciled', datetime.now(timezone.utc).isoformat()),
            "source": "local_state_file"
        }
    except Exception as e:
        print(f"  Warning: Could not read local positions.json for unrealized PnL: {e}")

    # Nothing available -- return safe empty result
    return {
        "by_symbol": {},
        "total_unrealized": 0.0,
        "position_count": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "none"
    }


def _map_positions_to_strategies():
    """
    Map position symbols to their strategy names using state files.

    Reads state/positions.json first (has an explicit 'strategy' field).
    Falls back to state/trailing_stops.json and state/copy_trades.json for
    symbols that are missing.

    Returns:
        dict: {symbol: strategy_name}
    """
    strategy_map = {}

    # Primary source: state/positions.json
    try:
        state_dir = Path(__file__).resolve().parent.parent / 'state'
        with open(state_dir / 'positions.json', 'r') as f:
            data = json.load(f)
        for p in data.get('positions', []):
            strategy_map[p['symbol']] = p.get('strategy', 'UNKNOWN')
    except Exception:
        pass

    # Supplement from trailing_stops.json (may use slash-format symbols like BTC/USD)
    try:
        state_dir = Path(__file__).resolve().parent.parent / 'state'
        with open(state_dir / 'trailing_stops.json', 'r') as f:
            data = json.load(f)
        for stop in data.get('active_stops', []):
            sym = stop['symbol']
            # Normalise "BTC/USD" -> "BTCUSD" to match Alpaca position symbols
            normalised = sym.replace('/', '')
            if normalised not in strategy_map:
                strategy_map[normalised] = stop.get('strategy_source', 'TRAILING_STOP')
    except Exception:
        pass

    # Supplement from copy_trades.json
    try:
        state_dir = Path(__file__).resolve().parent.parent / 'state'
        with open(state_dir / 'copy_trades.json', 'r') as f:
            data = json.load(f)
        for trade in data.get('replicated_trades', []):
            sym = trade['symbol']
            if sym not in strategy_map:
                strategy_map[sym] = 'politician_copy'
    except Exception:
        pass

    return strategy_map


def calculate_total_return_sharpe(trades, unrealized_data, account_equity,
                                  risk_free_rate=None):
    """
    Calculate Sharpe using total returns (realized + unrealized).

    Approach:
      1. Compute daily realized P&L from trades (same as calculate_sharpe).
      2. Distribute the current total unrealized P&L evenly across the holding
         period as a daily mark-to-market return.
      3. Express each day's combined P&L as a percentage of account equity.
      4. Compute annualised Sharpe from those daily return percentages.

    This is an approximation (we lack historical intraday unrealized snapshots),
    but it is far better than ignoring hundreds of dollars in open-position P&L.

    Args:
        trades (list): Trade records with 'pnl' and 'timestamp' fields.
        unrealized_data (dict): Output of get_unrealized_pnl().
        account_equity (float): Current account equity (used for return %).
        risk_free_rate (float): Annual risk-free rate. If None, reads from
            config/strategy_params.json (defaults to 0.045).

    Returns:
        float: Annualised Sharpe ratio.
    """
    if risk_free_rate is None:
        risk_free_rate = _load_config_risk_free_rate()
    if account_equity <= 0:
        return 0.0

    # --- Realised daily P&L ------------------------------------------------
    daily_pnls = defaultdict(float)
    for trade in trades:
        ts = trade.get('timestamp', '')
        date = ts[:10] if ts else '2000-01-01'
        daily_pnls[date] += trade.get('pnl', 0)

    # --- Unrealized daily component ----------------------------------------
    total_unrealized = unrealized_data.get('total_unrealized', 0.0)

    # Estimate holding days: from earliest trade to today
    if trades:
        earliest = min(t.get('timestamp', '')[:10] for t in trades if t.get('timestamp'))
        try:
            start_date = datetime.fromisoformat(earliest)
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            holding_days = max(
                (datetime.now(timezone.utc) - start_date).days, 1
            )
        except (ValueError, TypeError):
            holding_days = 1
    else:
        holding_days = 1

    daily_unrealized_pnl = total_unrealized / holding_days

    # Merge: add the daily unrealized slice to every day in the period
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if not daily_pnls:
        # No realized trades -- create a synthetic entry so we still capture
        # unrealized movement.
        daily_pnls[today_str] = 0.0

    for date in list(daily_pnls.keys()):
        daily_pnls[date] += daily_unrealized_pnl

    # Also ensure we have entries for days between the earliest trade and today
    # so that the holding-period spread is represented.
    if trades:
        try:
            cur = datetime.fromisoformat(earliest)
            if cur.tzinfo is None:
                cur = cur.replace(tzinfo=timezone.utc)
            end = datetime.now(timezone.utc)
            while cur <= end:
                d = cur.strftime('%Y-%m-%d')
                if d not in daily_pnls:
                    daily_pnls[d] = daily_unrealized_pnl
                cur += timedelta(days=1)
        except (ValueError, TypeError):
            pass

    # --- Convert to daily return % of equity -------------------------------
    daily_returns = [pnl / account_equity for pnl in daily_pnls.values()]

    if len(daily_returns) < 2:
        return 0.0

    mean_return = statistics.mean(daily_returns)
    std_dev = statistics.stdev(daily_returns)

    if std_dev == 0:
        return 0.0

    risk_free_daily = risk_free_rate / 252
    sharpe = ((mean_return - risk_free_daily) / std_dev) * (252 ** 0.5)
    return round(sharpe, 2)


def calculate_enhanced_metrics_by_strategy(trades, days=5):
    """
    Enhanced metrics that include unrealized P&L from live positions.

    Builds on calculate_metrics_by_strategy() by adding:
      - unrealized_pnl per strategy (mapped via state files)
      - total_pnl (realized + unrealized)
      - enhanced_sharpe using calculate_total_return_sharpe()

    Falls back to base (realized-only) metrics if Alpaca / state data is
    unavailable.

    Args:
        trades (list): All trades.
        days (int): Lookback period.

    Returns:
        dict: Metrics by strategy name, enriched with unrealized data.
    """
    base_metrics = calculate_metrics_by_strategy(trades, days)

    # Fetch unrealized data
    unrealized = get_unrealized_pnl()
    if unrealized.get('position_count', 0) == 0:
        # Nothing to enhance -- return base metrics with zero placeholders
        for metrics in base_metrics.values():
            metrics['unrealized_pnl'] = 0.0
            metrics['total_pnl'] = metrics['total_pnl']
            metrics['enhanced_sharpe'] = metrics['sharpe']
        return base_metrics

    # Map each symbol to its strategy
    strategy_map = _map_positions_to_strategies()

    # Compute account equity for Sharpe denominator
    account_equity = _get_account_equity()

    recent_trades = filter_trades_by_days(trades, days)

    for strategy, metrics in base_metrics.items():
        # Sum unrealized P&L for positions belonging to this strategy
        strategy_unrealized = sum(
            unrealized['by_symbol'].get(sym, 0)
            for sym, strat in strategy_map.items()
            if strat == strategy
        )
        metrics['unrealized_pnl'] = round(strategy_unrealized, 2)
        metrics['total_pnl'] = round(metrics['total_pnl'] + strategy_unrealized, 2)

        # Compute enhanced Sharpe for this strategy's trades
        strat_trades = [t for t in recent_trades if t.get('strategy') == strategy]
        strat_unrealized = {
            'total_unrealized': strategy_unrealized,
            'by_symbol': {
                sym: unrealized['by_symbol'].get(sym, 0)
                for sym, strat in strategy_map.items()
                if strat == strategy
            }
        }
        metrics['enhanced_sharpe'] = calculate_total_return_sharpe(
            strat_trades, strat_unrealized, account_equity
        )

    # Handle positions whose strategy does not appear in base_metrics
    strategies_with_positions = set(strategy_map.values())
    for strat_name in strategies_with_positions:
        if strat_name not in base_metrics:
            strategy_unrealized = sum(
                unrealized['by_symbol'].get(sym, 0)
                for sym, strat in strategy_map.items()
                if strat == strat_name
            )
            if strategy_unrealized != 0:
                base_metrics[strat_name] = {
                    'trade_count': 0,
                    'total_pnl': round(strategy_unrealized, 2),
                    'sharpe': 0.0,
                    'win_rate': 0.0,
                    'max_drawdown': 0.0,
                    'unrealized_pnl': round(strategy_unrealized, 2),
                    'enhanced_sharpe': 0.0
                }

    return base_metrics


def _get_account_equity():
    """
    Get account equity from Alpaca API, falling back to state file totals.

    Returns:
        float: Account equity, or 100000.0 as a safe default.
    """
    # Try Alpaca API
    try:
        from alpaca.trading.client import TradingClient

        api_key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

        if api_key and secret_key:
            trading_client = TradingClient(api_key, secret_key, paper=True)
            account = trading_client.get_account()
            return float(account.equity)
    except Exception:
        pass

    # Fallback: estimate from positions.json totals
    try:
        state_path = Path(__file__).resolve().parent.parent / 'state' / 'positions.json'
        with open(state_path, 'r') as f:
            data = json.load(f)
        totals = data.get('totals', {})
        total_value = totals.get('total_value', 0)
        cash = totals.get('cash_available', 0)
        if total_value > 0 or cash > 0:
            return float(total_value + cash)
    except Exception:
        pass

    return 100000.0  # safe default for paper account


if __name__ == '__main__':
    # Print realized-only summary
    summary = generate_summary()
    print("=" * 60)
    print("ATLAS Lite Metrics Summary")
    print("=" * 60)
    print(json.dumps(summary, indent=2))

    # Print enhanced (realized + unrealized) metrics
    print()
    print("=" * 60)
    print("Enhanced Metrics (Realized + Unrealized)")
    print("=" * 60)
    trades = load_trades()
    enhanced = calculate_enhanced_metrics_by_strategy(trades, days=5)
    print(json.dumps(enhanced, indent=2))

    # Print total-return Sharpe
    unrealized = get_unrealized_pnl()
    equity = _get_account_equity()
    recent = filter_trades_by_days(trades, days=5)
    total_sharpe = calculate_total_return_sharpe(recent, unrealized, equity)
    print()
    print(f"Total-return Sharpe (realized + unrealized): {total_sharpe}")
    print(f"Account equity: ${equity:,.2f}")
    print(f"Unrealized P&L: ${unrealized.get('total_unrealized', 0):,.2f}")
    print(f"Data source: {unrealized.get('source', 'unknown')}")
