@echo off
cd /d "C:\Users\magli\Desktop\TradeEngine"
for /f "tokens=1,2 delims==" %%a in (.env) do set %%a=%%b
python scripts/crypto_strategy.py --quiet >> logs/crypto.log 2>&1
