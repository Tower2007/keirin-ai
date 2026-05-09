param(
    [Parameter(Mandatory = $true)]
    [string]$DriveDataDir,

    [switch]$NoCopy,
    [switch]$DeleteSource
)

# 既存 data/ を Google Drive 等の指定パスへコピーし、.env の DATA_DIR を更新する。
#
# 使い方:
#   .\scripts\migrate_data_to_drive.ps1 -DriveDataDir "G:\マイドライブ\keirin-ai\data"
#
# オプション:
#   -NoCopy        コピーをスキップ (.env だけ更新)
#   -DeleteSource  コピー成功後にプロジェクト内 data/ を削除 (節約用、慎重に)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceData = Join-Path $projectRoot "data"
$envPath = Join-Path $projectRoot ".env"
$envExamplePath = Join-Path $projectRoot ".env.example"

# 絶対パスへ正規化
if ([System.IO.Path]::IsPathRooted($DriveDataDir)) {
    $targetData = [System.IO.Path]::GetFullPath($DriveDataDir)
} else {
    $targetData = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $DriveDataDir))
}

# プロジェクト内に向けるのは禁止 (意味が無い + 暴走防止)
if ($targetData.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "DriveDataDir はプロジェクト外を指定してください: $targetData"
}

# ターゲットディレクトリ作成
New-Item -ItemType Directory -Force -Path $targetData | Out-Null

# data/ をコピー
if (-not $NoCopy -and (Test-Path $sourceData)) {
    Write-Host "data/ を $targetData へコピー中... (サイズによっては時間がかかります)"
    Copy-Item -Path (Join-Path $sourceData "*") -Destination $targetData -Recurse -Force
    Write-Host "コピー完了。"
}

# .env が無ければ .env.example から作成
if (-not (Test-Path $envPath)) {
    Copy-Item -LiteralPath $envExamplePath -Destination $envPath
    Write-Host ".env を .env.example から作成しました。"
}

# .env の DATA_DIR を更新 (既存行があれば置換、無ければ追記)
$lines = Get-Content -LiteralPath $envPath -ErrorAction Stop
$updated = $false
$newLines = foreach ($line in $lines) {
    if ($line -match "^\s*#?\s*DATA_DIR\s*=") {
        "DATA_DIR=$targetData"
        $updated = $true
    } else {
        $line
    }
}
if (-not $updated) {
    $newLines += "DATA_DIR=$targetData"
}
Set-Content -LiteralPath $envPath -Value $newLines -Encoding UTF8

Write-Host ""
Write-Host ".env DATA_DIR を更新しました:"
Write-Host "  $targetData"

# 元 data/ を削除 (-DeleteSource 指定時のみ)
if ($DeleteSource -and -not $NoCopy -and (Test-Path $sourceData)) {
    Write-Host ""
    Write-Host "プロジェクト内 data/ を削除します (元のパス: $sourceData)"
    Remove-Item -Path $sourceData -Recurse -Force
    Write-Host "削除完了。"
}

Write-Host ""
Write-Host "次のステップ:"
Write-Host "  python -c `"from src.config import DATA_DIR; print('DATA_DIR =', DATA_DIR)`""
Write-Host "  python backfill_odds.py 2021-01-01 2026-12-31"
