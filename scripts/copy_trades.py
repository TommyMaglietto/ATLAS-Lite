#!/usr/bin/env python3
"""
ATLAS Lite - Phase 3: Politician Copy Trading
Places limit buy orders for top 2 bipartisan overlap symbols from Ro Khanna's
recent buy wave. Market is closed (Saturday) so orders will fill Monday.

Rules:
  - Max $5,000 per position (5% of $100K)
  - Prioritize bipartisan overlap symbols (both parties buying)
  - Up to $10K total deployed for copy trades
  - Limit orders at current ask price
  - Atomic writes for state files
  - Log everything to trades.jsonl
"""

import os
import sys
import json
import math
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from atomic_write import atomic_write_json, atomic_read_json

CONFIG_DIR = PROJECT_ROOT / "config"
STATE_DIR = PROJECT_ROOT / "state"
LOG_DIR = PROJECT_ROOT / "logs"

WATCHLIST_FILE = CONFIG_DIR / "politician_watchlist.json"
COPY_TRADES_FILE = STATE_DIR / "copy_trades.json"
TRADES_LOG = LOG_DIR / "trades.jsonl"

# ---------------------------------------------------------------------------
# Alpaca setup - ALWAYS paper trading
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")
PAPER = True  # ALWAYS paper trading per CLAUDE.md rules

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.enums import DataFeed

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_POSITION_USD = 5000.0   # 5% of $100K account
MAX_TOTAL_USD = 10000.0     # Total budget for copy trades
NUM_POSITIONS = 2           # Top 2 symbols

print("=" * 64)
print("  ATLAS Lite - Phase 3: Politician Copy Trading")
print("=" * 64)
print()

# ---------------------------------------------------------------------------
# Step 1: Read politician watchlist and get recent buy signals
# ---------------------------------------------------------------------------
print("[1] Reading politician watchlist ...")
watchlist = atomic_read_json(str(WATCHLIST_FILE))
if not watchlist:
    print("ERROR: Could not read politician_watchlist.json")
    sys.exit(1)

tracked = watchlist["tracked_politicians"]
bipartisan_symbols = watchlist.get("bipartisan_overlap_symbols", [])
primary_politician = None
for p in tracked:
    if p.get("priority") == "primary":
        primary_politician = p
        break

if not primary_politician:
    print("ERROR: No primary politician found in watchlist")
    sys.exit(1)

print(f"     Primary politician : {primary_politician['name']} ({primary_politician['party']}-{primary_politician['state']})")
print(f"     Recent buys        : {len(primary_politician['recent_buys'])}")
print(f"     Bipartisan overlap : {bipartisan_symbols}")
print()

# ---------------------------------------------------------------------------
# Step 2: Prioritize symbols - bipartisan first, then largest positions
# ---------------------------------------------------------------------------
print("[2] Prioritizing symbols ...")

# Build ranked list: bipartisan overlap first (UNH, ADP), then Khanna's largest
recent_buys = primary_politician["recent_buys"]

# Separate bipartisan symbols from the buy list
bipartisan_buys = [b for b in recent_buys if b["symbol"] in bipartisan_symbols]
non_bipartisan_buys = [b for b in recent_buys if b["symbol"] not in bipartisan_symbols]

# Size ranking: 15K-50K > 1K-15K
def size_rank(size_str):
    if "50K-100K" in size_str:
        return 4
    if "15K-50K" in size_str:
        return 3
    if "1K-15K" in size_str:
        return 2
    return 1

# Sort bipartisan by size (largest first), then non-bipartisan by size
bipartisan_buys.sort(key=lambda b: size_rank(b["size"]), reverse=True)
non_bipartisan_buys.sort(key=lambda b: size_rank(b["size"]), reverse=True)

# Final ranked list: bipartisan first, then rest
ranked = bipartisan_buys + non_bipartisan_buys
selected = ranked[:NUM_POSITIONS]

print(f"     Selected {NUM_POSITIONS} symbols:")
for i, s in enumerate(selected):
    bp = "BIPARTISAN" if s["symbol"] in bipartisan_symbols else "single-party"
    print(f"       [{i+1}] {s['symbol']} - size: {s['size']}, date: {s['date']}, signal: {bp}")
print()

# ---------------------------------------------------------------------------
# Step 3: Get current quotes for selected symbols
# ---------------------------------------------------------------------------
print("[3] Fetching current quotes ...")

symbols = [s["symbol"] for s in selected]
quotes = {}

for sym in symbols:
    req = StockLatestQuoteRequest(
        symbol_or_symbols=sym,
        feed=DataFeed.IEX
    )
    result = data_client.get_stock_latest_quote(req)
    q = result[sym]
    ask = float(q.ask_price) if q.ask_price else None
    bid = float(q.bid_price) if q.bid_price else None

    # Use ask for limit buy (worst case), fall back to bid, then midpoint
    if ask and ask > 0:
        price = ask
    elif bid and bid > 0:
        price = bid
    else:
        print(f"     WARNING: No valid quote for {sym}, skipping")
        continue

    # If market closed, ask/bid may be stale; use the best available
    mid = round((ask + bid) / 2, 2) if (ask and bid and ask > 0 and bid > 0) else price
    quotes[sym] = {"ask": ask, "bid": bid, "mid": mid, "limit_price": round(mid, 2)}
    print(f"     {sym}: bid=${bid}, ask=${ask}, mid=${mid} -> limit=${quotes[sym]['limit_price']}")

print()

# ---------------------------------------------------------------------------
# Step 4: Calculate share counts and place limit buy orders
# ---------------------------------------------------------------------------
print("[4] Placing limit buy orders ...")
print(f"     Budget per position: ${MAX_POSITION_USD:,.2f}")
print(f"     Total budget: ${MAX_TOTAL_USD:,.2f}")
print()

orders_placed = []
total_deployed = 0.0

for entry in selected:
    sym = entry["symbol"]
    if sym not in quotes:
        print(f"     SKIP {sym}: no valid quote")
        continue

    limit_price = quotes[sym]["limit_price"]
    qty = math.floor(MAX_POSITION_USD / limit_price)

    if qty < 1:
        print(f"     SKIP {sym}: price ${limit_price} exceeds ${MAX_POSITION_USD} budget")
        continue

    notional = round(qty * limit_price, 2)
    total_deployed += notional

    print(f"     Placing LIMIT BUY: {qty} shares of {sym} @ ${limit_price}")
    print(f"       Notional: ${notional:,.2f}")

    try:
        order_req = LimitOrderRequest(
            symbol=sym,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,  # Good-til-cancelled: will fill Monday
            limit_price=limit_price,
        )
        order = trading.submit_order(order_req)
        order_id = str(order.id)
        order_status = str(order.status)

        print(f"       Order ID: {order_id}")
        print(f"       Status: {order_status}")
        print()

        orders_placed.append({
            "symbol": sym,
            "source_politician": primary_politician["name"],
            "bipartisan": sym in bipartisan_symbols,
            "filing_date": entry["date"],
            "order_id": order_id,
            "entry_price": None,  # Will be filled Monday
            "limit_price": limit_price,
            "qty": qty,
            "notional": notional,
            "status": "PENDING_FILL",
            "order_status": order_status,
            "order_timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        print(f"       ERROR placing order for {sym}: {e}")
        print()

# ---------------------------------------------------------------------------
# Step 5: Update state/copy_trades.json (atomic write)
# ---------------------------------------------------------------------------
print("[5] Updating state/copy_trades.json ...")

copy_state = atomic_read_json(str(COPY_TRADES_FILE))
if not copy_state:
    copy_state = {
        "schema_version": "1.0.0",
        "tracked_politician": None,
        "replicated_trades": [],
        "pending_signals": [],
        "performance": {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0,
            "win_rate": 0,
        },
    }

copy_state["tracked_politician"] = primary_politician["name"]
copy_state["replicated_trades"].extend(orders_placed)
copy_state["last_updated"] = datetime.now(timezone.utc).isoformat()
copy_state["total_deployed_notional"] = total_deployed
copy_state["regime_at_entry"] = "RISK_ON"

success = atomic_write_json(str(COPY_TRADES_FILE), copy_state)
if success:
    print(f"     State written successfully")
else:
    print(f"     ERROR: Failed to write state file")
print()

# ---------------------------------------------------------------------------
# Step 6: Append to logs/trades.jsonl
# ---------------------------------------------------------------------------
print("[6] Logging trades ...")

LOG_DIR.mkdir(parents=True, exist_ok=True)

for order in orders_placed:
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "copy_trade_order",
        "strategy": "politician_copy",
        "symbol": order["symbol"],
        "side": "buy",
        "order_type": "limit",
        "qty": order["qty"],
        "limit_price": order["limit_price"],
        "notional": order["notional"],
        "order_id": order["order_id"],
        "order_status": order["order_status"],
        "source_politician": order["source_politician"],
        "bipartisan": order["bipartisan"],
        "filing_date": order["filing_date"],
        "regime": "RISK_ON",
        "paper": PAPER,
    }
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")
    print(f"     Logged: {order['symbol']} {order['qty']} shares @ ${order['limit_price']}")

print()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("=" * 64)
print("  POLITICIAN COPY TRADING SUMMARY")
print("=" * 64)
print(f"  Tracked politician    : {primary_politician['name']}")
print(f"  Regime                : RISK_ON (full size)")
print(f"  Orders placed         : {len(orders_placed)}")
for o in orders_placed:
    bp_tag = " [BIPARTISAN]" if o["bipartisan"] else ""
    print(f"    - {o['symbol']}: {o['qty']} shares @ ${o['limit_price']} "
          f"(${o['notional']:,.2f}){bp_tag}")
print(f"  Total capital deployed: ${total_deployed:,.2f}")
print(f"  Order type            : LIMIT BUY (GTC)")
print(f"  Expected fill         : Monday 2026-04-13 at market open")
print(f"  State file updated    : {COPY_TRADES_FILE}")
print(f"  Trades logged         : {TRADES_LOG}")
print("=" * 64)
print()
print("  Done. Orders are pending fill for Monday market open.")
print()
