@echo off
REM keirin-ai morning cron 7:00 - fetch today's lines/entries/stats
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
C:\Python313\python.exe ingest_today_lines.py >> logs\cron_lines_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
