# Politician Copy Trading Strategy — Execution Instructions

## Overview
Monitor U.S. Congress members' stock trades via Capitol Trades and replicate their positions. Trades are public within 45 days of execution. Copy the best performers in a bipartisan manner.

## When This Runs
- Morning Scan: 9:00 AM ET daily (Mon-Fri, market hours)
- Afternoon Review: 3:30 PM ET daily (position rebalance and assignment checks)
- Politician Selection: Weekly (auto-select top performer if enabled)

## Execution Steps

### Politician Selection (First Run or Weekly)
1. If config/politician_watchlist.json has tracked_politicians list and is not empty: use them
2. Otherwise, auto-select:
   a. Call Capitol Trades MCP: get_politician_stats(period_days=90)
   b. Filter for politicians with >= min_trades_last_90_days
   c. Score by: trade_count * win_rate (bipartisan bonus: +0.1 if both buy/sell)
   d. Select top 1-3 politicians
   e. Write to config/politician_watchlist.json (atomic write)
   f. Git commit: git add config/ && git commit -m "[POLITICIAN_COPY] Selected {name} (win_rate={wr}, trades={ct})"

### Morning Scan (9:00 AM ET)
1. Read config/politician_watchlist.json for tracked_politicians
2. For each politician:
   a. Call Capitol Trades MCP: get_politician_recent_trades(name, days=45)
   b. Filter for FILINGS within last 45 days (staleness_threshold_days)
   c. Filter for size $1,001 - $15,000 (min_trade_size from params)
   d. For each FILING:
      - If it's a BUY and we don't already own it:
        - Calculate shares: (available_cash * 5%) / current_price (max 5% per position)
        - Place market order: place_stock_order(symbol, qty, side="buy", type="market")
        - Record to state/copy_trades.json with source_politician, filing_date, entry_price
      - If it's a SELL and we own it:
        - Close entire position at market
        - Record exit_price, P&L to state/copy_trades.json
3. Write state/copy_trades.json (atomic write)
4. Log all trades to logs/trades.jsonl
5. Git commit any changes

### Afternoon Review (3:30 PM ET)
1. Read state/copy_trades.json
2. For each replicated position:
   a. Check current price vs entry_price
   b. Update unrealized P&L
   c. If position is older than replication_delay_max_days (2 days) and politician has closed it (via recent trades):
      - Close the position
      - Record realized P&L
3. Update performance metrics:
   - total_trades = number of buy+sell pairs completed
   - winning_trades = positions with positive P&L
   - losing_trades = positions with negative P&L
   - total_pnl = sum of all realized P&L
   - win_rate = winning_trades / total_trades
4. Write state/copy_trades.json (atomic write)
5. Git commit

### Exit Conditions
- Politician closes position (detected in daily scan)
- Position age > 45 days (stale data, exit at market)
- Position loss > 10% (risk management, exit at market)
- User manually removes politician from watchlist

## Capitol Trades MCP Calls Required
- get_politician_stats(period_days=90) — returns leaderboard with trade counts, win rates
- get_politician_recent_trades(name, days=45) — returns recent trades with dates, symbols, sides, sizes
- get_buy_momentum_assets() — optional: trending symbols among Congress

## Edge Cases
- Politician leaves Congress: Stop tracking, close positions
- Position size > available cash: Scale down to max 5% per position
- Stock not tradeable on Alpaca (delisted, etc.): Skip and log warning
- Same stock traded by multiple politicians: Combine positions, cap at 5% per symbol
- API timeout: Retry 3x with exponential backoff, skip scan if all fail

## Parameters (from strategy_params.json)
| Parameter | Default | Description |
|-----------|---------|-------------|
| enabled | true | Enable politician copy trading |
| max_position_pct | 5.0 | Max % of account per position |
| staleness_threshold_days | 45 | Max age of trade filing to copy |
| min_trade_size | "$1,001 - $15,000" | Only copy trades in this range |
| replication_delay_max_days | 2 | Max days between filing and replication |
| bipartisan_signal_bonus | true | Bonus score if politician trades both sides |
