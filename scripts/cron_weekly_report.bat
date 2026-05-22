@echo off
REM keirin-ai weekly drift report (毎週月曜 7:30 想定)
REM 過去 7 日分の snapshot 蓄積状況 + drift をメール送信
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
C:\Python313\python.exe weekly_drift_report.py --days 7 --mail >> logs\cron_weekly_report_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
