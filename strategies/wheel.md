# Options Wheel Strategy — Execution Instructions

## Overview
Sell cash-secured puts to collect premium. When assigned, sell covered calls to collect more premium. If called away, keep the profit. Repeat indefinitely on the same stock (the "wheel").

## When This Runs
- Three execution windows: 10:00 AM, 1:00 PM, 3:30 PM ET (Mon-Fri, market hours)
- Assignment detection: 9:35 AM ET (check for overnight assignments)
- Exit checks: Continuous during business hours

## Execution Steps

### Pre-Execution Checks (Before Each Window)
1. Call Alpaca: get_account_info() — verify options approval level
   - If level < 2: Log warning "Options not enabled", skip execution, exit gracefully
2. Read config/strategy_params.json for wheel parameters
3. Read config/symbol_lists.json for wheel_candidates
4. Check state/wheel_cycles.json for active cycles and their status
5. Verify cash available >= 20% reserve (if not, halt until cash freed)

### Finding the Best Put to Sell (Execution Logic)
1. For each symbol in wheel_candidates:
   a. Skip if already have active put or call on this symbol
   b. Get current stock price: get_stock_latest_quote(symbol)
   c. Find options chain for 14-45 DTE (call get_options_chain(symbol))
   d. Filter puts with:
      - DTE between dte_min (14) and dte_max (45)
      - Delta between put_delta_bounds (0.20-0.35), closer to 0.25
      - Bid-ask spread < $0.10
      - Open interest >= open_interest_min (100)
   e. For each qualifying put, calculate score:
      - score = (1 - abs(delta - 0.25)) * (250 / (dte + 5)) * (bid / strike)
      - (Rewards: delta close to 0.25, low DTE, high premium per dollar)
   f. Calculate annualized yield: (premium * 365) / (strike * 100)
      - Require yield between yield_min (0.5%) and yield_max (5%)
   g. Rank all puts by score, select highest scorer above score_min (0.5)

2. If no valid puts found: Log "No puts meeting criteria", exit gracefully

### Selling a Put
1. Place order: place_options_order(symbol, qty=1, type="put", side="sell", strike=chosen_strike, expiry=chosen_expiry, order_type="limit", price=bid_price)
2. Wait for fill (poll up to 5 minutes)
3. Record to state/wheel_cycles.json (atomic write)
4. Append to logs/trades.jsonl
5. Git commit: git add state/ logs/ && git commit -m "[WHEEL] Sold {symbol} ${strike} put for ${premium}"

### Assignment Detection (9:35 AM ET)
1. Read state/wheel_cycles.json for all open puts
2. Call Alpaca: get_positions() to see if any new stock positions appeared overnight
3. For each newly assigned stock:
   a. Find corresponding put in wheel_cycles.json and mark as ASSIGNED
   b. Calculate cost basis (strike * 100) - premium_collected
   c. Update cycle record with assignment_timestamp
   d. Move to SELLING_CALL phase
4. Write state/wheel_cycles.json (atomic write)

### Selling a Call (After Assignment)
1. For each assigned position (now own stock):
   a. Get current stock price
   b. Find options chain for calls with:
      - DTE between dte_min (14) and dte_max (45)
      - Delta between call_delta_bounds (0.20-0.35), closer to 0.25
      - Bid > 0 (must have liquid bid)
      - Open interest >= open_interest_min
   c. Score calls same as puts (reward delta ~0.25, low DTE, high premium)
   d. Select highest-scoring call above score_min
   e. Calculate profit on this leg: (call_premium + put_premium) / entry_cost (%)

2. Place order: place_options_order(symbol, qty=1, type="call", side="sell", strike=chosen_strike, expiry=chosen_expiry, order_type="limit", price=bid_price)
3. Record to wheel_cycles.json
4. Git commit

### Early Close Logic (During Each Window)
If any open call/put has unrealized profit >= early_close_profit_pct (50%):
1. Close the option at market (sell if long, buy if short)
2. Record realized profit
3. Update cycle to EARLY_CLOSED or EXPIRED status
4. Calculate total cycle P&L and store in completed_cycles
5. Git commit

### Expiration Handling (Auto)
On each monitoring pass, check all open options for expiration:
- If put/call expired worthless (most common): Collect remaining premium, mark EXPIRED
- If put/call expired in-the-money:
  - Put expired ITM: Stock will be assigned to your account (handle in next morning's assignment detection)
  - Call expired ITM: Stock will be called away (exit position, record profit)
- Update wheel_cycles.json, log results, git commit

## Cycle Completion
When a full wheel cycle closes (either called away or both legs expired worthless):
1. Calculate cycle metrics:
   - total_premium = put_premium + call_premium
   - cycle_pnl = (total_premium - cost_of_capital)
   - days_to_completion = assignment_date to call_expiry
   - annualized_return = cycle_pnl / days_to_completion * 365
2. Move cycle to completed_cycles array
3. Update totals: total_premium_collected, total_cycles_completed
4. Log full cycle summary
5. Git commit

## Edge Cases
- No assignments: Puts expire worthless, keep premium, restart wheel on same or new symbol
- Early assignment on put: Stock arrives before expiry, move immediately to selling call
- Early assignment on call: Stock called away before expiry, cycle completes early, record profit
- Stock split during cycle: Adjust strike prices and quantities accordingly
- Options halted: Skip this symbol for this window, retry next window
- Insufficient buying power: Don't enter new cycles until cash freed
- Delta creep: If option delta drifts far from 0.25, close early and reselect

## Parameters (from strategy_params.json)
| Parameter | Default | Bounds | Description |
|-----------|---------|--------|-------------|
| put_delta | 0.25 | 0.20-0.35 | Preferred delta for short puts |
| call_delta | 0.25 | 0.20-0.35 | Preferred delta for short calls |
| dte_min | 14 | — | Minimum days to expiration |
| dte_max | 45 | — | Maximum days to expiration |
| dte_target | 30 | — | Optimal target DTE |
| open_interest_min | 100 | — | Minimum open interest |
| yield_min | 0.005 | — | Minimum annualized yield (0.5%) |
| yield_max | 0.05 | — | Maximum annualized yield (5%) |
| early_close_profit_pct | 50 | — | Close if 50% profit reached |
| max_contracts_per_symbol | 1 | — | Never run more than 1 wheel per symbol |
| score_min | 0.5 | — | Minimum score to execute |
| scoring_formula | See above | — | Option scoring algorithm |

## Alpaca MCP Calls Required
- get_account_info() — options approval level, buying power
- get_stock_latest_quote(symbol) — current price
- get_options_chain(symbol) — all available options
- place_options_order(...) — open/close options positions
- get_positions() — current holdings, detect assignments
- get_orders() — pending orders
