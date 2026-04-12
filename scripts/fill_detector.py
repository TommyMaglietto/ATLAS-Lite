#!/usr/bin/env python3
"""
ATLAS Lite -- Fill Detector
Checks all pending orders against Alpaca, updates state files for fills/cancellations,
and logs every state transition. Does NOT place new orders.

Usage:  python scripts/fill_detector.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json

STATE_DIR = PROJECT_ROOT / "state"
CONFIG_DIR = PROJECT_ROOT / "config"

TRAILING_STOPS_FILE = STATE_DIR / "trailing_stops.json"
COPY_TRADES_FILE = STATE_DIR / "copy_trades.json"
POSITIONS_FILE = STATE_DIR / "positions.json"
WHEEL_FILE = STATE_DIR / "wheel_cycles.json"
TRADES_LOG = PROJECT_ROOT / "logs" / "trades.jsonl"

# ---------------------------------------------------------------------------
# Alpaca client (always paper=True)
# ---------------------------------------------------------------------------
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderStatus
except ImportError:
    print("ERROR: alpaca-py not installed. Run: pip install alpaca-py", file=sys.stderr)
    sys.exit(1)

API_KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

if not API_KEY or not SECRET_KEY:
    print("ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.", file=sys.stderr)
    sys.exit(1)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# Terminal order states -- order will never change again
TERMINAL_STATES = {"filled", "canceled", "cancelled", "expired", "replaced", "rejected"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_trade_log(record: dict) -> None:
    """Append a single JSON line to the trades log."""
    try:
        TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"  WARNING: Could not write to trades log: {e}", file=sys.stderr)


def order_status_str(status) -> str:
    """Normalise an Alpaca OrderStatus enum to a lowercase string."""
    return str(status).replace("OrderStatus.", "").lower()


def fetch_order(order_id: str) -> dict | None:
    """
    Fetch a single order from Alpaca.  Returns a dict with normalised fields
    or None on failure (order may have been deleted, network error, etc.).
    """
    try:
        order = trading_client.get_order_by_id(order_id)
        return {
            "raw": order,
            "status": order_status_str(order.status),
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "filled_qty": float(order.filled_qty) if order.filled_qty else None,
            "symbol": order.symbol,
            "side": str(order.side),
            "order_id": str(order.id),
        }
    except Exception as e:
        print(f"  WARNING: Could not fetch order {order_id}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 1.  Process trailing-stop pending fills
# ---------------------------------------------------------------------------

def process_trailing_stops() -> int:
    """Check pending trailing-stop orders, update state.  Returns count of changes."""
    print("\n=== Trailing Stops ===")
    ts_data = atomic_read_json(str(TRAILING_STOPS_FILE))
    if ts_data is None:
        print("  ERROR: Could not read trailing_stops.json")
        return 0

    # Load strategy params for loss_pct default
    params = atomic_read_json(str(CONFIG_DIR / "strategy_params.json")) or {}
    default_loss_pct = params.get("trailing_stop", {}).get("loss_pct", 5.0)

    changes = 0
    for stop in ts_data.get("active_stops", []):
        if stop.get("status") != "PENDING_FILL":
            continue

        order_id = stop.get("entry_order_id")
        symbol = stop.get("symbol", "???")
        print(f"  Checking {symbol} order {order_id} ...")

        if not order_id:
            print(f"    SKIP: no order_id for {symbol}")
            continue

        result = fetch_order(order_id)
        if result is None:
            print(f"    SKIP: could not reach Alpaca for {symbol}")
            continue

        status = result["status"]
        print(f"    Alpaca status: {status}")

        # --- FILLED ---
        if status == "filled":
            filled_price = result["filled_avg_price"]
            filled_qty = result["filled_qty"]
            loss_pct = stop.get("loss_pct", default_loss_pct)

            stop["status"] = "ACTIVE"
            stop["entry_price"] = filled_price
            stop["qty"] = filled_qty
            stop["highest_price"] = filled_price
            stop["floor_price"] = round(filled_price * (1 - loss_pct / 100), 2)
            stop["last_checked"] = now_iso()

            print(f"    FILLED: {filled_qty} shares @ ${filled_price:.2f}  "
                  f"floor=${stop['floor_price']:.2f}")

            append_trade_log({
                "timestamp": now_iso(),
                "strategy": "TRAILING_STOP",
                "action": "BUY_FILL",
                "symbol": symbol,
                "asset_class": stop.get("asset_class", "equity"),
                "qty": filled_qty,
                "price": filled_price,
                "order_type": "limit",
                "order_id": order_id,
                "status": "filled",
                "signal_type": "limit_order_fill",
                "pnl": 0,
            })
            changes += 1

        # --- CANCELLED / EXPIRED / REJECTED ---
        elif status in TERMINAL_STATES:
            stop["status"] = status.upper()
            stop["last_checked"] = now_iso()
            print(f"    TERMINAL: order is {status}")

            append_trade_log({
                "timestamp": now_iso(),
                "strategy": "TRAILING_STOP",
                "action": f"ORDER_{status.upper()}",
                "symbol": symbol,
                "asset_class": stop.get("asset_class", "equity"),
                "qty": stop.get("qty", 0),
                "price": stop.get("entry_price", 0),
                "order_type": "limit",
                "order_id": order_id,
                "status": status,
                "signal_type": "order_terminal",
                "pnl": 0,
            })
            changes += 1

        else:
            # Still open / partially filled / etc.
            stop["last_checked"] = now_iso()
            print(f"    STILL PENDING ({status})")

    if changes:
        atomic_write_json(str(TRAILING_STOPS_FILE), ts_data)
        print(f"  Wrote {changes} update(s) to trailing_stops.json")

    return changes


# ---------------------------------------------------------------------------
# 2.  Process copy-trade pending fills
# ---------------------------------------------------------------------------

def process_copy_trades() -> int:
    """Check pending copy-trade orders, update state.  Returns count of changes."""
    print("\n=== Copy Trades ===")
    ct_data = atomic_read_json(str(COPY_TRADES_FILE))
    if ct_data is None:
        print("  ERROR: Could not read copy_trades.json")
        return 0

    changes = 0
    for trade in ct_data.get("replicated_trades", []):
        if trade.get("status") != "PENDING_FILL":
            continue

        order_id = trade.get("order_id")
        symbol = trade.get("symbol", "???")
        print(f"  Checking {symbol} order {order_id} ...")

        if not order_id:
            print(f"    SKIP: no order_id for {symbol}")
            continue

        result = fetch_order(order_id)
        if result is None:
            print(f"    SKIP: could not reach Alpaca for {symbol}")
            continue

        status = result["status"]
        print(f"    Alpaca status: {status}")

        # --- FILLED ---
        if status == "filled":
            filled_price = result["filled_avg_price"]
            filled_qty = result["filled_qty"]

            trade["status"] = "FILLED"
            trade["entry_price"] = filled_price
            trade["qty"] = filled_qty
            trade["order_status"] = f"OrderStatus.FILLED"
            trade["filled_at"] = now_iso()

            print(f"    FILLED: {filled_qty} shares @ ${filled_price:.2f}")

            append_trade_log({
                "timestamp": now_iso(),
                "event": "copy_trade_fill",
                "strategy": "politician_copy",
                "action": "BUY_FILL",
                "symbol": symbol,
                "asset_class": "equity",
                "qty": filled_qty,
                "price": filled_price,
                "order_type": "limit",
                "order_id": order_id,
                "status": "filled",
                "source_politician": trade.get("source_politician", ""),
                "bipartisan": trade.get("bipartisan", False),
                "signal_type": "limit_order_fill",
                "pnl": 0,
            })
            changes += 1

        # --- CANCELLED / EXPIRED / REJECTED ---
        elif status in TERMINAL_STATES:
            trade["status"] = status.upper()
            trade["order_status"] = f"OrderStatus.{status.upper()}"

            print(f"    TERMINAL: order is {status}")

            append_trade_log({
                "timestamp": now_iso(),
                "event": f"copy_trade_{status}",
                "strategy": "politician_copy",
                "action": f"ORDER_{status.upper()}",
                "symbol": symbol,
                "asset_class": "equity",
                "qty": trade.get("qty", 0),
                "price": trade.get("limit_price", 0),
                "order_type": "limit",
                "order_id": order_id,
                "status": status,
                "source_politician": trade.get("source_politician", ""),
                "signal_type": "order_terminal",
                "pnl": 0,
            })
            changes += 1

        else:
            print(f"    STILL PENDING ({status})")

    if changes:
        ct_data["last_updated"] = now_iso()
        atomic_write_json(str(COPY_TRADES_FILE), ct_data)
        print(f"  Wrote {changes} update(s) to copy_trades.json")

    return changes


# ---------------------------------------------------------------------------
# 3.  Sync positions.json pending_orders
# ---------------------------------------------------------------------------

def sync_positions(ts_changes: int, ct_changes: int) -> int:
    """
    Reconcile positions.json: move filled orders into positions, remove
    terminal orders from pending_orders.  Returns count of changes.
    """
    print("\n=== Positions Sync ===")
    pos_data = atomic_read_json(str(POSITIONS_FILE))
    if pos_data is None:
        print("  ERROR: Could not read positions.json")
        return 0

    pending = pos_data.get("pending_orders", [])
    positions = pos_data.get("positions", [])

    if not pending:
        print("  No pending orders in positions.json")
        return 0

    # Build a set of all order_ids we need to check (from pending_orders)
    changes = 0
    remaining_pending = []

    for porder in pending:
        order_id = porder.get("order_id")
        symbol = porder.get("symbol", "???")
        print(f"  Checking pending {symbol} order {order_id} ...")

        if not order_id:
            remaining_pending.append(porder)
            print(f"    SKIP: no order_id, keeping in pending")
            continue

        result = fetch_order(order_id)
        if result is None:
            remaining_pending.append(porder)
            print(f"    SKIP: API error, keeping in pending")
            continue

        status = result["status"]
        print(f"    Alpaca status: {status}")

        if status == "filled":
            filled_price = result["filled_avg_price"]
            filled_qty = result["filled_qty"]

            new_position = {
                "symbol": symbol,
                "asset_class": porder.get("asset_class", "equity"),
                "strategy": porder.get("strategy", "UNKNOWN"),
                "qty": filled_qty,
                "entry_price": filled_price,
                "current_price": filled_price,
                "market_value": round(filled_price * filled_qty, 2),
                "cost_basis": round(filled_price * filled_qty, 2),
                "unrealized_pnl": 0.0,
                "opened_at": now_iso(),
            }
            positions.append(new_position)
            print(f"    FILLED: moved to positions ({filled_qty} @ ${filled_price:.2f})")
            changes += 1

        elif status in TERMINAL_STATES:
            print(f"    REMOVED: order {status}, dropped from pending")
            changes += 1

        else:
            # Still live -- keep it
            porder["status"] = status.upper()
            remaining_pending.append(porder)
            print(f"    STILL PENDING ({status})")

    if changes:
        pos_data["pending_orders"] = remaining_pending
        pos_data["positions"] = positions

        # Recompute totals
        total_value = sum(p.get("market_value", 0) for p in positions)
        pos_data["totals"]["total_positions"] = len(positions)
        pos_data["totals"]["total_pending"] = len(remaining_pending)
        pos_data["last_reconciled"] = now_iso()

        atomic_write_json(str(POSITIONS_FILE), pos_data)
        print(f"  Wrote {changes} update(s) to positions.json  "
              f"({len(positions)} positions, {len(remaining_pending)} pending)")

    return changes


# ---------------------------------------------------------------------------
# 4.  Check wheel cycle status
# ---------------------------------------------------------------------------

def check_wheel_cycles() -> None:
    """Report on wheel cycles that need attention. Does not auto-place orders."""
    print("\n=== Wheel Cycles ===")
    wheel_data = atomic_read_json(str(WHEEL_FILE))
    if wheel_data is None:
        print("  ERROR: Could not read wheel_cycles.json")
        return

    for cycle in wheel_data.get("active_cycles", []):
        cycle_id = cycle.get("id", "unknown")
        underlying = cycle.get("underlying", "???")
        order_status = cycle.get("order_status", "")
        order_id = cycle.get("order_id")

        if order_status == "QUEUED_FOR_MONDAY" and order_id is None:
            strike = cycle.get("strike", "?")
            expiry = cycle.get("expiry", "?")
            delta = cycle.get("delta", "?")
            premium = cycle.get("target_premium", "?")
            print(f"  NEEDS ATTENTION: {underlying} ${strike} put exp {expiry}")
            print(f"    delta={delta}, premium=${premium}, status={order_status}")
            print(f"    This order was never placed. Re-evaluate before placing.")

            append_trade_log({
                "timestamp": now_iso(),
                "event": "wheel_needs_attention",
                "strategy": "WHEEL",
                "underlying": underlying,
                "option_symbol": cycle.get("option_symbol", ""),
                "strike": strike,
                "expiry": expiry,
                "cycle_id": cycle_id,
                "order_status": order_status,
                "note": "Queued wheel order has no order_id -- needs manual re-evaluation",
            })

        elif order_id:
            # Has an order_id, check its status
            result = fetch_order(order_id)
            if result:
                status = result["status"]
                print(f"  {underlying} cycle {cycle_id}: Alpaca status = {status}")
            else:
                print(f"  {underlying} cycle {cycle_id}: could not fetch order {order_id}")
        else:
            print(f"  {underlying} cycle {cycle_id}: status={order_status}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("ATLAS Lite -- Fill Detector")
    print(f"Timestamp: {now_iso()}")
    print("=" * 60)

    ts_changes = process_trailing_stops()
    ct_changes = process_copy_trades()
    pos_changes = sync_positions(ts_changes, ct_changes)
    check_wheel_cycles()

    total = ts_changes + ct_changes + pos_changes
    print("\n" + "=" * 60)
    print(f"SUMMARY: {total} state change(s) applied")
    print(f"  Trailing stop updates:  {ts_changes}")
    print(f"  Copy trade updates:     {ct_changes}")
    print(f"  Position moves:         {pos_changes}")
    print("=" * 60)

    return 0 if total >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
