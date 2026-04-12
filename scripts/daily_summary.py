#!/usr/bin/env python3
"""
ATLAS Lite daily summary generator.
Reads all state files, fetches live Alpaca account data, and produces
an end-of-day report saved to logs/daily_summaries/YYYY-MM-DD.json.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Project paths & imports
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json

STATE_DIR = PROJECT_ROOT / "state"
LOGS_DIR = PROJECT_ROOT / "logs"
SUMMARIES_DIR = LOGS_DIR / "daily_summaries"
TRADES_LOG = LOGS_DIR / "trades.jsonl"

POSITIONS_FILE = STATE_DIR / "positions.json"
TRAILING_STOPS_FILE = STATE_DIR / "trailing_stops.json"
EXPERIMENTS_FILE = STATE_DIR / "experiments.json"
REGIME_FILE = STATE_DIR / "regime.json"


def get_today_str():
    """Return today's date as YYYY-MM-DD string."""
    return datetime.now().strftime("%Y-%m-%d")


def read_todays_trades(today_str):
    """
    Read trades from logs/trades.jsonl that match today's date.

    Returns:
        list: Trade entries from today
    """
    trades = []
    if not TRADES_LOG.exists():
        return trades

    try:
        with open(TRADES_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")
                    # Match if the date portion starts with today's date
                    if ts.startswith(today_str) or today_str in ts[:10]:
                        trades.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"  Warning: Could not read trades log: {e}")

    return trades


def summarize_trades(trades):
    """
    Summarize today's trades into counts and totals.

    Returns:
        dict: Trade summary
    """
    count = len(trades)
    total_bought = 0.0
    total_sold = 0.0
    realized_pnl = 0.0

    for t in trades:
        side = t.get("side", t.get("action", "")).upper()
        qty = float(t.get("qty", 0))
        price = float(t.get("price", t.get("limit_price", 0)) or 0)
        notional = qty * price

        if side in ("BUY", "BOUGHT"):
            total_bought += notional
        elif side in ("SELL", "SOLD"):
            total_sold += notional

        pnl = float(t.get("pnl", t.get("realized_pnl", 0)) or 0)
        realized_pnl += pnl

    return {
        "count": count,
        "total_bought": round(total_bought, 2),
        "total_sold": round(total_sold, 2),
        "realized_pnl": round(realized_pnl, 2),
    }


def get_trailing_stops_summary(stops_data, today_str):
    """
    Summarize trailing stop status.

    Returns:
        dict: Trailing stops summary
    """
    if not stops_data:
        return {"count_active": 0, "count_triggered_today": 0, "stops": []}

    active = [s for s in stops_data.get("active_stops", []) if s.get("status") == "ACTIVE"]
    pending = [s for s in stops_data.get("active_stops", []) if s.get("status") == "PENDING_FILL"]

    # Count stops triggered/closed today
    triggered_today = 0
    for s in stops_data.get("closed_stops", []):
        closed_at = s.get("closed_at", s.get("triggered_at", ""))
        if today_str in str(closed_at)[:10]:
            triggered_today += 1

    return {
        "count_active": len(active),
        "count_pending": len(pending),
        "count_triggered_today": triggered_today,
    }


def detect_warnings(positions_data, stops_data, alpaca_positions, alpaca_account):
    """
    Detect anomalies and generate warnings.

    Returns:
        list: Warning strings
    """
    warnings = []

    # Check for stale reconciliation
    last_reconciled = positions_data.get("last_reconciled", "")
    if last_reconciled:
        try:
            reconciled_dt = datetime.fromisoformat(last_reconciled.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - reconciled_dt).total_seconds() / 3600
            if age_hours > 1:
                warnings.append(f"Positions last reconciled {age_hours:.1f} hours ago")
        except (ValueError, TypeError):
            warnings.append("Could not parse last_reconciled timestamp")

    # Check for trailing stop quantity mismatches vs Alpaca
    if stops_data and alpaca_positions:
        alpaca_by_sym = {}
        for p in alpaca_positions:
            sym = p.get("symbol", "")
            alpaca_by_sym[sym] = p
            # Normalize crypto: BTCUSD -> BTC/USD
            if sym.endswith("USD") and "/" not in sym and len(sym) > 3:
                alpaca_by_sym[sym[:-3] + "/USD"] = p

        for stop in stops_data.get("active_stops", []):
            if stop.get("status") != "ACTIVE":
                continue
            sym = stop.get("symbol")
            ap = alpaca_by_sym.get(sym)
            if ap:
                stop_qty = float(stop.get("qty", 0))
                alpaca_qty = float(ap.get("qty", 0))
                if abs(stop_qty - alpaca_qty) > 0.000001:
                    warnings.append(
                        f"Trailing stop qty mismatch for {sym}: "
                        f"stop={stop_qty}, alpaca={alpaca_qty}"
                    )

    # Check cash reserve (should be >= 20%)
    equity = float(alpaca_account.get("equity", 0))
    cash = float(alpaca_account.get("cash", 0))
    if equity > 0:
        cash_pct = (cash / equity) * 100
        if cash_pct < 20:
            warnings.append(f"Cash reserve at {cash_pct:.1f}% -- below 20% target")

    return warnings


def main():
    """Generate daily summary report."""
    print("=" * 64)
    print("  ATLAS Lite Daily Summary")
    print("=" * 64)
    print()

    today_str = get_today_str()

    # -----------------------------------------------------------------------
    # 1. Initialize Alpaca client
    # -----------------------------------------------------------------------
    API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
    SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

    alpaca_account = {}
    alpaca_positions = []

    if not API_KEY or not SECRET_KEY:
        print("  Warning: Alpaca API keys not found. Using local state only.")
        print()
    else:
        try:
            from alpaca.trading.client import TradingClient
            trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)  # ALWAYS paper

            print("[1] Fetching live Alpaca data ...")
            account = trading_client.get_account()
            positions = trading_client.get_all_positions()

            alpaca_account = {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value) if account.portfolio_value else 0,
                "status": str(account.status),
            }

            for pos in positions:
                alpaca_positions.append({
                    "symbol": pos.symbol,
                    "qty": float(pos.qty),
                    "avg_entry_price": float(pos.avg_entry_price) if pos.avg_entry_price else 0,
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "asset_class": str(pos.asset_class),
                })

            print(f"     Account equity   : ${alpaca_account['equity']:,.2f}")
            print(f"     Positions        : {len(alpaca_positions)}")
            print()
        except ImportError:
            print("  Warning: alpaca-py not installed. Using local state only.")
            print()
        except Exception as e:
            print(f"  Warning: Alpaca API error: {e}. Using local state only.")
            print()

    # -----------------------------------------------------------------------
    # 2. Read all state files
    # -----------------------------------------------------------------------
    print("[2] Reading state files ...")
    positions_data = atomic_read_json(str(POSITIONS_FILE)) or {}
    stops_data = atomic_read_json(str(TRAILING_STOPS_FILE)) or {}
    experiments_data = atomic_read_json(str(EXPERIMENTS_FILE)) or {}
    regime_data = atomic_read_json(str(REGIME_FILE)) or {}

    print(f"     positions.json      : {'OK' if positions_data else 'MISSING'}")
    print(f"     trailing_stops.json : {'OK' if stops_data else 'MISSING'}")
    print(f"     experiments.json    : {'OK' if experiments_data else 'MISSING'}")
    print(f"     regime.json         : {'OK' if regime_data else 'MISSING'}")
    print()

    # -----------------------------------------------------------------------
    # 3. Read today's trades
    # -----------------------------------------------------------------------
    print("[3] Reading today's trades ...")
    todays_trades = read_todays_trades(today_str)
    trade_summary = summarize_trades(todays_trades)
    print(f"     Today's trades      : {trade_summary['count']}")
    print(f"     Total bought        : ${trade_summary['total_bought']:,.2f}")
    print(f"     Total sold          : ${trade_summary['total_sold']:,.2f}")
    print(f"     Realized PnL        : ${trade_summary['realized_pnl']:,.2f}")
    print()

    # -----------------------------------------------------------------------
    # 4. Build positions list
    # -----------------------------------------------------------------------
    positions_list = []
    if alpaca_positions:
        # Use live Alpaca data
        for p in alpaca_positions:
            positions_list.append({
                "symbol": p["symbol"],
                "qty": p["qty"],
                "entry_price": p["avg_entry_price"],
                "current_price": p["current_price"],
                "unrealized_pnl": p["unrealized_pl"],
                "market_value": p["market_value"],
            })
    else:
        # Fall back to local state
        for p in positions_data.get("positions", []):
            positions_list.append({
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "entry_price": float(p.get("entry_price", 0)),
                "current_price": float(p.get("current_price", 0)),
                "unrealized_pnl": float(p.get("unrealized_pnl", 0)),
                "market_value": float(p.get("market_value", 0)),
            })

    # -----------------------------------------------------------------------
    # 5. Trailing stops summary
    # -----------------------------------------------------------------------
    stops_summary = get_trailing_stops_summary(stops_data, today_str)

    # -----------------------------------------------------------------------
    # 6. Detect warnings
    # -----------------------------------------------------------------------
    print("[4] Checking for anomalies ...")
    warnings = detect_warnings(positions_data, stops_data, alpaca_positions, alpaca_account)
    if warnings:
        for w in warnings:
            print(f"     WARNING: {w}")
    else:
        print("     No warnings.")
    print()

    # -----------------------------------------------------------------------
    # 7. Build the summary dict
    # -----------------------------------------------------------------------
    summary = {
        "date": today_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "equity": alpaca_account.get("equity", 0),
            "cash": alpaca_account.get("cash", 0),
            "buying_power": alpaca_account.get("buying_power", 0),
            "portfolio_value": alpaca_account.get("portfolio_value", 0),
            "status": alpaca_account.get("status", "UNKNOWN"),
        },
        "positions": positions_list,
        "position_count": len(positions_list),
        "today_trades": trade_summary,
        "trailing_stops": stops_summary,
        "regime": {
            "current": regime_data.get("current_regime", "UNKNOWN"),
            "composite_score": regime_data.get("composite_score", 0),
            "last_updated": regime_data.get("last_updated", ""),
        },
        "experiment_status": {
            "engine_status": experiments_data.get("engine_status", "UNKNOWN"),
            "engine_status_reason": experiments_data.get("engine_status_reason", ""),
            "total_experiments": experiments_data.get("statistics", {}).get("total_experiments", 0),
            "accepted": experiments_data.get("statistics", {}).get("accepted", 0),
            "rejected": experiments_data.get("statistics", {}).get("rejected", 0),
        },
        "warnings": warnings,
    }

    # -----------------------------------------------------------------------
    # 8. Save to logs/daily_summaries/YYYY-MM-DD.json
    # -----------------------------------------------------------------------
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    summary_file = SUMMARIES_DIR / f"{today_str}.json"

    print("[5] Saving daily summary ...")
    if atomic_write_json(str(summary_file), summary):
        print(f"     Saved to {summary_file}")
    else:
        print(f"     ERROR: Failed to save {summary_file}")
        return 1
    print()

    # -----------------------------------------------------------------------
    # 9. Print formatted summary
    # -----------------------------------------------------------------------
    print("=" * 64)
    print(f"  DAILY SUMMARY  --  {today_str}")
    print("=" * 64)
    print()
    print("  ACCOUNT")
    print(f"    Equity              : ${summary['account']['equity']:,.2f}")
    print(f"    Cash                : ${summary['account']['cash']:,.2f}")
    print(f"    Buying power        : ${summary['account']['buying_power']:,.2f}")
    print()

    print("  POSITIONS")
    if positions_list:
        total_unrealized = 0.0
        total_value = 0.0
        print(f"    {'Symbol':<12} {'Qty':>12} {'Entry':>10} {'Current':>10} {'PnL':>12} {'Value':>12}")
        print(f"    {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*12} {'-'*12}")
        for p in positions_list:
            pnl = p.get("unrealized_pnl", 0)
            val = p.get("market_value", 0)
            total_unrealized += pnl
            total_value += val
            print(f"    {p['symbol']:<12} {p['qty']:>12.6f} ${p['entry_price']:>9.2f} "
                  f"${p['current_price']:>9.2f} ${pnl:>11.2f} ${val:>11.2f}")
        print(f"    {'':12} {'':12} {'':10} {'TOTAL':>10} ${total_unrealized:>11.2f} ${total_value:>11.2f}")
    else:
        print("    No positions.")
    print()

    print("  TODAY'S TRADES")
    print(f"    Count               : {trade_summary['count']}")
    print(f"    Total bought        : ${trade_summary['total_bought']:,.2f}")
    print(f"    Total sold          : ${trade_summary['total_sold']:,.2f}")
    print(f"    Realized PnL        : ${trade_summary['realized_pnl']:,.2f}")
    print()

    print("  TRAILING STOPS")
    print(f"    Active              : {stops_summary['count_active']}")
    print(f"    Pending fill        : {stops_summary.get('count_pending', 0)}")
    print(f"    Triggered today     : {stops_summary['count_triggered_today']}")
    print()

    print("  REGIME")
    print(f"    Current             : {summary['regime']['current']}")
    print(f"    Composite score     : {summary['regime']['composite_score']}")
    print()

    print("  SELF-IMPROVEMENT")
    print(f"    Engine status       : {summary['experiment_status']['engine_status']}")
    print(f"    Reason              : {summary['experiment_status']['engine_status_reason']}")
    print(f"    Experiments run     : {summary['experiment_status']['total_experiments']}")
    print()

    if warnings:
        print("  WARNINGS")
        for w in warnings:
            print(f"    - {w}")
        print()

    print("=" * 64)
    print(f"  Report saved to: {summary_file}")
    print("=" * 64)
    print()
    print("  Done.")
    print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
