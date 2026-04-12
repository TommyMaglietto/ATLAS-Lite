#!/usr/bin/env python3
"""
ATLAS Lite position reconciliation utility.
Verifies local state/positions.json matches actual Alpaca account positions.
Reconciles trailing_stops.json quantities with live Alpaca data.
Detects assignments, fills, and discrepancies.
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

POSITIONS_FILE = PROJECT_ROOT / "state" / "positions.json"
TRAILING_STOPS_FILE = PROJECT_ROOT / "state" / "trailing_stops.json"
TRADES_LOG = PROJECT_ROOT / "logs" / "trades.jsonl"


def load_local_state(state_file=None):
    """
    Load local position state.

    Returns:
        dict: Local positions state
    """
    if state_file is None:
        state_file = str(POSITIONS_FILE)
    data = atomic_read_json(state_file)
    if data:
        return data
    print(f"Warning: {state_file} not found or unreadable, starting fresh")
    return {
        'schema_version': '1.0.0',
        'last_reconciled': None,
        'positions': [],
        'pending_orders': [],
        'totals': {
            'total_positions': 0,
            'total_value': 0,
            'cash_available': 0,
            'cash_reserved_pct': 100,
            'deployed_pct': 0
        }
    }


def normalize_symbol(symbol):
    """
    Normalize crypto symbol formats.
    BTCUSD -> BTC/USD, ETHUSD -> ETH/USD, etc.
    Leaves equity symbols unchanged.
    """
    if symbol.endswith("USD") and "/" not in symbol and len(symbol) > 3:
        return symbol[:-3] + "/USD"
    return symbol


def reconcile_positions(local_state, alpaca_positions, alpaca_account):
    """
    Compare local state with Alpaca positions.

    Args:
        local_state (dict): State from state/positions.json
        alpaca_positions (list): Positions from Alpaca API (as dicts)
        alpaca_account (dict): Account info from Alpaca API (as dict)

    Returns:
        dict: Reconciliation report
    """
    discrepancies = []
    assignments_detected = []

    # Create lookup for local positions, normalizing symbols
    local_by_symbol = {}
    for p in local_state.get('positions', []):
        sym = p['symbol']
        normalized = normalize_symbol(sym)
        local_by_symbol[sym] = p
        local_by_symbol[normalized] = p

    # Create lookup for Alpaca positions, normalizing symbols
    alpaca_by_symbol = {}
    for p in alpaca_positions:
        sym = p['symbol']
        normalized = normalize_symbol(sym)
        alpaca_by_symbol[sym] = p
        alpaca_by_symbol[normalized] = p

    # Track which alpaca symbols we've matched
    matched_alpaca = set()

    # Check for new positions in Alpaca not in local (assignments/new fills)
    for p in alpaca_positions:
        sym = p['symbol']
        normalized = normalize_symbol(sym)
        if sym not in local_by_symbol and normalized not in local_by_symbol:
            assignments_detected.append({
                'symbol': sym,
                'qty': p['qty'],
                'entry_price': p.get('avg_entry_price', 0),
                'current_price': p.get('current_price', 0),
                'market_value': p.get('market_value', 0),
                'asset_class': p.get('asset_class', 'unknown'),
                'timestamp': datetime.now(timezone.utc).isoformat()
            })

    # Check for quantity mismatches
    for p in local_state.get('positions', []):
        sym = p['symbol']
        normalized = normalize_symbol(sym)
        alpaca_pos = alpaca_by_symbol.get(sym) or alpaca_by_symbol.get(normalized)

        if alpaca_pos:
            matched_alpaca.add(alpaca_pos['symbol'])
            local_qty = float(p.get('qty', 0))
            alpaca_qty = float(alpaca_pos.get('qty', 0))
            if abs(local_qty - alpaca_qty) > 0.000001:
                discrepancies.append({
                    'symbol': sym,
                    'local_qty': local_qty,
                    'alpaca_qty': alpaca_qty,
                    'mismatch_type': 'QUANTITY'
                })
        else:
            # Position exists locally but not in Alpaca -- closed or sold
            discrepancies.append({
                'symbol': sym,
                'local_qty': float(p.get('qty', 0)),
                'alpaca_qty': 0,
                'mismatch_type': 'POSITION_CLOSED'
            })

    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'discrepancies': discrepancies,
        'assignments_detected': assignments_detected,
        'local_position_count': len(local_state.get('positions', [])),
        'alpaca_position_count': len(alpaca_positions),
        'alpaca_equity': alpaca_account.get('equity', 0),
        'alpaca_buying_power': alpaca_account.get('buying_power', 0),
        'alpaca_cash': alpaca_account.get('cash', 0),
        'status': 'OK' if not discrepancies else 'MISMATCH'
    }


def update_local_state(local_state, reconciliation_report):
    """
    Update local state based on reconciliation findings.

    Args:
        local_state (dict): Current state
        reconciliation_report (dict): Reconciliation results

    Returns:
        dict: Updated state
    """
    # Add detected assignments to positions
    for assignment in reconciliation_report.get('assignments_detected', []):
        local_state['positions'].append({
            'symbol': assignment['symbol'],
            'qty': assignment['qty'],
            'entry_price': assignment['entry_price'],
            'current_price': assignment.get('current_price', 0),
            'market_value': assignment.get('market_value', 0),
            'asset_class': assignment.get('asset_class', 'unknown'),
            'entry_timestamp': assignment['timestamp'],
            'type': 'ASSIGNED'
        })

    # Remove closed positions
    for disc in reconciliation_report.get('discrepancies', []):
        if disc['mismatch_type'] == 'POSITION_CLOSED':
            closed_sym = disc['symbol']
            closed_norm = normalize_symbol(closed_sym)
            local_state['positions'] = [
                p for p in local_state['positions']
                if p['symbol'] != closed_sym and normalize_symbol(p['symbol']) != closed_norm
            ]

    # Fix quantity mismatches -- update local to match Alpaca
    for disc in reconciliation_report.get('discrepancies', []):
        if disc['mismatch_type'] == 'QUANTITY':
            for p in local_state['positions']:
                if p['symbol'] == disc['symbol'] or normalize_symbol(p['symbol']) == normalize_symbol(disc['symbol']):
                    p['qty'] = disc['alpaca_qty']
                    break

    # Update totals
    local_state['last_reconciled'] = datetime.now(timezone.utc).isoformat()
    local_state['totals']['total_positions'] = len(local_state['positions'])
    local_state['totals']['cash_available'] = reconciliation_report.get('alpaca_cash', 0)

    return local_state


def reconcile_trailing_stops(alpaca_positions):
    """Reconcile trailing_stops.json quantities with actual Alpaca positions."""
    stops_data = atomic_read_json(str(TRAILING_STOPS_FILE))
    if not stops_data:
        return {"status": "NO_STOPS_FILE", "fixes": []}

    # Build Alpaca position lookup, normalizing symbols
    alpaca_by_symbol = {}
    for pos in alpaca_positions:
        symbol = pos.get("symbol", "")
        # Normalize: BTCUSD -> BTC/USD, ETHUSD -> ETH/USD
        normalized = normalize_symbol(symbol)
        alpaca_by_symbol[normalized] = pos
        alpaca_by_symbol[symbol] = pos  # Keep original too

    fixes = []
    for stop in stops_data.get("active_stops", []):
        sym = stop.get("symbol")
        alpaca_pos = alpaca_by_symbol.get(sym)

        if alpaca_pos and stop.get("status") == "ACTIVE":
            actual_qty = float(alpaca_pos.get("qty", 0))
            stop_qty = float(stop.get("qty", 0))

            if abs(actual_qty - stop_qty) > 0.000001:
                old_qty = stop_qty
                stop["qty"] = actual_qty
                # Also update entry price to match Alpaca's avg
                stop["entry_price"] = float(alpaca_pos.get("avg_entry_price", stop.get("entry_price", 0)))
                fixes.append({
                    "symbol": sym,
                    "old_qty": old_qty,
                    "new_qty": actual_qty,
                    "source": "alpaca_reconciliation"
                })

    if fixes:
        atomic_write_json(str(TRAILING_STOPS_FILE), stops_data)

    return {"status": "OK", "fixes": fixes}


def append_log(entry):
    """Append a JSONL entry to the trades log."""
    TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    """Main reconciliation flow -- fetches live Alpaca data and reconciles."""
    print("=" * 64)
    print("  ATLAS Lite Position Reconciliation")
    print("=" * 64)
    print()

    # -----------------------------------------------------------------------
    # 1. Initialize Alpaca client (env vars only, no hardcoded keys)
    # -----------------------------------------------------------------------
    API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
    SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

    if not API_KEY or not SECRET_KEY:
        print("ERROR: Alpaca API keys not found in environment variables.")
        print("  Set ALPACA_API_KEY / ALPACA_SECRET_KEY or APCA_API_KEY_ID / APCA_API_SECRET_KEY")
        return 1

    try:
        from alpaca.trading.client import TradingClient
        trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)  # ALWAYS paper
    except ImportError:
        print("ERROR: alpaca-py not installed. Run: pip install alpaca-py")
        return 1
    except Exception as e:
        print(f"ERROR: Failed to initialize Alpaca client: {e}")
        return 1

    # -----------------------------------------------------------------------
    # 2. Fetch live data from Alpaca
    # -----------------------------------------------------------------------
    print("[1] Fetching live Alpaca positions and account info ...")
    try:
        positions = trading_client.get_all_positions()
        account = trading_client.get_account()
    except Exception as e:
        print(f"ERROR: Failed to fetch Alpaca data: {e}")
        return 1

    # Convert position objects to dicts
    alpaca_positions = []
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

    # Convert account to dict
    alpaca_account = {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value) if account.portfolio_value else 0,
        "status": str(account.status),
    }

    print(f"     Alpaca positions : {len(alpaca_positions)}")
    print(f"     Account equity   : ${alpaca_account['equity']:,.2f}")
    print(f"     Account cash     : ${alpaca_account['cash']:,.2f}")
    print(f"     Buying power     : ${alpaca_account['buying_power']:,.2f}")
    print()

    # -----------------------------------------------------------------------
    # 3. Load local state and reconcile positions
    # -----------------------------------------------------------------------
    print("[2] Loading local state ...")
    local_state = load_local_state()
    print(f"     Local positions  : {len(local_state.get('positions', []))}")
    print(f"     Pending orders   : {len(local_state.get('pending_orders', []))}")
    print()

    print("[3] Reconciling positions ...")
    report = reconcile_positions(local_state, alpaca_positions, alpaca_account)
    print(f"     Status           : {report['status']}")
    print(f"     Discrepancies    : {len(report['discrepancies'])}")
    print(f"     Assignments      : {len(report['assignments_detected'])}")

    if report['discrepancies']:
        print()
        print("     Discrepancy details:")
        for d in report['discrepancies']:
            print(f"       {d['symbol']}: {d['mismatch_type']} "
                  f"(local={d['local_qty']}, alpaca={d['alpaca_qty']})")

    if report['assignments_detected']:
        print()
        print("     New positions detected:")
        for a in report['assignments_detected']:
            print(f"       {a['symbol']}: {a['qty']} @ ${a['entry_price']:.2f}")
    print()

    # -----------------------------------------------------------------------
    # 4. Update local state
    # -----------------------------------------------------------------------
    print("[4] Updating local positions state ...")
    updated_state = update_local_state(local_state, report)

    if atomic_write_json(str(POSITIONS_FILE), updated_state):
        print(f"     Saved {POSITIONS_FILE}")
    else:
        print(f"     ERROR: Failed to save {POSITIONS_FILE}")
        return 1
    print()

    # -----------------------------------------------------------------------
    # 5. Reconcile trailing stops
    # -----------------------------------------------------------------------
    print("[5] Reconciling trailing stops ...")
    stops_report = reconcile_trailing_stops(alpaca_positions)
    print(f"     Status           : {stops_report['status']}")
    print(f"     Fixes applied    : {len(stops_report['fixes'])}")

    if stops_report['fixes']:
        print()
        print("     Trailing stop fixes:")
        for fix in stops_report['fixes']:
            print(f"       {fix['symbol']}: qty {fix['old_qty']} -> {fix['new_qty']}")
    print()

    # -----------------------------------------------------------------------
    # 6. Log reconciliation event
    # -----------------------------------------------------------------------
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "reconciliation",
        "position_status": report['status'],
        "discrepancies": len(report['discrepancies']),
        "assignments": len(report['assignments_detected']),
        "local_positions": report['local_position_count'],
        "alpaca_positions": report['alpaca_position_count'],
        "trailing_stop_fixes": len(stops_report['fixes']),
        "account_equity": alpaca_account['equity'],
        "account_cash": alpaca_account['cash'],
    }
    append_log(log_entry)

    # -----------------------------------------------------------------------
    # 7. Summary
    # -----------------------------------------------------------------------
    print("=" * 64)
    print("  RECONCILIATION SUMMARY")
    print("=" * 64)
    print(f"  Account equity        : ${alpaca_account['equity']:,.2f}")
    print(f"  Account cash          : ${alpaca_account['cash']:,.2f}")
    print(f"  Buying power          : ${alpaca_account['buying_power']:,.2f}")
    print(f"  Alpaca positions      : {len(alpaca_positions)}")
    print(f"  Local positions       : {report['local_position_count']}")
    print(f"  Position status       : {report['status']}")
    print(f"  Discrepancies fixed   : {len(report['discrepancies'])}")
    print(f"  Assignments detected  : {len(report['assignments_detected'])}")
    print(f"  Trailing stop fixes   : {len(stops_report['fixes'])}")
    print(f"  State file            : {POSITIONS_FILE}")
    print(f"  Stops file            : {TRAILING_STOPS_FILE}")
    print("=" * 64)
    print()
    print("  Done.")
    print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
