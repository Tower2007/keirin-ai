# keirin-ai

競輪 (keirin.jp) のデータ蓄積・可視化・ML 検証アプリ。
姉妹プロジェクト: auto-racing-ai (オートレース)、boat-racing-ai (競艇)。
運用ガイドの正本は `CLAUDE.md` (Claude Code 用)、`AGENTS.md` (Codex 用)、`GEMINI.md` (Gemini 用)。

## 目的

- keirin.jp (公式) の HTML 内埋め込み JSON から出走表・ライン・選手成績・結果・払戻を CSV に蓄積する
- netkeirin の発走直前オッズ (5/3/2/1/0.5 分前) をスナップショット記録し、約定実現性 (執行ウェッジ) を検証する
- LightGBM (`production_no_odds` 系) で予測モデルを週次再学習し、採用品質ゲート (ROI 劣化拒否権つき) で運用する
- 購入は行わない (shadow 評価のみ)。購入開始は凍結ゲート A/B 通過後の別判断

## データ置き場

`.env` の `DATA_DIR` で指定する。**本番は `D:\keirin-ai-data`** (ローカル SSD/HDD)。
省略時はプロジェクト直下 `data/` (`.gitignore` 済)。

- 大容量ファイル (`race_odds.csv` 約 2.5 GB) を Google Drive Stream 上で active write すると壊れるため、Drive は完成 ZIP の保管庫としてのみ使う (`scripts/backup_to_drive.py`)
- `race_odds.csv` (発走後オッズ) は 2026-05-12 以降更新停止、`race_odds_prerace.csv` (発走前スナップショット) が本線
- 主要 CSV の構成とキーは `CLAUDE.md` の「CSV ファイル構成」を参照

## 自動実行タスク (Windows タスクスケジューラ)

| タスク名 | タイミング | 実行内容 |
|---|---|---|
| `keirin-ai-results` | 毎日 05:00 | `scripts/cron_results.bat` → `ingest_yesterday.py` (前日の結果・払戻) |
| `keirin-ai-lines` | 毎日 07:00 | `scripts/cron_lines.bat` → `ingest_today_lines.py` (当日全場の出走表・ライン・成績) |
| `keirin-ai-shadow-predict` | 毎日 07:30 | `daily_shadow_predict.py` (当日レースの shadow 予測を時点固定で保存) |
| `keirin-ai-prerace-daemon` | 毎日 08:00 | `ingest_odds_prerace_daemon.py` (pythonw、発走前オッズ snapshot) |
| `keirin-ai-backup-drive` | 月曜 03:00 | `scripts/cron_backup_drive.bat` → `scripts/backup_to_drive.py --keep 3` |
| `keirin-ai-weekly-retrain` | 月曜 06:00 | `scripts/cron_weekly_retrain.bat` → `weekly_retrain.py` (週次再学習 + 採用ゲート) |
| `keirin-ai-weekly-report` | 月曜 07:30 | `scripts/cron_weekly_report.bat` → `weekly_drift_report.py --days 7 --mail` (週次メール) |

タスクのログは `logs/cron_*_YYYY-MM-DD.log`。タスク名・時刻の正本はタスクスケジューラ側
(`Get-ScheduledTask | Where-Object TaskName -like 'keirin-ai-*'`)。

## テスト

```powershell
python -m pytest tests -q -p no:cacheprovider
```

`weekly_drift_report.py` / `daily_shadow_predict.py` / `roi_gate.py` などの表示・判定ロジックを
変更したら必ず実行する (現在 30 件)。

## セットアップ

1. Python 3.13、`pip install -r requirements.txt`
2. `.env` に `DATA_DIR` / `KEIRIN_REQUEST_DELAY_SEC` / Gmail 通知設定 (`NOTIFY_TO` 等) を置く (git 管理外)
3. `python smoke_test.py` で 1 日分の取得を確認
