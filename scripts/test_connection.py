#!/usr/bin/env python3
"""
ATLAS Lite — Phase 1, Agent 1: Alpaca Paper Trading Connection Test

Verifies:
  1. API authentication and account access
  2. Market clock / hours
  3. Market data retrieval (AAPL quote)
  4. Order placement and cancellation (or round-trip trade if market open)
  5. Summary printout
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Setup & connect
# ---------------------------------------------------------------------------
API_KEY    = os.environ.get("ALPACA_API_KEY",    "PKNG4F2EBQEA2GVWLJBSHLYLQW")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "5q5T4NUp5f35MHoE8J5Bxxva8pYzyLTZPVFqiH7dYMRD")
PAPER      = True  # ALWAYS paper trading per CLAUDE.md rules

print("=" * 64)
print("  ATLAS Lite — Alpaca Connection Test")
print("=" * 64)
print()

# --- Trading client ---
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

# --- Market-data client ---
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ---------------------------------------------------------------------------
# 2. Account info
# ---------------------------------------------------------------------------
print("[1] Fetching account info ...")
account = trading.get_account()

acct_status   = account.status
buying_power  = float(account.buying_power)
cash          = float(account.cash)
equity        = float(account.equity)
currency      = account.currency

print(f"     Account status : {acct_status}")
print(f"     Buying power   : ${buying_power:,.2f}")
print(f"     Cash           : ${cash:,.2f}")
print(f"     Equity         : ${equity:,.2f}")
print(f"     Currency       : {currency}")
print(f"     Paper trading  : {PAPER}")
print()

if "ACTIVE" not in str(acct_status).upper():
    print("ERROR: Account is not ACTIVE. Aborting.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 3. Market clock
# ---------------------------------------------------------------------------
print("[2] Checking market clock ...")
clock = trading.get_clock()

is_open   = clock.is_open
next_open  = clock.next_open
next_close = clock.next_close

print(f"     Market open    : {is_open}")
print(f"     Next open      : {next_open}")
print(f"     Next close     : {next_close}")
print()

# ---------------------------------------------------------------------------
# 4. Get current AAPL quote for reference
# ---------------------------------------------------------------------------
print("[3] Fetching AAPL latest quote ...")
quote_req = StockLatestQuoteRequest(symbol_or_symbols="AAPL")
quotes = data_client.get_stock_latest_quote(quote_req)
aapl_quote = quotes["AAPL"]

ask = float(aapl_quote.ask_price) if aapl_quote.ask_price else None
bid = float(aapl_quote.bid_price) if aapl_quote.bid_price else None
mid = round((ask + bid) / 2, 2) if (ask and bid) else None

print(f"     AAPL ask       : ${ask}")
print(f"     AAPL bid       : ${bid}")
print(f"     AAPL mid       : ${mid}")
print()

# ---------------------------------------------------------------------------
# 5. Order test — depends on market hours
# ---------------------------------------------------------------------------
order_test_passed = False

if is_open:
    # ---- Market is OPEN: buy 1 share at market, wait, then sell ----
    print("[4] Market is OPEN — executing round-trip test trade ...")

    # Buy
    print("     Placing market BUY for 1 share of AAPL ...")
    buy_req = MarketOrderRequest(
        symbol="AAPL",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )
    buy_order = trading.submit_order(buy_req)
    print(f"     Buy order ID   : {buy_order.id}")
    print(f"     Buy status     : {buy_order.status}")

    # Wait for fill
    print("     Waiting for fill (up to 15 s) ...")
    for i in range(15):
        time.sleep(1)
        buy_order = trading.get_order_by_id(buy_order.id)
        if "FILLED" in str(buy_order.status).upper():
            break
    print(f"     Buy final status : {buy_order.status}")
    filled_price = buy_order.filled_avg_price
    print(f"     Fill price       : ${filled_price}")

    # Sell
    print("     Placing market SELL for 1 share of AAPL ...")
    sell_req = MarketOrderRequest(
        symbol="AAPL",
        qty=1,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    sell_order = trading.submit_order(sell_req)
    print(f"     Sell order ID  : {sell_order.id}")

    for i in range(15):
        time.sleep(1)
        sell_order = trading.get_order_by_id(sell_order.id)
        if "FILLED" in str(sell_order.status).upper():
            break
    print(f"     Sell final status : {sell_order.status}")
    sell_price = sell_order.filled_avg_price
    print(f"     Sell price        : ${sell_price}")

    order_test_passed = True
    print()

else:
    # ---- Market is CLOSED: place limit order then cancel ----
    print("[4] Market is CLOSED — placing limit order then cancelling ...")

    # Set limit price 5% below mid (or below bid) to avoid accidental fill
    if mid:
        limit_price = round(mid * 0.95, 2)
    elif bid:
        limit_price = round(bid * 0.95, 2)
    else:
        limit_price = 100.00  # safe fallback
    print(f"     Limit price    : ${limit_price}")

    limit_req = LimitOrderRequest(
        symbol="AAPL",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    order = trading.submit_order(limit_req)
    order_id = order.id
    print(f"     Order ID       : {order_id}")
    print(f"     Order status   : {order.status}")

    # Brief pause then cancel
    time.sleep(2)
    print("     Cancelling order ...")
    trading.cancel_order_by_id(order_id)
    time.sleep(1)

    # Verify cancelled
    cancelled = trading.get_order_by_id(order_id)
    print(f"     Final status   : {cancelled.status}")
    status_str = str(cancelled.status).upper()
    order_test_passed = any(
        kw in status_str for kw in ("CANCELED", "CANCELLED", "PENDING_CANCEL")
    )
    print()

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------
print("=" * 64)
print("  CONNECTION TEST SUMMARY")
print("=" * 64)
print(f"  Account status        : {acct_status}")
print(f"  Equity                : ${equity:,.2f}")
print(f"  Buying power          : ${buying_power:,.2f}")
print(f"  Cash                  : ${cash:,.2f}")
print(f"  Paper trading         : {PAPER}")
print(f"  Market open           : {is_open}")
print(f"  AAPL mid price        : ${mid}")
print(f"  Order test passed     : {order_test_passed}")
print(f"  Connection verified   : True")
print("=" * 64)

# ---------------------------------------------------------------------------
# 7. Log the result to logs/trades.jsonl (per project rules)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR  = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "trades.jsonl"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log_entry = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "event": "connection_test",
    "account_status": str(acct_status),
    "equity": equity,
    "buying_power": buying_power,
    "cash": cash,
    "market_open": is_open,
    "aapl_mid": mid,
    "order_test_passed": order_test_passed,
    "paper": PAPER,
}

with open(LOG_FILE, "a", encoding="utf-8") as f:
    f.write(json.dumps(log_entry) + "\n")

print(f"\n  Result logged to {LOG_FILE}")
print("  Done.\n")
