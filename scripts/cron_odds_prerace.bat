@echo off
REM keirin-ai pre-start odds snapshot cron (every 1 minute)
REM 発走 5 分前のレースを検出して odds を取得 → race_odds_prerace.csv に追記
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
C:\Python313\python.exe ingest_odds_prerace.py >> logs\cron_prerace_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
