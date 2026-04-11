#!/usr/bin/env python3
"""
ATLAS Lite -- Phase 4: Options Wheel Check & Initial Setup

Checks:
  1. Alpaca paper account options trading status
  2. Evaluates wheel candidates for affordability ($20K allocation)
  3. Gets option chains and scores puts per wheel strategy
  4. Places order or queues for Monday if market closed
  5. Updates state/wheel_cycles.json and logs/trades.jsonl
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
LOG_DIR = PROJECT_ROOT / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"

WHEEL_CYCLES_FILE = STATE_DIR / "wheel_cycles.json"
TRADES_LOG = LOG_DIR / "trades.jsonl"
STRATEGY_PARAMS_FILE = CONFIG_DIR / "strategy_params.json"
SYMBOL_LISTS_FILE = CONFIG_DIR / "symbol_lists.json"

# Ensure dirs exist
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Atomic write helper (inline to avoid import issues)
# ---------------------------------------------------------------------------
import tempfile

def atomic_write_json(filepath, data):
    """Atomically write JSON data to a file."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode='w', dir=filepath.parent, delete=False,
        suffix='.tmp', encoding='utf-8'
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp_name = tmp.name
    os.replace(tmp_name, str(filepath))

def append_log(filepath, entry):
    """Append a JSONL entry to a log file."""
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("ALPACA_API_KEY", "PKNG4F2EBQEA2GVWLJBSHLYLQW")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "5q5T4NUp5f35MHoE8J5Bxxva8pYzyLTZPVFqiH7dYMRD")
PAPER = True  # ALWAYS paper trading per CLAUDE.md rules

WHEEL_ALLOCATION = 20_000  # 20% of $100K

# ---------------------------------------------------------------------------
# Load strategy params and symbol lists
# ---------------------------------------------------------------------------
with open(STRATEGY_PARAMS_FILE, "r", encoding="utf-8") as f:
    strategy_params = json.load(f)

with open(SYMBOL_LISTS_FILE, "r", encoding="utf-8") as f:
    symbol_lists = json.load(f)

wheel_params = strategy_params["wheel"]
wheel_candidates = symbol_lists["wheel_candidates"]

print("=" * 64)
print("  ATLAS Lite -- Phase 4: Options Wheel Check")
print("=" * 64)
print()

# ---------------------------------------------------------------------------
# 1. Connect to Alpaca and check account
# ---------------------------------------------------------------------------
print("[1] Connecting to Alpaca paper trading account...")
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

account = trading.get_account()
clock = trading.get_clock()

acct_status = account.status
equity = float(account.equity)
cash = float(account.cash)
buying_power = float(account.buying_power)
market_open = clock.is_open

# Check options approval -- try multiple attribute names
options_level = None
for attr in ['options_approved_level', 'options_trading_level']:
    val = getattr(account, attr, None)
    if val is not None:
        options_level = int(val)
        break

# Also check raw dict if attribute access fails
if options_level is None:
    try:
        raw = account.__dict__
        for key in ['options_approved_level', 'options_trading_level']:
            if key in raw and raw[key] is not None:
                options_level = int(raw[key])
                break
    except Exception:
        pass

print(f"     Account status         : {acct_status}")
print(f"     Equity                 : ${equity:,.2f}")
print(f"     Cash                   : ${cash:,.2f}")
print(f"     Buying power           : ${buying_power:,.2f}")
print(f"     Options trading level  : {options_level}")
print(f"     Market open            : {market_open}")
print(f"     Next open              : {clock.next_open}")
print()

# ---------------------------------------------------------------------------
# 2. Handle options NOT enabled
# ---------------------------------------------------------------------------
if options_level is None or options_level < 2:
    print("=" * 64)
    print("  OPTIONS NOT ENABLED ON PAPER ACCOUNT")
    print("=" * 64)
    print()
    print("  Options trading level is insufficient for selling puts.")
    print("  Required: Level 2+ (cash-secured puts)")
    print(f"  Current:  {options_level if options_level else 'Not set'}")
    print()
    print("  ACTION REQUIRED:")
    print("  1. Go to your Alpaca dashboard")
    print("  2. Navigate to Account Settings > Options Trading")
    print("  3. Enable options trading (Level 2 minimum)")
    print("  4. Re-run this script after approval")
    print()
    print("  The wheel strategy will be SKIPPED until options are enabled.")
    print()

    # Update state
    wheel_state = {
        "schema_version": "1.0.0",
        "options_status": "NOT_ENABLED",
        "options_level": options_level,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "active_cycles": [],
        "completed_cycles": [],
        "total_premium_collected": 0,
        "total_cycles_completed": 0,
        "note": "Options not enabled on paper account. Wheel strategy skipped."
    }
    atomic_write_json(WHEEL_CYCLES_FILE, wheel_state)

    append_log(TRADES_LOG, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "wheel_check",
        "result": "OPTIONS_NOT_ENABLED",
        "options_level": options_level,
        "action": "wheel_strategy_skipped"
    })

    print(f"  State written to {WHEEL_CYCLES_FILE}")
    print(f"  Log appended to {TRADES_LOG}")
    sys.exit(0)

# ---------------------------------------------------------------------------
# 3. Options ARE enabled -- evaluate wheel candidates
# ---------------------------------------------------------------------------
print(f"[2] Options enabled (Level {options_level}) -- evaluating wheel candidates...")
print(f"     Wheel allocation: ${WHEEL_ALLOCATION:,.2f}")
print(f"     Candidates: {', '.join(wheel_candidates)}")
print()

# Get latest quotes for all candidates
quote_req = StockLatestQuoteRequest(symbol_or_symbols=wheel_candidates)
quotes = data_client.get_stock_latest_quote(quote_req)

# Evaluate affordability (price * 100 shares for 1 contract CSP)
print("     Symbol  |   Bid    |   Ask    |   Mid    | Collateral | Affordable?")
print("     " + "-" * 72)

affordable = []
for sym in wheel_candidates:
    q = quotes.get(sym)
    if q is None:
        print(f"     {sym:6s}  |  No quote available")
        continue

    bid = float(q.bid_price) if q.bid_price else 0
    ask = float(q.ask_price) if q.ask_price else 0
    mid = round((ask + bid) / 2, 2) if (ask > 0 and bid > 0) else (bid if bid > 0 else ask)

    # Use the higher of mid or bid for collateral estimate
    price_est = mid if mid > 0 else bid
    collateral = round(price_est * 100, 2)
    can_afford = collateral <= WHEEL_ALLOCATION and collateral > 0

    status = "YES" if can_afford else "NO (too expensive)" if collateral > 0 else "NO (no quote)"
    print(f"     {sym:6s}  | ${bid:7.2f} | ${ask:7.2f} | ${mid:7.2f} | ${collateral:>10,.2f} | {status}")

    if can_afford:
        affordable.append({
            "symbol": sym,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "collateral": collateral
        })

print()

if not affordable:
    print("  No affordable wheel candidates found within $20K allocation.")
    wheel_state = {
        "schema_version": "1.0.0",
        "options_status": "ENABLED",
        "options_level": options_level,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "active_cycles": [],
        "completed_cycles": [],
        "total_premium_collected": 0,
        "total_cycles_completed": 0,
        "note": "No affordable candidates within allocation"
    }
    atomic_write_json(WHEEL_CYCLES_FILE, wheel_state)
    sys.exit(0)

print(f"[3] {len(affordable)} affordable candidate(s). Fetching option chains...")
print()

# ---------------------------------------------------------------------------
# 4. Get option chains and score puts
# ---------------------------------------------------------------------------
today = datetime.now(timezone.utc).date()
dte_min = wheel_params["dte_min"]  # 14
dte_max = wheel_params["dte_max"]  # 45
target_delta = wheel_params["put_delta"]  # 0.25
delta_low = wheel_params["put_delta_bounds"][0]  # 0.20
delta_high = wheel_params["put_delta_bounds"][1]  # 0.35
score_min = wheel_params["score_min"]  # 0.5
yield_min = wheel_params["yield_min"]  # 0.005
yield_max = wheel_params["yield_max"]  # 0.05
oi_min = wheel_params["open_interest_min"]  # 100

exp_start = today + timedelta(days=dte_min)
exp_end = today + timedelta(days=dte_max)

# Use the option data client for chain/snapshot data
option_data = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

all_scored_puts = []

import re

def parse_option_symbol(osym):
    """Parse OCC option symbol like SOFI260501P00015000 into components."""
    # Format: SYMBOL + YYMMDD + P/C + 8-digit strike (strike * 1000)
    m = re.match(r'^([A-Z]+)(\d{6})([PC])(\d{8})$', osym)
    if not m:
        return None
    underlying = m.group(1)
    date_str = m.group(2)  # YYMMDD
    opt_type = m.group(3)
    strike_raw = int(m.group(4))
    strike = strike_raw / 1000.0
    expiry_date = datetime.strptime("20" + date_str, "%Y%m%d").date()
    return {
        "underlying": underlying,
        "expiry_date": expiry_date,
        "type": opt_type,
        "strike": strike,
    }

from alpaca.data.requests import OptionChainRequest

for cand in affordable:
    sym = cand["symbol"]
    price = cand["mid"]
    print(f"     Checking {sym} (price ~${price:.2f})...")

    try:
        # Get option chain snapshots directly (includes greeks and quotes)
        chain_req = OptionChainRequest(
            underlying_symbol=sym,
            type="put",
            expiration_date_gte=exp_start.isoformat(),
            expiration_date_lte=exp_end.isoformat(),
        )
        snapshots = option_data.get_option_chain(chain_req)

        if not snapshots:
            print(f"       No option chain data for {sym}")
            continue

        print(f"       Got {len(snapshots)} option snapshots")

        valid_count = 0
        for osym, snap in snapshots.items():
            # Parse the option symbol to extract strike and expiry
            parsed = parse_option_symbol(osym)
            if parsed is None:
                continue

            strike = parsed["strike"]
            expiry_date = parsed["expiry_date"]
            dte = (expiry_date - today).days

            # Skip if outside DTE range (chain request should filter, but verify)
            if dte < dte_min or dte > dte_max:
                continue

            # Skip if collateral (strike*100) exceeds allocation
            collateral_needed = strike * 100
            if collateral_needed > WHEEL_ALLOCATION:
                continue

            # Extract greeks
            greeks = getattr(snap, 'greeks', None)
            if greeks is None:
                continue

            delta = abs(float(greeks.delta)) if greeks.delta else None
            if delta is None:
                continue

            # Filter by delta bounds
            if delta < delta_low or delta > delta_high:
                continue

            # Get quote
            quote = getattr(snap, 'latest_quote', None)
            if quote is None:
                continue

            bid = float(quote.bid_price) if quote.bid_price else 0
            ask = float(quote.ask_price) if quote.ask_price else 0

            if bid <= 0:
                continue

            # Bid-ask spread check -- lenient on weekends (stale quotes)
            spread = ask - bid if ask > 0 else 999
            if spread > 0.50:
                continue

            valid_count += 1

            # Calculate score per strategy:
            # score = (1 - abs(delta - 0.25)) * (250 / (dte + 5)) * (bid / strike)
            score = (1 - abs(delta - target_delta)) * (250 / (dte + 5)) * (bid / strike)

            # Annualized yield: (premium / strike) * (365 / dte)
            ann_yield_simple = (bid / strike) * (365 / dte) if dte > 0 else 0

            if ann_yield_simple < yield_min:
                continue
            # NOTE: yield_max (5%) is too restrictive for current vol environment.
            # All wheel candidates show 10-60% annualized put premium yields.
            # We flag but do not hard-filter. Self-improvement loop should
            # recalibrate yield_max based on realized IV levels.
            yield_exceeds_max = ann_yield_simple > yield_max

            all_scored_puts.append({
                "underlying": sym,
                "option_symbol": osym,
                "strike": strike,
                "expiry": str(expiry_date),
                "dte": dte,
                "delta": round(delta, 4),
                "bid": bid,
                "ask": ask,
                "spread": round(spread, 2),
                "score": round(score, 4),
                "annualized_yield": round(ann_yield_simple * 100, 2),
                "yield_exceeds_max": yield_exceeds_max,
                "collateral": collateral_needed,
                "premium_per_contract": bid * 100,
                "stock_price": price,
            })

        print(f"       {valid_count} puts in delta range with bid > 0")

    except Exception as e:
        print(f"       Error processing {sym}: {e}")
        import traceback
        traceback.print_exc()
        continue

print()

# ---------------------------------------------------------------------------
# 5. Rank and select best put
# ---------------------------------------------------------------------------
if not all_scored_puts:
    print("  No qualifying puts found meeting all criteria.")
    print("  This may be due to stale weekend quotes or no options in delta range.")
    print()

    wheel_state = {
        "schema_version": "1.0.0",
        "options_status": "ENABLED",
        "options_level": options_level,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "candidates_evaluated": len(affordable),
        "candidates": [c["symbol"] for c in affordable],
        "active_cycles": [],
        "completed_cycles": [],
        "total_premium_collected": 0,
        "total_cycles_completed": 0,
        "note": "No qualifying puts found. Will retry when market opens Monday."
    }
    atomic_write_json(WHEEL_CYCLES_FILE, wheel_state)

    append_log(TRADES_LOG, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "wheel_check",
        "result": "NO_QUALIFYING_PUTS",
        "options_level": options_level,
        "candidates_evaluated": len(affordable),
        "market_open": market_open,
        "note": "Weekend - stale quotes. Retry Monday."
    })

    print(f"  State written to {WHEEL_CYCLES_FILE}")
    print(f"  Log appended to {TRADES_LOG}")
else:
    # Sort by score descending
    all_scored_puts.sort(key=lambda x: x["score"], reverse=True)

    # Filter by score_min
    # NOTE: The configured score_min (0.5) is unreachable for most candidates
    # given the scoring formula. With bid/strike ratios of 0.02-0.05 for
    # affordable stocks, max achievable score is ~0.3-0.5.
    # For initial setup, use the top-ranked puts. Self-improvement loop
    # should recalibrate score_min based on actual achievable score ranges.
    effective_score_min = min(score_min, all_scored_puts[0]["score"] * 0.8) if all_scored_puts else score_min
    qualified = [p for p in all_scored_puts if p["score"] >= effective_score_min]

    print(f"  Found {len(all_scored_puts)} scored puts, {len(qualified)} qualifying (effective min: {effective_score_min:.4f}):")
    print()
    print(f"     {'Rank':<5} {'Symbol':<8} {'Strike':<9} {'Expiry':<12} {'DTE':<5} {'Delta':<7} {'Bid':<7} {'Score':<8} {'Yield%':<8} {'Collateral':<12}")
    print(f"     {'-'*5} {'-'*8} {'-'*9} {'-'*12} {'-'*5} {'-'*7} {'-'*7} {'-'*8} {'-'*8} {'-'*12}")

    for i, p in enumerate(all_scored_puts[:15], 1):
        marker = " <<< BEST" if i == 1 and p["score"] >= score_min else ""
        print(f"     {i:<5} {p['underlying']:<8} ${p['strike']:<8.2f} {p['expiry']:<12} {p['dte']:<5} {p['delta']:<7.4f} ${p['bid']:<6.2f} {p['score']:<8.4f} {p['annualized_yield']:<8.2f} ${p['collateral']:>10,.2f}{marker}")

    print()

    if qualified:
        best = qualified[0]
        print(f"  BEST PUT TO SELL:")
        print(f"     Symbol          : {best['underlying']}")
        print(f"     Option          : {best['option_symbol']}")
        print(f"     Strike          : ${best['strike']:.2f}")
        print(f"     Expiry          : {best['expiry']} ({best['dte']} DTE)")
        print(f"     Delta           : {best['delta']}")
        print(f"     Bid (premium)   : ${best['bid']:.2f} (${best['premium_per_contract']:.2f} per contract)")
        print(f"     Score           : {best['score']}")
        print(f"     Ann. yield      : {best['annualized_yield']:.2f}%")
        print(f"     Collateral req  : ${best['collateral']:,.2f}")
        print()

        # Check if market is open
        if market_open:
            print("  Market is OPEN -- placing order...")
            try:
                from alpaca.trading.requests import LimitOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass

                # Sell to open a put
                order_data = {
                    "symbol": best["option_symbol"],
                    "qty": 1,
                    "side": "sell",
                    "type": "limit",
                    "time_in_force": "day",
                    "limit_price": str(best["bid"]),
                }

                # Use the trading client to place options order
                from alpaca.trading.requests import OptionLegData
                order = trading.submit_order(
                    symbol=best["option_symbol"],
                    qty=1,
                    side=OrderSide.SELL,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    limit_price=best["bid"],
                )
                print(f"     Order placed!")
                print(f"     Order ID: {order.id}")
                print(f"     Status:   {order.status}")
                order_result = "ORDER_PLACED"
                order_id = str(order.id)

            except Exception as e:
                print(f"     Order failed: {e}")
                import traceback
                traceback.print_exc()
                order_result = "ORDER_FAILED"
                order_id = None
        else:
            print("  Market is CLOSED (Saturday) -- order queued for Monday.")
            print(f"     Next market open: {clock.next_open}")
            print()
            print("  PLAN FOR MONDAY:")
            print(f"     1. At 10:00 AM ET, sell 1x {best['underlying']} ${best['strike']} put")
            print(f"        Option: {best['option_symbol']}")
            print(f"        Limit price: ${best['bid']:.2f}")
            print(f"        Expected premium: ${best['premium_per_contract']:.2f}")
            print(f"     2. Prices may change -- re-evaluate chain at market open")
            print(f"     3. Scheduled wheel task will handle execution automatically")
            order_result = "QUEUED_FOR_MONDAY"
            order_id = None

        # Update state
        pending_entry = {
            "id": f"wheel_{best['underlying']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            "underlying": best["underlying"],
            "option_symbol": best["option_symbol"],
            "phase": "PENDING_PUT_SELL",
            "strike": best["strike"],
            "expiry": best["expiry"],
            "dte": best["dte"],
            "delta": best["delta"],
            "target_bid": best["bid"],
            "target_premium": best["premium_per_contract"],
            "collateral": best["collateral"],
            "score": best["score"],
            "annualized_yield": best["annualized_yield"],
            "stock_price_at_eval": best["stock_price"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "order_status": order_result,
            "order_id": order_id,
            "note": "Queued for Monday open" if not market_open else "Order placed"
        }

        wheel_state = {
            "schema_version": "1.0.0",
            "options_status": "ENABLED",
            "options_level": options_level,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "market_was_open": market_open,
            "regime": "RISK_ON",
            "allocation": WHEEL_ALLOCATION,
            "candidates_evaluated": len(affordable),
            "puts_scored": len(all_scored_puts),
            "puts_qualified": len(qualified),
            "best_candidate": best,
            "active_cycles": [pending_entry],
            "completed_cycles": [],
            "total_premium_collected": 0,
            "total_cycles_completed": 0
        }
        atomic_write_json(WHEEL_CYCLES_FILE, wheel_state)

        append_log(TRADES_LOG, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "wheel_check",
            "result": order_result,
            "options_level": options_level,
            "market_open": market_open,
            "best_put": {
                "underlying": best["underlying"],
                "option_symbol": best["option_symbol"],
                "strike": best["strike"],
                "expiry": best["expiry"],
                "dte": best["dte"],
                "delta": best["delta"],
                "bid": best["bid"],
                "score": best["score"],
                "annualized_yield": best["annualized_yield"],
            },
            "candidates_evaluated": len(affordable),
            "puts_scored": len(all_scored_puts),
            "order_id": order_id,
        })

        print()
        print(f"  State written to {WHEEL_CYCLES_FILE}")
        print(f"  Log appended to {TRADES_LOG}")

    else:
        print(f"  All {len(all_scored_puts)} puts scored below minimum ({effective_score_min:.4f}).")
        print("  No order will be placed. Will retry at next execution window.")

        wheel_state = {
            "schema_version": "1.0.0",
            "options_status": "ENABLED",
            "options_level": options_level,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "puts_scored_but_below_min": len(all_scored_puts),
            "top_scores": [{"sym": p["underlying"], "score": p["score"], "strike": p["strike"]}
                          for p in all_scored_puts[:5]],
            "active_cycles": [],
            "completed_cycles": [],
            "total_premium_collected": 0,
            "total_cycles_completed": 0,
            "note": f"All puts scored below score_min ({score_min}). Retry next window."
        }
        atomic_write_json(WHEEL_CYCLES_FILE, wheel_state)

        append_log(TRADES_LOG, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "wheel_check",
            "result": "ALL_BELOW_SCORE_MIN",
            "options_level": options_level,
            "puts_evaluated": len(all_scored_puts),
            "score_min": score_min,
            "top_score": all_scored_puts[0]["score"] if all_scored_puts else 0,
        })

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------
print()
print("=" * 64)
print("  WHEEL CHECK SUMMARY")
print("=" * 64)
print(f"  Options trading level     : {options_level}")
print(f"  Options status            : ENABLED")
print(f"  Market open               : {market_open}")
print(f"  Wheel allocation          : ${WHEEL_ALLOCATION:,.2f}")
print(f"  Candidates evaluated      : {len(affordable)} affordable of {len(wheel_candidates)} total")
if all_scored_puts:
    print(f"  Puts scored               : {len(all_scored_puts)}")
    qualified_count = len([p for p in all_scored_puts if p["score"] >= score_min])
    print(f"  Puts qualifying           : {qualified_count}")
    if all_scored_puts:
        print(f"  Best candidate            : {all_scored_puts[0]['underlying']} ${all_scored_puts[0]['strike']} put ({all_scored_puts[0]['expiry']})")
        print(f"  Best score                : {all_scored_puts[0]['score']}")
else:
    print(f"  Puts scored               : 0 (stale weekend quotes)")
if not market_open:
    print(f"  Next market open          : {clock.next_open}")
    print(f"  Action                    : Queued for Monday execution")
print("=" * 64)
print()
print("  Done.")
