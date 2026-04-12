#!/usr/bin/env python3
"""
ATLAS Lite — Phase 2: Open initial trailing stop positions.

Picks top 3 equities and top 2 crypto by 20-day momentum,
places buy orders, and records trailing stop parameters for later activation.

Uses Alpaca MCP-compatible SDK calls.
"""

import json
import os
import sys
import math
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"

EQUITY_BUDGET = 30_000       # $30K for equities
CRYPTO_BUDGET = 30_000       # $30K for crypto
TOP_EQUITY_N = 3             # pick 3 equities
TOP_CRYPTO_N = 2             # pick 2 crypto
TRAIL_PCT = 5.0              # trailing stop %
LOSS_PCT = 5.0               # initial stop loss %

# ---------------------------------------------------------------------------
# Atomic write (inline version matching scripts/atomic_write.py)
# ---------------------------------------------------------------------------
def atomic_write_json(filepath, data):
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=filepath.parent, delete=False,
        suffix=".tmp", encoding="utf-8"
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp_name = tmp.name
    os.replace(tmp_name, str(filepath))

def append_jsonl(filepath, record):
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ---------------------------------------------------------------------------
# Hard-coded market data from Alpaca MCP calls (gathered moments ago)
# ---------------------------------------------------------------------------
# 20-day bars (first close = ~Mar 18, last close = Apr 10)
EQUITY_BARS = {
    "AAPL":  {"first_close": 249.93,  "last_close": 260.43},
    "MSFT":  {"first_close": 391.79,  "last_close": 370.89},
    "GOOGL": {"first_close": 307.77,  "last_close": 317.28},
    "AMZN":  {"first_close": 209.82,  "last_close": 238.42},
    "TSLA":  {"first_close": 392.80,  "last_close": 348.87},
    "NVDA":  {"first_close": 180.375, "last_close": 188.61},
    "META":  {"first_close": 615.49,  "last_close": 629.84},
}

CRYPTO_BARS = {
    "BTC/USD": {"first_close": 71250.805, "last_close": 73257.983},
    "ETH/USD": {"first_close": 2203.293,  "last_close": 2264.52},
    "SOL/USD": {"first_close": 90.0805,   "last_close": 84.970},
}

# Latest quotes (from Alpaca at ~2026-04-11 18:30 UTC)
# Equities: use Apr 10 closing prices for limit orders (market closed on weekend)
EQUITY_LAST_CLOSE = {
    "AAPL":  260.43,
    "MSFT":  370.89,
    "GOOGL": 317.28,
    "AMZN":  238.42,
    "TSLA":  348.87,
    "NVDA":  188.61,
    "META":  629.84,
}

# Crypto: live ask prices (crypto trades 24/7)
CRYPTO_ASK = {
    "BTC/USD": 73253.88,
    "ETH/USD": 2265.70,
    "SOL/USD": 85.12,
}

# ---------------------------------------------------------------------------
# Momentum ranking
# ---------------------------------------------------------------------------
def momentum_pct(first, last):
    return ((last - first) / first) * 100.0

equity_momentum = {
    sym: momentum_pct(d["first_close"], d["last_close"])
    for sym, d in EQUITY_BARS.items()
}
crypto_momentum = {
    sym: momentum_pct(d["first_close"], d["last_close"])
    for sym, d in CRYPTO_BARS.items()
}

# Sort descending
eq_ranked = sorted(equity_momentum.items(), key=lambda x: x[1], reverse=True)
cr_ranked = sorted(crypto_momentum.items(), key=lambda x: x[1], reverse=True)

top_equities = [sym for sym, _ in eq_ranked[:TOP_EQUITY_N]]
top_crypto   = [sym for sym, _ in cr_ranked[:TOP_CRYPTO_N]]

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
eq_per_position = EQUITY_BUDGET / TOP_EQUITY_N      # $10,000 each
cr_per_position = CRYPTO_BUDGET / TOP_CRYPTO_N       # $15,000 each

now_iso = datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Build orders
# ---------------------------------------------------------------------------
orders = []

print("=" * 70)
print("ATLAS Lite — Phase 2: Opening Initial Positions")
print("=" * 70)
print(f"Timestamp: {now_iso}")
print(f"Regime: RISK_ON (composite +0.65)")
print(f"Market status: CLOSED (Saturday)")
print()

# --- Equity momentum table ---
print("EQUITY MOMENTUM (20-day):")
print(f"  {'Symbol':<8} {'Start':>10} {'End':>10} {'Momentum':>10}  {'Selected'}")
print(f"  {'-'*8} {'-'*10} {'-'*10} {'-'*10}  {'-'*8}")
for sym, mom in eq_ranked:
    sel = " <-- TOP 3" if sym in top_equities else ""
    d = EQUITY_BARS[sym]
    print(f"  {sym:<8} {d['first_close']:>10.2f} {d['last_close']:>10.2f} {mom:>+9.2f}%{sel}")
print()

# --- Crypto momentum table ---
print("CRYPTO MOMENTUM (20-day):")
print(f"  {'Symbol':<10} {'Start':>12} {'End':>12} {'Momentum':>10}  {'Selected'}")
print(f"  {'-'*10} {'-'*12} {'-'*12} {'-'*10}  {'-'*8}")
for sym, mom in cr_ranked:
    sel = " <-- TOP 2" if sym in top_crypto else ""
    d = CRYPTO_BARS[sym]
    print(f"  {sym:<10} {d['first_close']:>12.2f} {d['last_close']:>12.3f} {mom:>+9.2f}%{sel}")
print()

# --- Equity orders (limit at last close, GTC so they persist to Monday) ---
print("EQUITY ORDERS (limit buy at last close, GTC):")
print(f"  Budget per position: ${eq_per_position:,.0f}")
print()

for sym in top_equities:
    price = EQUITY_LAST_CLOSE[sym]
    shares = math.floor(eq_per_position / price)
    notional = shares * price
    floor_price = round(price * (1 - LOSS_PCT / 100), 2)

    orders.append({
        "asset_class": "equity",
        "symbol": sym,
        "side": "buy",
        "qty": shares,
        "order_type": "limit",
        "limit_price": price,
        "time_in_force": "gtc",
        "notional_approx": round(notional, 2),
        "momentum_pct": round(equity_momentum[sym], 2),
        "trail_pct": TRAIL_PCT,
        "loss_pct": LOSS_PCT,
        "initial_floor": floor_price,
    })
    print(f"  {sym}: {shares} shares @ ${price:.2f} limit = ${notional:,.2f}")
    print(f"    Momentum: {equity_momentum[sym]:+.2f}%  |  Stop floor: ${floor_price:.2f}")

print()

# --- Crypto orders (market, crypto trades 24/7) ---
print("CRYPTO ORDERS (market buy, crypto is 24/7):")
print(f"  Budget per position: ${cr_per_position:,.0f}")
print()

for sym in top_crypto:
    ask = CRYPTO_ASK[sym]
    # Fractional crypto — calculate quantity to 8 decimal places
    qty = cr_per_position / ask
    qty_rounded = math.floor(qty * 1e8) / 1e8   # floor to 8 decimals
    notional = qty_rounded * ask
    floor_price = round(ask * (1 - LOSS_PCT / 100), 2)

    orders.append({
        "asset_class": "crypto",
        "symbol": sym,
        "side": "buy",
        "qty": qty_rounded,
        "order_type": "market",
        "limit_price": None,
        "time_in_force": "gtc",
        "notional_approx": round(notional, 2),
        "momentum_pct": round(crypto_momentum[sym], 2),
        "trail_pct": TRAIL_PCT,
        "loss_pct": LOSS_PCT,
        "initial_floor": floor_price,
    })
    print(f"  {sym}: {qty_rounded:.8f} units @ ~${ask:,.2f} ask = ~${notional:,.2f}")
    print(f"    Momentum: {crypto_momentum[sym]:+.2f}%  |  Stop floor: ~${floor_price:,.2f}")

print()

# ---------------------------------------------------------------------------
# Place orders via Alpaca API
# ---------------------------------------------------------------------------
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

placed_orders = []

print("PLACING ORDERS...")
print("-" * 70)

for o in orders:
    sym = o["symbol"]
    try:
        if o["asset_class"] == "equity":
            req = LimitOrderRequest(
                symbol=sym,
                qty=o["qty"],
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                limit_price=o["limit_price"],
            )
            result = client.submit_order(req)
        else:
            # Crypto market order
            req = MarketOrderRequest(
                symbol=sym,
                qty=o["qty"],
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            )
            result = client.submit_order(req)

        order_id = str(result.id)
        status = str(result.status).split(".")[-1] if result.status else "submitted"
        filled_price = float(result.filled_avg_price) if result.filled_avg_price else None
        filled_qty = float(result.filled_qty) if result.filled_qty else 0

        o["order_id"] = order_id
        o["api_status"] = status
        o["filled_price"] = filled_price
        o["filled_qty"] = filled_qty
        placed_orders.append(o)

        price_str = f"${filled_price:,.2f}" if filled_price else "pending"
        print(f"  OK  {sym:<10} order_id={order_id[:12]}...  status={status}  fill={price_str}")

    except Exception as e:
        o["order_id"] = None
        o["api_status"] = "ERROR"
        o["error"] = str(e)
        placed_orders.append(o)
        print(f"  ERR {sym:<10} {e}")

print()

# ---------------------------------------------------------------------------
# Update state/trailing_stops.json
# ---------------------------------------------------------------------------
stops_file = STATE_DIR / "trailing_stops.json"
stops_data = {"schema_version": "1.0.0", "active_stops": [], "closed_stops": []}
if stops_file.exists():
    with open(stops_file, "r", encoding="utf-8") as f:
        stops_data = json.load(f)

for o in placed_orders:
    if o.get("order_id") is None:
        continue

    is_crypto = o["asset_class"] == "crypto"
    filled = o.get("filled_price") is not None and o["filled_qty"] > 0

    entry_price = o["filled_price"] if filled else o["limit_price"] or CRYPTO_ASK.get(o["symbol"], 0)
    floor_price = round(entry_price * (1 - o["loss_pct"] / 100), 2)

    stop_entry = {
        "symbol": o["symbol"],
        "asset_class": o["asset_class"],
        "entry_order_id": o["order_id"],
        "qty": o["qty"],
        "entry_price": entry_price,
        "highest_price": entry_price,
        "floor_price": floor_price,
        "trail_pct": o["trail_pct"],
        "loss_pct": o["loss_pct"],
        "status": "ACTIVE" if (is_crypto and filled) else "PENDING_FILL",
        "trailing_stop_order_id": None,
        "opened_at": now_iso,
        "last_checked": now_iso,
        "momentum_pct_at_entry": o["momentum_pct"],
    }
    stops_data["active_stops"].append(stop_entry)

atomic_write_json(stops_file, stops_data)
print(f"Updated {stops_file}")

# ---------------------------------------------------------------------------
# Update state/positions.json
# ---------------------------------------------------------------------------
pos_file = STATE_DIR / "positions.json"
pos_data = {
    "schema_version": "1.0.0",
    "last_reconciled": now_iso,
    "positions": [],
    "pending_orders": [],
    "totals": {}
}
if pos_file.exists():
    with open(pos_file, "r", encoding="utf-8") as f:
        pos_data = json.load(f)

total_deployed = 0.0

for o in placed_orders:
    if o.get("order_id") is None:
        continue

    is_crypto = o["asset_class"] == "crypto"
    filled = o.get("filled_price") is not None and o["filled_qty"] > 0

    if filled:
        pos_data["positions"].append({
            "symbol": o["symbol"],
            "asset_class": o["asset_class"],
            "strategy": "TRAILING_STOP",
            "qty": o["filled_qty"],
            "entry_price": o["filled_price"],
            "current_price": o["filled_price"],
            "market_value": round(o["filled_qty"] * o["filled_price"], 2),
            "unrealized_pnl": 0,
            "order_id": o["order_id"],
            "opened_at": now_iso,
        })
        total_deployed += o["filled_qty"] * o["filled_price"]
    else:
        entry_est = o["limit_price"] if o["limit_price"] else CRYPTO_ASK.get(o["symbol"], 0)
        est_value = o["qty"] * entry_est
        pos_data["pending_orders"].append({
            "symbol": o["symbol"],
            "asset_class": o["asset_class"],
            "strategy": "TRAILING_STOP",
            "qty": o["qty"],
            "limit_price": o["limit_price"],
            "order_type": o["order_type"],
            "order_id": o["order_id"],
            "status": o["api_status"],
            "estimated_value": round(est_value, 2),
            "placed_at": now_iso,
        })
        total_deployed += est_value

cash_available = 100_000 - total_deployed
pos_data["totals"] = {
    "total_positions": len(pos_data["positions"]),
    "total_pending": len(pos_data["pending_orders"]),
    "total_value": round(total_deployed, 2),
    "cash_available": round(cash_available, 2),
    "cash_reserved_pct": round((cash_available / 100_000) * 100, 1),
    "deployed_pct": round((total_deployed / 100_000) * 100, 1),
}
pos_data["last_reconciled"] = now_iso

atomic_write_json(pos_file, pos_data)
print(f"Updated {pos_file}")

# ---------------------------------------------------------------------------
# Append to logs/trades.jsonl
# ---------------------------------------------------------------------------
trades_log = LOGS_DIR / "trades.jsonl"

for o in placed_orders:
    if o.get("order_id") is None:
        continue

    filled = o.get("filled_price") is not None and o["filled_qty"] > 0

    record = {
        "timestamp": now_iso,
        "strategy": "TRAILING_STOP",
        "action": "BUY",
        "symbol": o["symbol"],
        "asset_class": o["asset_class"],
        "qty": o["filled_qty"] if filled else o["qty"],
        "price": o["filled_price"] if filled else o["limit_price"],
        "order_type": o["order_type"],
        "order_id": o["order_id"],
        "status": "filled" if filled else "pending",
        "momentum_pct": o["momentum_pct"],
        "pnl": 0,
    }
    append_jsonl(trades_log, record)

print(f"Appended {len([o for o in placed_orders if o.get('order_id')])} records to {trades_log}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("ORDER SUMMARY")
print("=" * 70)
print()

for o in placed_orders:
    sym = o["symbol"]
    oid = (o.get("order_id") or "FAILED")[:16]
    status = o.get("api_status", "?")
    filled = o.get("filled_price") is not None and o.get("filled_qty", 0) > 0

    if filled:
        print(f"  {sym:<10}  FILLED  {o['filled_qty']:.8g} @ ${o['filled_price']:,.2f}"
              f"  = ${o['filled_qty'] * o['filled_price']:,.2f}  order={oid}")
    else:
        price = o.get("limit_price") or CRYPTO_ASK.get(sym, 0)
        val = o["qty"] * price
        print(f"  {sym:<10}  {status:<8}  {o['qty']:.8g} @ ${price:,.2f} limit"
              f"  ~ ${val:,.2f}  order={oid}")

print()
print(f"Total deployed / committed: ${total_deployed:,.2f}")
print(f"Cash remaining:             ${cash_available:,.2f}")
print(f"Cash reserve:               {pos_data['totals']['cash_reserved_pct']:.1f}%")
print(f"Deployed:                   {pos_data['totals']['deployed_pct']:.1f}%")
print()
print("NOTE: Trailing stop SELL orders will be placed AFTER buy orders fill.")
print("  - Equities: limit orders fill at Monday open (2026-04-13)")
print("  - Crypto: market orders should fill immediately (24/7 market)")
print()
print("Done.")
