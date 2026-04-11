# Trailing Stop Strategy — Execution Instructions

## Overview
Buy a stock position and protect it with an automatically-adjusting trailing stop that locks in gains while limiting downside.

## When This Runs
- Monitor: Every 5 minutes during market hours (9:30 AM - 4:00 PM ET)
- New positions: On user command or when self-improvement suggests entry

## Execution Steps

### Opening a Position
1. Read config/strategy_params.json for current parameters
2. Read config/symbol_lists.json for watchlist
3. Check state/positions.json — verify cash available minus 20% reserve
4. Place market buy order via Alpaca MCP: place_stock_order(symbol, qty, side="buy", type="market")
5. Record the fill price as entry_price
6. Calculate initial floor: floor_price = entry_price * (1 - loss_pct / 100)
7. Place trailing stop order via Alpaca: place_stock_order(symbol, qty, side="sell", type="trailing_stop", trail_percent=trail_pct)
8. Write to state/trailing_stops.json (atomic write via scripts/atomic_write.py)
9. Append to logs/trades.jsonl
10. Git commit: git add state/ logs/ && git commit -m "[TRAILING_STOP] Opened {symbol} at ${entry_price}, floor at ${floor_price}"

### Monitoring Loop (Every 5 Minutes)
1. For each active stop in state/trailing_stops.json:
   a. Get current price: get_stock_latest_quote(symbol)
   b. If current_price > highest_price: update highest_price
   c. Calculate new floor: new_floor = highest_price * (1 - trail_pct / 100)
   d. If new_floor > floor_price: update floor_price (floor only goes UP, never down)
   e. Verify Alpaca's trailing stop order is still active: get_orders()
   f. If trailing stop was triggered (position closed): mark as STOPPED_OUT, log, commit
2. Write updated state (atomic write)
3. If any position was stopped out, log the P&L

### Ladder Buys (Averaging Down)
If enabled and current_price has dropped by ladder level %:
1. Check cash available (respect 20% reserve)
2. Buy additional shares at current market price
3. Recalculate average cost basis
4. Set new trailing stop on entire position
5. Log and commit

### Exit Conditions
- Trailing stop triggers automatically (handled by Alpaca)
- User manually requests exit
- Self-improvement engine suggests closing position

## Edge Cases
- Market closed: Skip monitoring, log "market closed"
- Stock halted: Do NOT cancel stop orders, wait for resume
- Partial fill: Track actual filled quantity, adjust stop accordingly
- API timeout: Retry 3x with exponential backoff, then alert user
- Stock split: Recalculate entry_price, floor_price, and shares after split

## Parameters (from strategy_params.json)
| Parameter | Default | Bounds | Description |
|-----------|---------|--------|-------------|
| loss_pct | 5.0 | 3.0-15.0 | Initial stop loss percentage below entry |
| trail_pct | 5.0 | 2.0-10.0 | Trailing distance below highest price |
| ladder_buy_enabled | true | — | Enable averaging down on dips |
