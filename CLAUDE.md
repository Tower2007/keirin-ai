# keirin-ai 運用ガイド

## プロジェクト概要

競輪（keirin.jp）のデータ蓄積・可視化・ML検証アプリ。
姉妹プロジェクト: auto-racing-ai (オートレース)、boat-racing-ai (競艇)。

## 技術スタック

- Python 3.13
- DB: ローカル CSV (DATA_DIR 配下) — Phase 5 で Supabase 移行予定
- データ取得: keirin.jp（公式）の HTML 内埋め込み JSON を抽出 / オッズは netkeirin
- ML: LightGBM (ml_baseline.py / ml_ev_backtest.py 実装済)

## AI 協働運用

複数 AI で意見を出し合って改善する体制 (Auto_racing_AI と同パターン)。

| AI | 役割 | 編集権限 |
|---|---|---|
| Claude Code | 実装本体 | プロジェクト全体 |
| Codex | 分析・数値検証・代替案立案 | `Opinion/` のみ |
| Gemini | 大局観・第三者レビュー (スポット) | `Opinion/` のみ |

詳細: `AGENTS.md` (Codex 用), `GEMINI.md` (Gemini 用), `Opinion/README.md`

## データ保存先 (DATA_DIR)

`.env` の `DATA_DIR` で指定。省略時はプロジェクト直下 `data/`。
**重要: 大容量ファイル (race_odds.csv 約 2.5 GB) の active write 中に
Google Drive Stream を使うと事故る** (Drive の "Free up space" が
ローカルキャッシュを消去 → cloud は upload 未完で壊れる)。

推奨運用:
- **本番 DATA_DIR はローカル SSD/HDD** (例: `D:\keirin-ai-data`)
- **Drive は完成 ZIP の保管庫** (`scripts/backup_to_drive.py` 経由、cron は `scripts/cron_backup_drive.bat`)
- **別 PC への引き渡しは ZIP 経由** (ブラウザ DL → ローカル展開)

## ディレクトリ構成

```
src/
  client.py             # KeirinClient (cookie session, GET only)
  parser.py             # HTML 内 jsonData 抽出 + flat dict 変換
  storage.py            # CSV 読み書き (data/ 配下)
smoke_test.py           # 1日分スモークテスト (HTML/JSON 保存)
ingest_day.py           # 1日分データ取得 → CSV 保存 (差分対応)
ingest_today_lines.py   # 当日全場のライン取得 (cron 朝実行用)
backfill.py             # 過去データ一括取得
data/                   # CSV + スモーク (.gitignore)
```

## 運用フロー

ingest_day.py は **差分取り込み** に対応しているため、同じ venue-day を複数回呼べる:

| タイミング | スクリプト | 取得されるもの |
|---|---|---|
| 朝 7:00 (cron) | `ingest_today_lines.py` | 当日開催の全場の meta/entries/**lines**/stats |
| **朝 8:00 (cron, daemon)** | **`ingest_odds_prerace_daemon.py`** | **発走 5 分前 pre-start odds snapshot** (Phase 6 観測基盤) |
| 翌朝 5:00 (cron `keirin-ai-results`) | `ingest_yesterday.py` | 昨日の全場の results/payouts + **直近 10 日の遡り再取得** (race_meta に無い日は全場、race_completion に missing がある日はその場だけ。PC 停止の取りこぼしを自動で補う。2026-11-12) |
| 月曜 7:30 (cron) | `weekly_drift_report.py --mail` | 過去 7 日蓄積状況 + drift を Gmail 通知 (サイレント障害早期検知) |
| 月曜 3:00 (cron) | `scripts/backup_to_drive.py --keep 3` | D: ドライブ全データを ZIP 化 → Google Drive へ転送 (最新 3 世代保持) |
| 月曜 6:00 (cron `keirin-ai-weekly-retrain`) | `scripts/cron_weekly_retrain.bat` (build_race_quality → build_race_completion → `weekly_retrain.py`) | 週次 ML 再学習 + 採用品質ゲート (ROI 劣化拒否権つき) |
| 毎日 7:30 (cron `keirin-ai-shadow-predict`) | `daily_shadow_predict.py` | 当日レースのシャドー予測を追記 (pythonw 起動、発走時刻ガードあり) |
| 月曜 7:30 (週次メールの一部) | `shadow_eval.py` (`weekly_drift_report.py` が呼ぶ) | **shadow 前向き評価 + 凍結ゲートB 判定 + 打ち切り条件** (下記「shadow 評価とゲートB」) |

### pre-start odds snapshot 設計 (Phase 6, 2026-05-12 〜)
- (2026-11-12 更新) 取得時点は既定 `5,3,2,1,0.5` 分前の 5 点。fail (通信/JSON エラー) は発走 10 秒前までなら
  3 秒後に 1 回だけ再試行、発走時刻は同一場を 10 分間隔・取得の合間に 1 場ずつ keirin.jp で再確認して
  繰上げ/繰下げ・終了・中止に追随する (`apply_program_update`)。取得完了時刻・所要・HTTP Date は
  `prerace_capture_timing.csv` に記録 (`capture_lag_sec` は sleep 明け基準で HTTP 所要を含まないため。
  `official_dt` は発走前 API が返さず 5 月から一貫して空 = 仕様、サイト時刻の独立確認は Date ヘッダで代替)。
  コード変更は次回のデーモン起動から効く (稼働中のプロセスは止めない)。
- 朝 8:00 に 1 度 `pythonw.exe` (コンソール非表示) でデーモン起動
- 当日のレース全部について「st_time - 5 分」を計算 → 内部 `time.sleep()` で待機 →
  ピンポイントで snapshot 取得 → 最終レース後に exit
- 取得タイミング精度: ±数秒 (毎分 cron 方式 ±1分 より高精度)
- CMD ウィンドウ点滅なし (毎分 cron の UX 問題を解消)
- 保存先: `race_odds_prerace_YYYY-MM.csv` (race_date の年月で月次パーティション、2026-09-04〜。
  既存 `race_odds.csv` とは別ファイル)。旧単一ファイル `race_odds_prerace.csv` (800MB/13.6M 行) は
  `scripts/partition_odds_prerace.py` で分割済み (元は `.bak-<日時>` で保持)。
  **読み手は必ず `src/odds_prerace.py` の `read_prerace` / `load_prerace_dedup` を使う**
  (月次 + 残っていれば旧ファイルを統合し、確定月の dedup を `cache/odds_prerace_dedup_YYYY-MM.parquet`
  にキャッシュ)。`pd.read_csv(DATA_DIR / "race_odds_prerace.csv")` を直接書かないこと。
  回帰テスト: `tests/test_odds_prerace.py`
- `snapshot_dt` (こちらの観測時刻) と `official_dt` (netkeirin 表示時刻) を分離記録
- 試行ログは `prerace_snapshot_log.csv` (成功・失敗・no_odds を全部記録、dedup に使用)
- 7:00 lines cron 失敗時は entries 未準備で最大 50 分 retry してから諦め
- 既存 `race_odds.csv` は post-start (st_time + 0〜5 分) の "最終オッズ近似"、
  リーケージのため**本番 EV 評価には使えない** (Codex 発見、Gemini 確認)。
  upper-bound 検証用に保持。詳細: `Opinion/CodexOpinion.md` 2026-05-12 エントリ参照

### ⚠️ Phase A: 約定実現性 (settlement realizability) 検証 (2026-06-06 開始)
30 エージェント収益化議論 (Opinion 参照) の最重要発見「5 分前 snapshot odds は
約定できる価格ではない」を実データで検証するフェーズ。

#### 初回測定結果 (`analyze_settlement_wedge.py`, 5/16〜6/5, 当選組 n≈1,100/券種)
執行ウェッジ = 実現配当(payout/100) / snapshot odds − 1。マイナス = 当選側で不利。
- **5 分前 snapshot は確定配当と大きく乖離**: median −0.9〜−6.7%、|wedge|>10% が当選切符の
  42〜65%。→ **5 分前 odds 基準の EV は信頼できない** (議論結論を実データが裏付け)。
- **2 分前にはほぼ収束**: median≈0%、|wedge|>10% は 4〜13% に激減。
- **ワイド(W) は構造的にダメ**: 2 分前でも median −6.7%、確定配当が snapshot レンジ内に
  収まるのは 66% のみ。レンジオッズで点価格約定できず、EV 検証には不向き。
- 逆選択は 3〜6 倍人気帯に集中 (median −8%)。穴(20倍+)はほぼ中立〜有利。

#### daemon 変更 (lead_mins に直前取得を追加)
- `LEAD_MINS_DEFAULT` を `"5,3,2"` → `"5,3,2,1,0.5"` に拡張 (float offset 対応, 0.5=30 秒前)。
- 目的: 2 分→0 分の残差ウェッジを直接観測し、約定可能な最遅 snapshot を特定する。
- **cron 変更不要** (引数なし呼び出しが新 default を自動採用)。
- `target_offset_min` は `5,3,2,1,0.5` で保存 (整数は int、0.5 のみ float)。
  `analyze_settlement_wedge.py` / `weekly_drift_report.py` は float offset 対応済。
- 締切直前は複数場のレースが同時刻に集中するため一部 0.5 分 snapshot が数秒遅れる可能性。
  実測 `minutes_before_start` を記録するので分析側で吸収。

#### 合格基準 / 現状 (2026-11-12 更新)
- 蓄積は 5/3/2/1/0.5 分前の 5 点で継続中。週次メールの §5 (実測ドリフト) と §8 (ゲートA) が毎週判定する。
- 実測 (11/09 週): 1 分前・0.5 分前は中央値 0%・|ズレ|>10% が数% まで収束。ゲートA の「4 週連続」を
  止めているのは主に **取得率 (PC 停止日)** で、市場側の品質ではない (S4U 化が未適用の間は継続)。
- 「最遅約定可能 snapshot → 確定の乖離が必要エッジ幅より小さく系統的逆選択無し」を満たせば本番 EV 設計続行、
  満たさなければ「5 分前 odds 基準の EV は机上計算」と確定し直接賭けは棄却する。続行/打ち切りの判断は
  下記「shadow 評価とゲートB」の打ち切り条件に従う。

### ✅ Phase B: 本番 EV 配管検証 (2026-06-06 完了)
日付重複ゼロのブロッカー (picks 4/26 凍結 vs prerace odds 5/16〜) を解消し、
production_no_odds 予測 → 組合せ確率 → live odds 結合の「配管」を通した。
**配管検証のみ。券種優劣・黒字赤字は一切結論づけない (本番判定は Phase C 以降)。**

#### 実施内容
1. `build_race_quality.py` 再実行 → `race_quality.csv` 解凍 (4/26 → 6/5, normal 13,544 venue-days)。
2. `ml_baseline.py` 再実行 → `ml_picks.csv` 解凍 (2025-01-01〜2026-06-05)。
   同時に **per-car 確率 `ml_pred_probs.csv` を新規出力** (EV 配管の材料)。
   make_picks は単一推奨組しか出さないため、車番単位 p_win/p_top2/p_top3 を別途保存。
3. `ev_prerace_pipeline.py` 新規: per-race 正規化 → Harville で ST2/SH2 組合せ確率
   → `race_odds_prerace` を (race,bet,kumi) 最新 snapshot に dedup → EV 結合。
   - 二車単 ST2 (順序 i→j): P = q_i·q_j/(1−q_i)
   - 二車複 SH2 (無順序): P(i-j)+P(j-i)

#### 健全性 2 指標 (両方クリア)
- **(1) overround**: dedup 後 Σ(1/odds) median = SH2 1.347 / ST2 1.342 / RH3 1.342 /
  RT3 1.336 (implied takeout 25〜26%)。生 1.18M → dedup 395k (≈3 offset 分が正しく畳まれた)。
  → odds dedup + implied prob 配管は正しい。**W は 4.33 (range odds の min を使うため過大、既知の特殊形式)**。
- **(2) all-buy ROI**: **pool 加重 (market implied 比例) で全 exotics ≈ −26.5〜−27.1%**
  → 清算ロジック健全。理論 1/overround−1=−25.4% との差 ≈2% は Phase A の執行ウェッジと一致
  (snapshot で賭け realized で清算するため)。Phase A↔B が独立に整合。
  uniform 均等買いは exotics で −40% 前後 (本命勝ち bias の正常挙動、清算検証には pool を使う)。

#### 配管健全性
- Harville 正規化: ST2/SH2 とも Σp_combo per-race = **1.0000** (完璧)。
- EV 結合: picks×odds 重複 1,066 レース、combo カバレッジほぼ 100% (70,080 行)。
  → 日付重複ゼロのブロッカー解消。出力 `ev_prerace_merged.csv`。
- ⚠️ 結合 EV は snapshot odds ベース。Phase A より snapshot は確定配当に対し楽観
  (当選側 −2〜7%) なので、EV>1 割合は上方バイアス。**黒字判定には使えない**。

#### 次アクション (Phase C への入口)
- 母数蓄積 + Phase A の最遅約定 snapshot 確定後、執行ウェッジ込み二重 EV 計算
  (snapshot odds と realized 両方) + bootstrap CI + FDR 補正で初めて採否判定。

### 凍結基準 (freeze gates、Codex 2026-07-11 提案を採択)
**「観測設定の固定」と「戦略の固定」を分離した二段階 gate。**
数値は初期の運用規約であり、通過は収益の保証ではない。
gate A は `weekly_drift_report.py` が毎週自動判定する (セクション 7)。

```text
A. snapshot timing lock（例: 0.5分前を採用するか）
  - 4週連続、各週で独立した settled races >= 150
  - planned race に対する ok coverage >= 98%
  - 発走後取得 = 0、|capture_lag_sec| の p95 <= 10秒
    （abs: 予定より早すぎる取得も遅延と同様に検知）
  - capture_lag_sec の null 率 <= 50%（週×offset 単位）。
    null 率 > 50% は「lag 未計測」として fail
    （2026-07-11 監査: 秒精度列 null の ok 行の黙った素通しを塞ぐ追加条件。
     既存基準の緩和ではない）
  - 券種×offset ごとに |median wedge| <= 1.0%
  - 同じ母集団で P90(|wedge|) <= 5%、P(|wedge| > 10%) <= 5%
  - 未解決 missing_results / missing_payout = 0（直近14日）

  分母と最小標本数の定義（固定、2026-07-11）:
  - lag null 率の分母 = その週×offset の prerace_snapshot_log で
    status=ok の行数（行単位。レース dedup はしない）。
    分子 = そのうち capture_lag_sec が欠損（数値化不能含む）の行数。
  - ok 行数 = 0 の週は null 率 100% とみなし「lag 未計測」で fail
    （coverage 98% でも fail するが、定義として明示固定）。
  - |lag| p95 は capture_lag_sec が非 null の ok 行のみで計算する。
    最小標本数は上の null 率条件が兼ねる（非 null が ok 行の 50% 超
    なければそもそも fail し、p95 は判定に使われない）。

B. strategy/policy lock（EV 閾値・券種を固定して shadow 評価するか）
  - A を満たした timing だけを候補にする
  - policy_id、校正器、候補生成規則、比較する閾値群を事前登録する
  - strategy × offset ごとに独立 selected races >= 200、かつ4週以上
  - 全閾値・券種の比較に FDR 補正を適用し、評価は固定後の OOS のみ
```

### shadow 評価とゲートB (2026-11-12 実装: `shadow_eval.py`)
週次メール §10 に出る。判定は機械的で、購入開始は別判断。
- **official 行の規則**: `run_type=official` のみ評価に使う。同一 (race_date, place_code, race_no, car_no) が
  複数なら `predicted_at` 最新を採用 (rerun / legacy_unverifiable は事前時点性を主張できないため除外)。
- **前向き成績**: 週別 + 累計で、p_win/p_top2/p_top3 の対数損失 (定数予測との差つき) と、make_picks と
  同じ買い目 (二車複/二車単/三連複/三連単 各 1 点 100 円) の的中率・ROI を確定払戻で清算。
- **ゲートB (事前登録)**: 戦略 = {SH2, ST2} x EV 閾値 {1.00, 1.10, 1.20} (EV = Harville 組合せ確率 x その取得時点のオッズ)、
  取得時点 = {5,3,2,1,0.5}。計 30 組すべてを BH-FDR (q < 0.10) の母集団にする。
  合格 = ゲートA 合格の (取得時点, 券種) で、選択レース >= 200・選択が存在する週 >= 4・ROI > 0 (確定払戻基準 =
  執行ウェッジ込み)・FDR q < 0.10。OOS は **ロック日 2026-11-13 以降のレースのみ**。ゲートA 未合格の組は
  「参考値」表示で判定対象外。戦略集合や閾値を変えるときは CHANGELOG に理由を残す (後から緩めない)。
- **打ち切り条件 (期限と件数)**: ロック日から 12 週 (2027-02-05) 経ってもゲートA 合格 0 -> 取得運用 (PC 稼働・S4U 化)
  の見直しか観測打ち切りを判断。26 週 (2027-05-14) で合格候補なし、または全戦略で選択 1000 レースに届いても
  未合格 -> **打ち切り推奨**。到達すると週次メールに「判定の時期」と出る。ゲートB 合格候補が出た場合も同様に表示。
- 総合状態 (🟢🟡🔴) はデータ収集の健全性のみで決め、ゲートA/B は含めない。件名に「ゲートA n/20」を別掲する。

購入開始は B 通過後の別判断 (FDR 補正済み OOS で執行ウェッジ控除後も
正エッジが残ることが前提)。gate を後から緩める場合は理由を CHANGELOG に記録。

### cron 登録コマンド (pre-start daemon)
```
schtasks /create /tn "keirin-ai-prerace-daemon" /sc daily /st 08:00 ^
  /tr "C:\Python313\pythonw.exe C:\Users\no28a\Claude-project\keirin-ai\ingest_odds_prerace_daemon.py"
```

**必ず朝に lines を取得**: PJ0305 の nInfo は完了後に空になるため。

### ⚠️ JSJ048 朝の前日問題 (2026-05-16 発見、5/23 根本治療)

#### 初版修正の失敗 (5/16〜5/22)
5/16 に発見: JSJ048「今日のレース一覧」は朝 7:00 時点では前日分を返す。
初版修正は retry 12 回 × 300 秒 = **60 分** で粘る方式。
→ **5/17〜5/22 の 6 日連続で再び失敗** (JSJ048 が 60 分超でも前日返す日があった)。
   蓄積失敗が継続していたことに 5/23 朝に気付いた。

#### 根本治療 (5/23): JSJ048 完全撤廃
`ingest_today_lines.py` を `ingest_yesterday.py` と同じパターンに書き換え:
- 全 43 場 `raceprogram` (PC0201) を明示日付で直叩き
- 開催無しの場は PC0201 空で瞬時に no_race として 0.8 秒で抜ける
- 全 43 場ループでも **約 6 秒** で完走
- JSJ048 一切経由せず、「前日返し」問題が構造的に発生しない

#### 累計被害 (5/13〜5/22、10 日分)
**実害は限定的**: results cron (翌朝 5:00 明示日付) が entries/stats/results を
全て救済済。永久ロストは:
- race_lines narabi 5/13〜15 + 5/17〜22 = **9 日分** (低価値、弱ライン特徴量は不使用)
- pre-start odds snapshot 5/13〜15 + 5/17〜22 = **9 日分**
  (これが唯一の実害、Phase 6 蓄積開始が実質 5/23 から)

ML 本線データ (production_no_odds) は完全無傷。

#### 教訓
- **AGENTS.md / CLAUDE.md の「センセーショナルな結果は測定バグを疑う」が二度目の的中**
- retry で粘るのは脆い対症療法、JSJ048 のような外部依存は構造的に排除すべきだった
- 週次 drift report を未稼働にしていたのが致命的 (蓄積 0 を 6 日見逃した) → T2 で対処予定

### Windows タスクスケジューラ登録例
```cmd
schtasks /create /tn "keirin-ai-lines" /sc daily /st 07:00 ^
  /tr "python C:\Users\user\Claude-Project\keirin-ai\ingest_today_lines.py"
```

## CSV ファイル構成 (data/)

| ファイル | 内容 | キー | 取得タイミング |
|---------|------|------|------|
| race_meta.csv | 開催メタ（場・グレード・レース名） | race_date + place_code | レース前後どちらでも |
| race_entries.csv | 出走表（選手・脚質・予想印） | race_date + place_code + race_no + car_no | レース前後どちらでも |
| race_lines.csv | ライン情報 (narabiX/narabiY) | race_date + place_code + race_no + car_no | **レース前のみ**（完了後は nInfo 空配列） |
| race_stats.csv | 選手成績（勝率/連対率/戦法回数等） | race_date + place_code + race_no + car_no | レース前後どちらでも (JSJ002) |
| race_results.csv | 着順・決まり手・天気 | race_date + place_code + race_no + car_no | **レース完了後のみ** (JSJ012) |
| data_gaps.csv | 事後に取れない欠測の台帳 (lines / prerace_odds / shadow。理由つき) | kind + race_date + place_code | 遡り取得時・手動 |
| prerace_capture_timing.csv | 直前オッズ取得の完了基準タイミング (2026-11-13 〜) | race + offset | daemon |
| payouts.csv | 払戻金 (7券種) | race_date + place_code + race_no + bet_type + kumi_ban | **レース完了後のみ** (JSJ012) |

## keirin.jp スクレイピング仕様

公式サイトは **JSF (WebOTX) で HTML レンダリング** だが、
ページ HTML 内に `jsonData['XXXXX'] = {...};` の形でデータが埋め込まれているため、
正規表現抽出で JSON 同等の処理が可能。

### 主要エンドポイント (全て GET、CSRF 不要)

| 用途 | URL | データ |
|---|---|---|
| 当日全場レース一覧 | `GET /pc/json?type=JSJ048&...` | 純JSON |
| 開催プログラム | `GET /pc/dfw/dataplaza/guest/raceprogram?KCD=&KST=YYYYMMDD` | HTML 内 `jsonData['PC0201']` |
| 出走表一覧（全12R） | `GET /pc/racelist?encp={encParaR}` | HTML 内 `jsonData['PJ0305']` |
| 選手成績 (全12R分) | `GET /pc/json?type=JSJ002&encp={encParaR}` | 純JSON、`raceInfo[12].sensyuTypeInfo[]` |
| 結果 + 払戻 (1R分) | `GET /pc/json?type=JSJ012&encp={encParaR}` | 純JSON、`tyakujyunItemSubData` + `haraiGakuSubData` |

- セッションCookie 必須: 初回 `/pc/top` を踏んで取得
- リクエスト間隔: 0.8秒 (`.env` の `KEIRIN_REQUEST_DELAY_SEC`)
- `encParaR` は raceprogram の PC0201 内 `C0201race[].encParaR` から取得
- 1ページで12レース分の出走表が取れる（autorace の per-race POST より効率的）

### 場コード (jocd / KCD / naibuKeirinCd)

2桁コード。北から:
11 函館, 12 青森, 13 いわき平,
21 弥彦, 22 前橋, 23 取手, 24 宇都宮, 25 大宮, 26 西武園, 27 京王閣, 28 立川,
31 松戸, 32 千葉, 34 川崎, 35 平塚, 36 小田原, 37 伊東, 38 静岡,
42 名古屋, 43 岐阜, 44 大垣, 45 豊橋, 46 富山, 47 松阪, 48 四日市,
51 福井, 53 奈良, 54 向日町, 55 和歌山, 56 岸和田,
61 玉野, 62 広島, 63 防府,
71 高松, 73 小松島, 74 高知, 75 松山,
81 小倉, 83 久留米, 84 武雄, 85 佐世保, 86 別府, 87 熊本

## コーディング規約

- 出力言語: 日本語
- docstring: 日本語
- 変数名: snake_case (英語)
- 進捗ログ: ASCII のみ（Windows cp932 対策。Python の `print` 経由で日本語出すときは
  `sys.stdout.reconfigure(encoding='utf-8')` を呼ぶ）
- 全角→半角は parser で NFKC 正規化（「Ｓ級一般」→「S級一般」）

## ML モデルの用途分類 (Phase 6 pivot 2026-05-15)

`ml_baseline.py` で生成される各構成は **本番候補** と **研究用** に分かれる:

| フラグ | 用途分類 | 本番判定使用可? |
|---|---|---|
| なし | `production_no_odds` (**本線**) | ✅ |
| `--use-line` | `base_line_shadow` (shadow 候補) | △ live n≥50 で判定 |
| `--use-odds` | `final_odds_upper_bound` | ❌ 研究のみ |
| `--use-odds --use-w-odds` | `final_odds_w_upper_bound` | ❌ 研究のみ |
| `--use-odds --use-line` | `final_odds_line_upper_bound` | ❌ 研究のみ |
| `--use-odds --use-mispricing` | `final_odds_mp_upper_bound` (5/13 失敗確認) | ❌ |

**本番 EV 計算**: `production_no_odds` のモデル予測 × `race_odds_prerace_YYYY-MM.csv` の
live snapshot odds を使う (Codex 5/15 設計)。`race_odds.csv` の post-start odds
を特徴に入れたモデルは drift で使えないため。

詳細: `Opinion/multi_model/proposal.md` 参照

### 方針 (2026-11-12): オッズ非依存モデルへの追加投資はやめる。計器だけ残す

根拠 (統合監視 精度検証 2026-11-12、前向き = 朝の official shadow × 直前二車単):
- 7/13〜11/11 の 8,746 R で 1 着の二値 logloss はモデル単体 0.3256 / 市場 0.2992 / 結合 0.2993。
  結合時のモデル係数 b は 0.138 (8 月適用) → 0.034 → 0.016 → 0.009 (11 月適用、95%CI [-0.033, +0.051]) と
  0 に収束し、9 月以降は 0 と区別できない。7/3 の `market_blend_eval.py` (coef_model≈0) の前向き再確認。
- レース単位 1 着対数尤度 (8〜11 月 7,337 R): モデル 1.4935 / 市場 1.3564 / 市場^α (α≈1.09〜1.13) 1.3540。
- よって本線 (朝の `production_no_odds`) は**止めない・差し替えない**が、特徴量追加・チューニング等の
  追加投資はしない。精度の計器 `market_anchor.py` (週次 §10 に 2 行、履歴 `D:\keirin-ai-data\market_anchor_history.json`)
  で b が 0 と区別できるようになったら方針を見直す。
- 例外: 実ラインの市場残差 (`eval_line_market_residual.py`, 2026-11-12) は係数 c の 95%CI が 0 をまたがなかった
  (c=+0.92 [+0.39, +1.52]) が、改善はレース単位 -0.0014 (10〜11 月 2,358 R、市場^α 1.3483 → 1.3469) と小さく、
  控除率 (約 25%) の壁を越える根拠にはならない。測定のみで本番の特徴量セットは変えていない。

採用ゲート v3 (2026-11-12): `weekly_retrain.py` は現役モデルを**同じ OOS 窓で採点し直して**比べる
(旧方式は前回 meta の別窓の値と比較していた)。同窓で AUC -0.003 / logloss +0.5% / ECE +0.005 超は NG、
AUC -0.001 / logloss +0.2% / ECE +0.003 超は WARN で**採用しない** (現役が 28 日以上古ければ鮮度優先で採用)。
現役を採点できないときだけ従来の別窓比較にフォールバック (gate_mode=cross_window)。ROI 拒否権は不変。
`ml_baseline.train_one` の early stopping は test ではなく学習期間の末尾 10% で行う (次回の学習から)。

## Phase ロードマップ

- ✅ **Phase 1**: 開催メタ・出走表・ライン情報 → CSV
- ✅ **Phase 2**: 選手成績 (JSJ002)・結果＋払戻 (JSJ012) (現在地)
- ⏳ **Phase 3**: 事前オッズ取得 (要ログイン or netkeirin等の代替)
- ⏳ **Phase 4**: backfill.py (5年分一括取得)
- ⏳ **Phase 5**: Supabase 移行 (auto-racing-ai と同じ構成)
- ⏳ **Phase 6**: LightGBM での予想検証

## 払戻券種コード

| code | 名称 |
|---|---|
| WH2 | 枠複（多くは未発売） |
| WT2 | 枠単（多くは未発売） |
| SH2 | 二車複 |
| ST2 | 二車単 |
| RH3 | 三連複 |
| RT3 | 三連単 |
| W | ワイド（3組合せ返却） |

## 既知の注意点

- HTML 内 jsonData は単引用符・二重引用符 両方で出現する（parser は両対応）
- HTTP 5xx は urllib3 Retry で自動再試行（最大5回、指数バックオフ）
- 重複投入防止: ingest_day は `race_meta.csv` に同じ race_date+place_code があれば skip
- 未開催日（KCD+KST に開催なし）: PC0201 が空または `C0201data` 不在で判定
