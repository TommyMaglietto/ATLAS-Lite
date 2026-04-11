#!/usr/bin/env python3
"""
ATLAS Lite metrics calculation utility.
Computes Sharpe ratio, win rate, drawdown, and other performance metrics from trade logs.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
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
            for line in f:
                if line.strip():
                    trades.append(json.loads(line))
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
    cutoff = datetime.now() - timedelta(days=days)
    return [
        t for t in trades
        if datetime.fromisoformat(t.get('timestamp', '2000-01-01')) >= cutoff
    ]


def calculate_sharpe(trades, risk_free_rate=0.02):
    """
    Calculate Sharpe ratio from trades.

    Args:
        trades (list): Trade records with 'pnl' field
        risk_free_rate (float): Annual risk-free rate (default 2%)

    Returns:
        float: Sharpe ratio
    """
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
    sharpe = (mean_return / std_dev) * (252 ** 0.5) - risk_free_rate
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

    return {
        'timestamp': datetime.now().isoformat(),
        'lookback_days': lookback_days,
        'total_trades': len(recent),
        'total_pnl': round(total_pnl, 2),
        'overall_sharpe': calculate_sharpe(recent),
        'overall_win_rate': calculate_win_rate(recent),
        'overall_max_drawdown': round(calculate_drawdown(recent), 2),
        'by_strategy': calculate_metrics_by_strategy(trades, lookback_days)
    }


if __name__ == '__main__':
    summary = generate_summary()
    print(json.dumps(summary, indent=2))
