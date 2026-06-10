@echo off
REM keirin-ai weekly ML retrain + adoption quality gate (weekly cron)
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
C:\Python313\python.exe weekly_retrain.py >> logs\cron_weekly_retrain_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
