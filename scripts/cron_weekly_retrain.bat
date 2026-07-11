@echo off
REM keirin-ai weekly ML retrain + adoption quality gate (weekly cron)
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
REM 派生テーブルを先に更新 (監査 P2: race_quality の stale 化で他スクリプトの再現性が割れる)
C:\Python313\python.exe build_race_quality.py >> logs\cron_weekly_retrain_%DATESTR%.log 2>&1
C:\Python313\python.exe build_race_completion.py --days 30 >> logs\cron_weekly_retrain_%DATESTR%.log 2>&1
C:\Python313\python.exe weekly_retrain.py >> logs\cron_weekly_retrain_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
