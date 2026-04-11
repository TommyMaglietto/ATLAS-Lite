# ATLAS Lite — Project Instructions

## What This Is
ATLAS Lite is a self-improving AI trading system built on Claude + Alpaca MCP + Capitol Trades MCP. It runs three strategies (trailing stops, politician copy-trading, options wheel) gated by a market regime filter, with a Karpathy-style autoresearch self-improvement loop that optimizes strategy parameters against rolling Sharpe ratio.

## Architecture
- **Orchestrator**: Claude (via scheduled tasks and conversational commands)
- **Regime Filter**: Runs BEFORE every strategy — classifies market as RISK_ON, CAUTIOUS, or RISK_OFF using cross-asset signals
- **Trading**: Alpaca MCP Server (stocks, options, all order types)
- **Political Data**: Capitol Trades MCP Server (no API key, web scraping)
- **State**: File-based JSON in `state/` directory with atomic writes (write-to-temp-then-rename)
- **Versioning**: Git — every parameter change is committed; failed experiments are reverted
- **Scheduling**: Claude desktop persistent tasks (trailing stops every 5 min, wheel 3x daily, politicians daily, self-improvement weekly)

## Directory Structure
```
TradeEngine/
├── CLAUDE.md
├── config/
│   ├── strategy_params.json
│   ├── symbol_lists.json
│   └── politician_watchlist.json
├── state/
│   ├── positions.json
│   ├── wheel_cycles.json
│   ├── trailing_stops.json
│   ├── copy_trades.json
│   ├── experiments.json
│   └── regime.json
├── logs/
│   ├── trades.jsonl
│   ├── experiments.jsonl
│   └── daily_summaries/
├── strategies/
│   ├── regime_filter.md
│   ├── trailing_stop.md
│   ├── politician_copy.md
│   └── wheel.md
├── self-improvement/
│   ├── program.md
│   └── metrics_history.json
├── scripts/
│   ├── reconcile.py
│   ├── metrics.py
│   └── atomic_write.py
└── docs/
    └── plans/
```

## Critical Rules
1. ALWAYS paper trading — never set ALPACA_PAPER_TRADE=false unless explicitly told
2. Atomic writes — all state file writes use write-to-temp-then-rename pattern
3. Log everything — every trade, every parameter change, every experiment in JSONL
4. Git every change — commit on improvements, revert on failures
5. Reconcile positions — every 15 minutes, verify local state matches Alpaca
6. Parameter bounds — self-improvement can NEVER push parameters outside defined bounds
7. Cash reserve — always keep 20% of account liquid for options assignment
8. Delta range — options delta always between 0.20 and 0.35

## MCP Servers Required
- alpaca-mcp-server (https://github.com/alpacahq/alpaca-mcp-server)
- mcp-capitol-trades (https://github.com/anguslin/mcp-capitol-trades)

## Key Parameters (in strategy_params.json)
- Trailing stop: loss_pct (3-15%), trail_pct (2-10%)
- Wheel: put_delta (0.20-0.35), call_delta (0.20-0.35), dte_min (14), dte_max (45)
- Copy trading: max_position_pct (5%), staleness_threshold_days (45)
- Self-improvement: eval_period_days (5), min_trades_before_commit (30), min_sharpe_improvement (0.1)
