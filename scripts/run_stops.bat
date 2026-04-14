@echo off
cd /d "C:\Users\magli\Desktop\TradeEngine"
for /f "tokens=1,2 delims==" %%a in (.env) do set %%a=%%b
python scripts/trailing_stop_monitor.py --quiet >> logs/stops.log 2>&1
