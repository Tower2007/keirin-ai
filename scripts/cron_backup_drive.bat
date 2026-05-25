@echo off
REM keirin-ai データを ZIP 化して Google Drive に転送 (週次)
REM D: ドライブが壊れた時のセーフネット。最新 3 世代のみ保持。
chcp 65001 > nul
cd /d C:\Users\no28a\Claude-project\keirin-ai
set PYTHONIOENCODING=utf-8
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set DATESTR=%%i
C:\Python313\python.exe scripts\backup_to_drive.py --keep 3 >> logs\cron_backup_%DATESTR%.log 2>&1
exit /b %ERRORLEVEL%
