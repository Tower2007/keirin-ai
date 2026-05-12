# keirin-ai data フォルダを ZIP 化して Google Drive に置く。
#
# Drive Stream の active write 事故を避けるため、データは D: に置き、
# このスクリプトで完成 ZIP だけを Drive に上げる方針。
#
# 使い方:
#   .\scripts\backup_to_drive.ps1
#   .\scripts\backup_to_drive.ps1 -DataDir "D:\keirin-ai-data" -DriveDir "G:\マイドライブ\keirin-ai-backups"

param(
    [string]$DataDir = "D:\keirin-ai-data",
    [string]$DriveDir = "G:\マイドライブ\keirin-ai-backups",
    [switch]$NoUpload   # ZIP は作るが Drive にコピーしない (検証用)
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $DataDir)) {
    throw "DataDir not found: $DataDir"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$zipName = "keirin-ai-data_$timestamp.zip"
$tempZip = Join-Path $env:TEMP $zipName

Write-Host "1/3 ZIP 作成中: $tempZip"
Write-Host "   source: $DataDir"

# .lock ファイルは除外 (壊れた状態でロックされてた可能性、不要)
$items = Get-ChildItem $DataDir -File | Where-Object { $_.Extension -ne ".lock" }
$itemPaths = $items | ForEach-Object { $_.FullName }
Compress-Archive -Path $itemPaths -DestinationPath $tempZip -CompressionLevel Optimal -Force

$zipSize = (Get-Item $tempZip).Length / 1MB
Write-Host "   完成: $([math]::Round($zipSize, 1)) MB"

# Drive へ転送
if ($NoUpload) {
    Write-Host ""
    Write-Host "2/3 -NoUpload 指定のため Drive へのコピーはスキップ"
    Write-Host "ZIP は $tempZip にあります"
    return
}

Write-Host ""
Write-Host "2/3 Drive へコピー: $DriveDir"

if (-not (Test-Path $DriveDir)) {
    New-Item -ItemType Directory -Path $DriveDir -Force | Out-Null
}

$destZip = Join-Path $DriveDir $zipName
Copy-Item $tempZip $destZip -Force

Write-Host "   完了: $destZip"

# 古いバックアップ整理 (直近 3 つだけ残す)
Write-Host ""
Write-Host "3/3 古いバックアップ整理 (最新 3 つだけ残す)"
$old = Get-ChildItem $DriveDir -Filter "keirin-ai-data_*.zip" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 3
foreach ($f in $old) {
    Remove-Item $f.FullName -Force
    Write-Host "   削除: $($f.Name)"
}

# 一時 ZIP 削除
Remove-Item $tempZip -Force

Write-Host ""
Write-Host "=== バックアップ完了 ==="
Write-Host "PC2 での復元手順:"
Write-Host "  1. Google Drive web から $zipName をダウンロード"
Write-Host "     (Drive Stream で扱うと事故るのでブラウザ DL 推奨)"
Write-Host "  2. D:\keirin-ai-data\ に展開"
Write-Host "  3. .env で DATA_DIR=D:\keirin-ai-data を設定"
