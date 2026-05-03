@echo off
REM keirin-ai next-morning cron 5:00 - fetch yesterday's results/payouts
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
C:\Python313\python.exe ingest_yesterday.py >> logs\cron_results_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
