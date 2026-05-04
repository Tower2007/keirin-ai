# keirin-ai 引き継ぎ資料

**初回作成日**: 2026-04-26  
**最終更新日**: 2026-05-04（Phase 3 オッズ取得実装完了）

---

## 自宅PC 向けタスク (2026-05-04): netkeirin オッズ 5年バックフィル

### 手順

```cmd
cd C:\Users\user\Claude-Project\keirin-ai
git pull origin main

REM (任意) 既存 race_meta.csv に is_canceled 列を追加
REM ※ 自宅PC 側で ingest_day.py を再開する場合のみ必要。
REM ※ backfill_odds.py だけ走らせる場合は不要。
python -c "import pandas as pd; df = pd.read_csv('data/race_meta.csv'); df['is_canceled'] = df.get('is_canceled', False) if 'is_canceled' in df.columns else False; df.to_csv('data/race_meta.csv', index=False); print('OK')"

REM オッズ 5年バックフィル開始 (バックグラウンド推奨)
python backfill_odds.py 2021-01-01 2026-12-31 > backfill_odds.log 2>&1
```

### ETA

- 対象: status=normal の **13,285 venue-day** (race_quality.csv から自動抽出)
- 1 venue-day あたり 12レース × 0.8秒 + parse ≒ 13秒
- **合計約 48 時間**（短縮されれば 36 時間程度）
- 途中中断 OK: `data/backfill_odds_done.txt` で再開可能

### 進捗確認

```cmd
wc -l data\backfill_odds_done.txt
type data\backfill_odds_stats.json
tail -50 data\backfill_odds.log
```

### 完了後

成果物 `data/race_odds.csv` を bundle で受領:
```cmd
git bundle create %USERPROFILE%\OneDrive\keirin-ai-data-YYYYMMDD.bundle data-snapshot
```

### 注意点

- ノートPCなら **スリープ無効化必須** (電源オプション → 「PCをスリープしない」)
- ネットワーク切断時は urllib3 Retry が自動再試行 (最大5回)。それでも fail なら `data/backfill_odds.log` 確認
- 期待行数: ~5,000 万行 (×7券種、1レース平均 600〜800行)

---

## 2026-05-04 後半セッション (このPC帰着 → クリーニング + ML + Phase 3)

このセッションでやったこと:

1. ✅ **bundle 経由でバックフィルデータ取り込み** (5年分、350MB)
2. ✅ **`no_entries` 42 venue-day の正体判明** — 全て公式キャンセル (中止/順延/打切)。
   データ取得不可能と確認した上で `is_canceled` 列追加によるクリーニングで対応 (commit `232b16b`)
3. ✅ **race_results.csv の placeholder 17 行削除**
4. ✅ **コード対応**: `parse_program_meta` で `flgRaceCancel` 検出、
   `ingest_day.py` がキャンセル日を冪等に扱える
5. ✅ **ML ベースライン完成** (commit `4205759`):
   - `ml_baseline.py`: 4 ターゲット (P(win)/P(top2)/P(top3)/rank) + 5 券種バックテスト
   - `ml_ev_backtest.py`: 校正→評価による疑似 EV 検証
6. ✅ **race_quality.csv 再生成**: status `canceled` ステータス追加、no_entries 0件に
7. ✅ **Phase 3: netkeirin オッズ取得実装** (commit `2a3cbc5`):
   - `src/netkeirin_client.py`: AplRaceOdds API クライアント (POST `/api/race/`)
   - `src/netkeirin_parser.py`: list_3..list_9 → 7券種 (WH2/WT2/SH2/ST2/W/RH3/RT3) 変換
   - `ingest_odds.py` + `backfill_odds.py`
   - 検証: 2026-04-26 名古屋 R8 で実払戻と完全一致 (差 0.0%)
   - 2021-04〜2026-04 全期間で OK 確認済 → 5年バックフィル可能

### ML ベースライン主要数値

**モデル性能 (test 2025-01〜2026-04, 35,843レース)**:
- P(win) AUC = 0.812 (公営競技ML として上位水準)
- P(top2) AUC = 0.780
- P(top3) AUC = 0.750
- 着順 MAE = 1.49

**バックテスト (1ticket/race, 全レース購入)**:
| 券種 | 的中率 | ROI |
|---|---|---|
| 二車複 | 26.7% | -28.6% |
| 二車単 | 17.2% | -28.7% |
| 三連複 | 16.9% | -31.4% |
| 三連単 | 4.9% | -34.7% |
| ワイド | 35.9% | -24.6% |

**confidence 上位 5% に絞った結果**:
- ワイド -17.3% (最良)、二車単 -19.7%、三連複 -23.4%
- 控除率 -25% を超える signal あり、ただし黒字化には届かず

**疑似 EV バックテスト (校正→評価)**:
- 校正期で profitable と判定されたビン → 評価期で全部負け
- → **モデル単独では期待値プラス困難。Phase 3 (事前オッズ取得) が決定打**

### 次回セッションでやりたいこと (2026-05-04 後半時点)

1. ✅ **Phase 3 オッズ取得コード**: 実装済 (commit `2a3cbc5`) → 自宅PCでバックフィル実行 (上の手順参照)
2. **オッズ取得後の収益検証**: race_odds.csv が揃ったら、本物の EV ベース戦略で ROI を再検証
3. **特徴量強化**: 過去N走ローリング、対戦相手特性 (予測精度をブースト)
4. **戦略複合**: 各券種の最良 confidence ビンを使い分けるミックス戦略 (二車単=中確信、三連複=低確信)
5. **Phase 5**: Supabase 移行 (CSV 400MB 超え予定)
6. **特徴量モデルの保存** (毎回 12分の再学習を避ける)

### 出力データ・成果物

| ファイル | 内容 |
|---|---|
| data/ml_picks.csv | 35,843 レース x 5 券種の picks + confidence (再分析用) |
| data/ml_baseline_v2_result.csv | 全レース購入時のROI |
| data/ml_threshold_result.csv | 閾値別 ROI |
| data/ml_ev_backtest_result.csv | EV 選別結果 |
| data/no_entries_diagnosis.json | 42件キャンセル日の診断 (再利用可) |

---

## 2026-05-04 更新版 TL;DR（別PCで作業継続する場合）

1. **コード**: 別PC で `git pull origin main`（GitHub から）
2. **データ**: 最新 bundle (`keirin-ai-data-YYYYMMDD.bundle`) を OneDrive/USB で持参
   ```cmd
   git fetch C:\path\to\keirin-ai-data-YYYYMMDD.bundle data-snapshot:data-snapshot
   git checkout data-snapshot -- data/
   ```
3. 別PC で `pip install -r requirements.txt`（pandas 追加済み）
4. `python build_race_quality.py` で品質サマリ再生成（任意）
5. 翌朝以降の継続: `ingest_today_lines.py` (朝7時) + `ingest_day.py` ループ (翌朝5時) を Windows タスクスケジューラ登録

**初回セットアップ（別PCにレポジトリも無い）の場合**: 下の「2. 別PCのセットアップ手順」参照。

---

## 2026-05-04 現状サマリ

### バックフィル完了
- 範囲: 2021-04-26 〜 2026-04-25（5年分）
- 経過時間: 7時間36分（約45時間想定 → 短縮 — Cloudflare 対策不要だった）
- errors=0, ingested=1,678 venue-day
- 累積データ: 13,430 venue-day / 137,176 race / 966,199 entries 行

### 新規追加
- `build_race_quality.py` + `data/race_quality.csv`: venue-day 単位の完了率/欠損サマリ
  - status 内訳: normal 13,285 / partial_canceled 92 / no_entries 42 / payout_missing 7 / all_canceled 4

### 現状の懸念点（次セッションで対応候補）

#### 🟡 懸念 1: `no_entries` 42 venue-day の anomaly
- `race_meta.csv` には記録があるが `race_entries.csv` に行が無い venue-day が 42件
- うち 17件は JSJ012 の placeholder 行のみ `race_results.csv` に残存（tyaku/kimarite 全 NaN）
- 原因仮説: ingest_day 実行時に PJ0305 取得が失敗したまま meta だけ書き込まれた
- 対応案: `ingest_day.py` のスキップ条件を「meta + entries 両方あれば skip」に変更し、該当 venue-day を再 ingest
- 影響: ML 学習データの欠損 0.31%。`status='normal'` でフィルタすれば実害無し

#### 🟡 懸念 2: `partial_canceled` 92 venue-day（実世界要因）
- 雪・台風・落車等で当日途中打切りの venue-day
- 集中日: 2023-01-24 (10R欠), 2025-02-07 (10R欠), 2025-01-21 (9R欠), 2021-10-01 (8R欠)
- これは仕様通りで対応不要だが、ML 学習時は天候特徴量として使うか除外検討

#### 🟡 懸念 3: ライン情報 (race_lines.csv) は 4/26 分のみ
- PJ0305 nInfo はレース完了後に空配列のため過去取得不可（仕様）
- 対応: 別PC で `ingest_today_lines.py` を朝7時 cron で確実に動かす
- 現状 race_lines.csv は ~330行（4/26 の1日分のみ）

#### 🟢 懸念無し（確認済）
- 主キー重複: 全 CSV で 0
- entries ↔ stats: 完全一致 (137,176 races / 966,199 rows)
- meta ↔ entries 整合性: 13,430 venue-day で完全一致

### 次回セッションでやりたいこと（優先度順）

1. **`no_entries` 42 venue-day の再 ingest**（`ingest_day.py` のスキップ条件修正後に再実行）
2. **ML 試作**: `race_quality.csv` で `status='normal'` フィルタ + LightGBM ベースライン
3. ✅ **日次 cron の安定運用**: 2026-05-04 セットアップ完了（下記参照）
4. **Phase 3**: 第三者サイトからの事前オッズ取得検討
5. **Phase 5**: Supabase 移行（CSV が 350MB 超え、検索が重くなってきた）

### 日次 cron セットアップ済 (2026-05-04 / このPC)

| タスク名 | 実行時刻 | スクリプト | 用途 |
|---|---|---|---|
| keirin-ai-lines   | 07:00 | scripts/cron_lines.bat   | 当日の全開催場の lines/entries/stats |
| keirin-ai-results | 05:00 | scripts/cron_results.bat | 昨日の全43場の results/payouts 回収 |

ログ出力先: `logs/cron_{lines,results}_YYYY-MM-DD.log`

⚠️ **注意点**:
- Logon Mode = "Interactive only" → ユーザーがログオンしている時のみ実行される。
  - 出張等で長期 PC を離れる場合は、自動ログオン or sleep 無効化 を別途検討
- ノートPCで蓋を閉じると suspend → タスクがスキップされる。AC 給電中の動作モードを「スリープしない」に設定推奨
- 失敗通知は未設定（`.env.example` に Gmail SMTP 枠だけ存在）。週次で `logs/` を目視確認するか、後日通知を実装

確認コマンド:
```cmd
schtasks /query /tn "keirin-ai-*"
schtasks /run /tn "keirin-ai-lines"      # 即時実行（手動テスト）
schtasks /delete /tn "keirin-ai-lines" /f # 削除
```

---

## 旧 TL;DR (2026-04-26 版、初回セットアップ用)

1. このPC で `git push`、別PC で `git clone` or `git pull`
2. 別PC で `.env` 作成、`pip install -r requirements.txt`
3. 別PC で `python backfill.py 2021-04-26 2026-04-25` 実行（**約 45 時間**）
4. 別PC で日次タスク 2 本を Windows タスクスケジューラ登録
5. データを定期的に作業用PC へコピー（rsync / OneDrive / USB 等）

---

## 1. プロジェクト現状

### 完了済（コミット済）
- ✅ Phase 1: 開催メタ・出走表・ライン → CSV
- ✅ Phase 2: 選手成績・結果・払戻 → CSV
- ✅ ingest_day.py の差分取り込み対応（朝lines / 夕results を別実行可）
- ✅ ingest_today_lines.py（cron 用）
- ✅ backfill.py（過去一括取得）
- ✅ エラー隠蔽バグ修正
- ✅ kojin_state / tyaku_note カラム追加（失格理由）

### このPCで蓄積済データ（1か月分）
| ファイル | 行数 |
|---|---|
| race_meta.csv | 251 |
| race_entries.csv | 17,473 |
| race_stats.csv | 17,473 |
| race_results.csv | 17,197 |
| payouts.csv | 17,474 |
| race_lines.csv | 330 |

範囲: 2026-03-26 〜 2026-04-26（うち lines は 2026-04-26 のみ完全）

### 未対応（Phase 3+）
- ⏳ 5年分バックフィル（**別PCの主タスク**）
- ⏳ 日次 cron でライン蓄積（**別PCの主タスク**）
- ⏳ 事前オッズ取得（要ログイン or 第三者サイト）
- ⏳ Supabase 移行
- ⏳ LightGBM 検証

---

## 2. 別PCのセットアップ手順（ゼロから）

### 2.0 前提（別PC）

- Windows 10 / 11
- Python 3.13 インストール済（python.org or Microsoft Store）
- Git for Windows インストール済
- ブラウザで GitHub にログイン可能（tower2007 アカウント）

### 2.1 作業フォルダ作成 + bundle から clone

転送用ファイル: `keirin-ai.bundle`（作業用PCの Desktop で生成、USB / OneDrive 等で持ち込み）

```cmd
mkdir C:\Users\<username>\Claude-Project
cd C:\Users\<username>\Claude-Project
git clone C:\path\to\keirin-ai.bundle keirin-ai
cd keirin-ai
git remote remove origin
```

bundle には main + data-snapshot ブランチが含まれている。
**作業は main で行うが、初回のみ data-snapshot から CSV を取り出す**:

```cmd
git checkout origin/data-snapshot -- data/
```

これで `data/` 配下に作業用PCの 1か月分データ（2026-03-26 〜 2026-04-26）が展開される。
取り出された data/ 配下は .gitignore 対象なので tracked にはならない（untracked のまま、main は綺麗）。

ブランチ確認:
```cmd
git branch -a
# * main
#   remotes/origin/HEAD -> origin/main
#   remotes/origin/data-snapshot
#   remotes/origin/main
git log --oneline -5
```

### 2.2 依存関係インストール

```bash
python -m pip install -r requirements.txt
```

必要パッケージ: `requests>=2.31`, `python-dotenv>=1.0`

### 2.3 環境変数（`.env` 作成）

`.env.example` をコピーして `.env` を作る:

```env
KEIRIN_USER_AGENT=Mozilla/5.0 (keirin-ai research; +https://github.com/Tower2007/keirin-ai)
KEIRIN_REQUEST_DELAY_SEC=0.8
```

`KEIRIN_REQUEST_DELAY_SEC` は **0.8 推奨**（autorace と同様、これ以下にするとサーバ負荷／IP制限懸念）。

### 2.4 動作確認

```bash
python smoke_test.py 2026-04-26 42
# data/smoke/ に HTML/JSON が保存されれば OK
```

---

## 3. 5年バックフィル実行

### 3.1 起動コマンド

```bash
python backfill.py 2021-04-26 2026-04-25 > backfill.log 2>&1
```

PowerShell バックグラウンド実行する場合:
```powershell
Start-Process python -ArgumentList "backfill.py 2021-04-26 2026-04-25" -RedirectStandardOutput backfill.log
```

### 3.2 試算

| 項目 | 値 |
|---|---|
| 期間 | 1825 日 |
| 場数 | 43 |
| 候補 venue-day | 78,475 |
| 平均 ingest 時間 | 18 秒/日 (開催あり), 1.5 秒/日 (開催なし) |
| **想定所要時間** | **約 45 時間（≒ 2 日）** |

### 3.3 中断/再開

- `Ctrl+C` で安全に中断（finally で stats 保存）
- `data/backfill_done.txt` に記録された venue-day はスキップされ、続きから再開
- ネットワーク障害時は `client.reset_session()` 自動で復旧（修正済）

### 3.4 進捗確認

別ターミナルから:
```bash
wc -l data/backfill_done.txt
# done venue-day の数を確認
tail -20 backfill.log
# 直近の進捗
```

### 3.5 短縮オプション（このPCの1か月分を流用）

このPCの `data/` 配下を別PCにコピーすれば、その範囲はスキップされて **約 75 分節約** できる:

```bash
# このPC側
tar czf keirin-data.tar.gz data/
# 転送 → 別PC で展開
tar xzf keirin-data.tar.gz
# その後 backfill.py を実行
```

---

## 4. 日次タスク登録

### 4.1 朝7時: ライン情報取得

```cmd
schtasks /create /tn "keirin-ai-lines" /sc daily /st 07:00 ^
  /tr "python C:\path\to\keirin-ai\ingest_today_lines.py"
```

**重要**: 第1R 発走前（通常 9〜10時）に実行必須。PJ0305 の nInfo は完了後に空になる。

### 4.2 翌朝5時: 結果・払戻回収

PowerShell で前日の全開催場をループ（差分対応のため、開催無し場は瞬時スキップ）:

```powershell
schtasks /create /tn "keirin-ai-results" /sc daily /st 05:00 /tr "powershell -Command `"cd C:\path\to\keirin-ai; `$y=(Get-Date).AddDays(-1).ToString('yyyy-MM-dd'); 11,12,13,21,22,23,24,25,26,27,28,31,32,34,35,36,37,38,42,43,44,45,46,47,48,51,53,54,55,56,61,62,63,71,73,74,75,81,83,84,85,86,87 | ForEach-Object { python ingest_day.py `$y `$_ }`""
```

### 4.3 確認・削除

```cmd
schtasks /query /tn "keirin-ai-*"
schtasks /delete /tn "keirin-ai-lines" /f
```

---

## 5. データ整合性チェック

5年バックフィル完了後、以下のスクリプトで整合性確認:

```bash
python -c "
import csv, sys
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding='utf-8')

def load(n):
    with open(f'data/{n}', encoding='utf-8') as f: return list(csv.DictReader(f))

meta = load('race_meta.csv')
entries = load('race_entries.csv')
results = load('race_results.csv')

print(f'meta: {len(meta)}, entries: {len(entries)}, results: {len(results)}')

# PK 重複チェック
def race_key(r): return (r['race_date'], r['place_code'], r['race_no'], r['car_no'])
dup_e = sum(1 for c in Counter(race_key(r) for r in entries).values() if c > 1)
dup_r = sum(1 for c in Counter(race_key(r) for r in results).values() if c > 1)
print(f'duplicate keys: entries={dup_e}, results={dup_r}')

# entries vs results 不一致
ec = Counter((r['race_date'], r['place_code'], r['race_no']) for r in entries)
rc = Counter((r['race_date'], r['place_code'], r['race_no']) for r in results)
mismatch = sum(1 for k in rc if k in ec and rc[k] != ec[k])
print(f'entries-results 選手数不一致: {mismatch} レース')

# 失格理由カバレッジ
null_t = [r for r in results if not r.get('tyaku','').strip()]
ks = sum(1 for r in null_t if r.get('kojin_state','').strip())
print(f'tyaku NULL: {len(null_t)}, kojin_state あり: {ks} (= 100% であれば OK)')
"
```

期待される結果（5年分）:
- meta: 約 9,000 venue-day
- entries: 約 600,000+
- duplicate keys: 0
- mismatch: 0
- kojin_state coverage: 100%

---

## 6. データ同期（別PC ↔ 作業用PC）

### 推奨フロー

別PC は「データ収集」、作業用PC（このPC）は「ML / 分析」と役割分担。
週1回程度、別PC → 作業用PC にデータをコピー。

### 同期スクリプト例（rsync）

別PC で:
```bash
rsync -av data/ user@workpc:/path/to/keirin-ai/data/
```

OneDrive / Google Drive / USB でも OK。

### 別PC で git push しない方針推奨

- `data/` は `.gitignore` 対象（CSV はコミットしない）
- データは別チャネルで同期、コードのみ git で管理

---

## 7. ファイル構成（要約）

```
src/
  client.py             KeirinClient (cookie session, GET only, retry)
  parser.py             HTML 内 jsonData 抽出 + flat dict 変換
  storage.py            CSV 読み書き (data/ 配下)
smoke_test.py           1日分の生 HTML/JSON を保存
ingest_day.py           1 venue-day を CSV に投入 (差分対応)
ingest_today_lines.py   今日の全開催場のライン取得 (cron 用)
backfill.py             過去データ一括取得
CLAUDE.md               プロジェクト運用ガイド (API 仕様、スキーマ等)
SESSION_HANDOFF.md      この文書
.env.example            環境変数テンプレ
requirements.txt        依存関係
data/                   CSV (.gitignore)
```

---

## 8. 既知の制約・注意点

### ライン情報は過去分が取れない
- PJ0305 nInfo はレース完了後に空。
- → 日次 cron で「これからのレース」のみ蓄積していく
- 5年バックフィルでは race_lines.csv は ほぼ空のまま

### 事前オッズは未対応
- `/pc/racevote` がログイン必須にリダイレクト
- 代替: netkeirin / kdreams スクレイピング（Phase 3 候補）

### Windows cp932 出力
- 進捗ログは ASCII のみ
- Python から日本語を `print` する場合は `sys.stdout.reconfigure(encoding='utf-8')` 必須

### IP 制限
- リクエスト間隔 0.8 秒が前提
- 並列化は要慎重（autorace と同じ理由）

---

## 9. トラブルシュート

### `WinError 10054` / `Connection reset`
→ `client.reset_session()` が自動呼出。backfill 続行可能。

### `Lines empty (race already finished?)` 警告
→ ingest_today_lines を遅い時間に実行している。朝7時に変更。

### `has_race_day` が遅い（CSV 巨大化時）
→ 5年分蓄積後は CSV が数百 MB になる。整合性チェックなど分析時は
   pandas / DuckDB に読み込む方が高速。

### 整合性監査で異常検出
→ 「データ整合性監査の手順.md」（あれば）または
   `git log` で過去のコミットメッセージ参照（`587586f` でバグ修正履歴あり）

---

## 10. 直近のコミット履歴（重要なものだけ）

| コミット | 内容 |
|---|---|
| `a990aa5` | ingest_day 差分対応 + ingest_today_lines.py |
| `587586f` | race_results に kojin_state / tyaku_note 追加（失格理由）|
| `fbd0861` | エラー隠蔽バグ修正 + reset_session 追加 |
| `b236025` | backfill.py 追加 |
| `6a63086` | Phase 2 (選手成績・結果・払戻) |
| `66b22e5` | 初版 Phase 1 (出走表) |

---

## 11. 次のセッションでやりたいこと

別PCでバックフィル中に並行して:
- ML 試作（基本特徴量での着順予測）
- 整合性監査の自動化
- Phase 3: 第三者サイトからの事前オッズ取得検討

何かあれば CLAUDE.md / メモリ更新で記録。
