# keirin-ai data フォルダを ZIP 化して Google Drive に置く (Python 実装の薄ラッパ)。
#
# PowerShell の Compress-Archive は 2 GB を超えると失敗するため、
# 実装は scripts/backup_to_drive.py に移譲。
#
# 使い方:
#   .\scripts\backup_to_drive.ps1
#   .\scripts\backup_to_drive.ps1 -NoUpload

param(
    [string]$DataDir = "D:\keirin-ai-data",
    [string]$DriveDir = "G:\マイドライブ\keirin-ai-backups",
    [switch]$NoUpload
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $projectRoot "scripts\backup_to_drive.py"

$args = @("--data-dir", $DataDir, "--drive-dir", $DriveDir)
if ($NoUpload) { $args += "--no-upload" }

python $script @args
