#!/usr/bin/env python3
"""
ATLAS Lite position reconciliation utility.
Verifies local state/positions.json matches actual Alpaca account positions.
Detects assignments, fills, and discrepancies.
"""

import json
import sys
from pathlib import Path
from datetime import datetime


def load_local_state(state_file='state/positions.json'):
    """
    Load local position state.

    Returns:
        dict: Local positions state
    """
    try:
        with open(state_file, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Warning: {state_file} not found, starting fresh")
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


def reconcile_positions(local_state, alpaca_positions, alpaca_account):
    """
    Compare local state with Alpaca positions.

    Args:
        local_state (dict): State from state/positions.json
        alpaca_positions (list): Positions from Alpaca API
        alpaca_account (dict): Account info from Alpaca API

    Returns:
        dict: Reconciliation report
    """
    discrepancies = []
    assignments_detected = []

    # Create lookup for local positions
    local_by_symbol = {p['symbol']: p for p in local_state.get('positions', [])}
    alpaca_by_symbol = {p['symbol']: p for p in alpaca_positions}

    # Check for new positions (assignments)
    for symbol, alpaca_pos in alpaca_by_symbol.items():
        if symbol not in local_by_symbol:
            assignments_detected.append({
                'symbol': symbol,
                'qty': alpaca_pos['qty'],
                'entry_price': alpaca_pos['avg_fill_price'],
                'timestamp': datetime.now().isoformat()
            })

    # Check for quantity mismatches
    for symbol, local_pos in local_by_symbol.items():
        if symbol in alpaca_by_symbol:
            alpaca_pos = alpaca_by_symbol[symbol]
            if local_pos['qty'] != alpaca_pos['qty']:
                discrepancies.append({
                    'symbol': symbol,
                    'local_qty': local_pos['qty'],
                    'alpaca_qty': alpaca_pos['qty'],
                    'mismatch_type': 'QUANTITY'
                })

    # Check for closed positions
    for symbol, local_pos in local_by_symbol.items():
        if symbol not in alpaca_by_symbol:
            discrepancies.append({
                'symbol': symbol,
                'local_qty': local_pos['qty'],
                'alpaca_qty': 0,
                'mismatch_type': 'POSITION_CLOSED'
            })

    return {
        'timestamp': datetime.now().isoformat(),
        'discrepancies': discrepancies,
        'assignments_detected': assignments_detected,
        'local_position_count': len(local_state.get('positions', [])),
        'alpaca_position_count': len(alpaca_positions),
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
            'entry_timestamp': assignment['timestamp'],
            'type': 'ASSIGNED'
        })

    # Remove closed positions
    for disc in reconciliation_report.get('discrepancies', []):
        if disc['mismatch_type'] == 'POSITION_CLOSED':
            local_state['positions'] = [
                p for p in local_state['positions']
                if p['symbol'] != disc['symbol']
            ]

    # Update totals
    local_state['last_reconciled'] = datetime.now().isoformat()
    local_state['totals']['total_positions'] = len(local_state['positions'])
    local_state['totals']['cash_available'] = reconciliation_report.get('alpaca_buying_power', 0)

    return local_state


def save_state(state, state_file='state/positions.json'):
    """
    Save state atomically using write-to-temp-then-rename pattern.

    Args:
        state (dict): State to save
        state_file (str): Target file

    Returns:
        bool: Success
    """
    from scripts.atomic_write import atomic_write_json
    return atomic_write_json(state_file, state)


def main():
    """Main reconciliation flow (for testing/manual runs)."""
    print("ATLAS Lite Position Reconciliation")
    print("===================================")
    print("Note: This script requires live Alpaca API calls.")
    print("In production, integrate with Alpaca MCP server results.")
    print()

    # Load local state
    local_state = load_local_state()
    print(f"Loaded local state: {len(local_state['positions'])} positions")

    # In actual use, get Alpaca data from MCP server:
    # alpaca_positions = mcp_call('get_positions')
    # alpaca_account = mcp_call('get_account_info')

    # For testing, use empty data
    alpaca_positions = []
    alpaca_account = {'buying_power': 100000, 'cash': 100000}

    # Run reconciliation
    report = reconcile_positions(local_state, alpaca_positions, alpaca_account)
    print(f"\nReconciliation Status: {report['status']}")
    print(f"Discrepancies Found: {len(report['discrepancies'])}")
    print(f"Assignments Detected: {len(report['assignments_detected'])}")

    # Update state
    updated_state = update_local_state(local_state, report)

    # Save
    if save_state(updated_state):
        print("State saved successfully")
    else:
        print("Failed to save state")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
