# CHANGELOG

## 2026-11-15 (並び欠損の取り直し・取り込みの終了コード)

- **並び (race_lines) 欠損の原因を数字で確認**: 7/1〜11/15 (PC 停止 2 日除く) でレース単位 17.0% 欠損、うち **93% (1,631/1,751 R) が
  発走 20 時以降のミッドナイト開催** (場・日単位で 64% が 7:00 時点で未掲載。昼・ナイターは 1〜3%)。レース単位の部分欠損は 0 (全 R か 0 R)。
  実測で、ミッドナイト 2 場の並び掲載更新は 11:12 / 11:25 (07:00 の取り込み後)。仮説どおり。
  なお本番モデルの特徴量 (41 列) に並び由来の列は無く、朝のシャドー予測は並び欠損の影響を受けない (影響は実ライン特徴量の再投入時の学習データ)
- **当日の並び取り直し** `src/lines_fill.py` 新設 + prerace デーモンへ配線: 起動時に並び未取得の場・日を洗い、専用スレッド/専用クライアントで
  未発走の最初のレースの 120/60/30/10 分前に取得 (未取得レースだけ追記・取得済みは再取得しない・二重行なし)。最後の確認でも未掲載なら
  data_gaps.csv (kind=lines) へ。オッズ取得ループとは独立で、例外はすべて握ってログ。pythonw 安全 (print/reconfigure なし)。
  タスクスケジューラは無変更。反映は次回デーモン起動 (翌朝 08:00) から。11/15 のミッドナイト 2 場 (42・53) は
  同じ関数で手動の 1 回取り直しをして 18 R 分を回収済み
- **欠損の可視化**: `build_race_completion.lines_gap_summary()` を新設。日次 (`ingest_yesterday` のログ 1 行)・`build_race_completion.py` の CLI・
  週次レポート §11 に「並びの欠損 R 数/率 (ミッドナイト内訳つき)」を出す (5% 超で ⚠️)
- **取り込みの終了コード**: `ingest_yesterday.py` は errors>0 / backfill の失敗・クラッシュ / 完了台帳の更新失敗で 1、`ingest_today_lines.py` は
  errors>0 で 1 (「当日だが並び未掲載」のような想定内の空は数えない)。`cron_*.bat` は変更不要 (python 直後の `exit /b %ERRORLEVEL%`、
  run_hidden.vbs も終了コードを返す) だが、テストで実際に cmd を通して 7 が返ることを確認
- tests: `tests/test_lines_fill.py` (19 件)

## 2026-11-12 (精度検証 Keirin C/E — 計器・ゲート・測定。本線の予測とモデルは不変)

- **市場基準の確率 (計器)** `market_anchor.py` 新設: 直前二車単 (dedup = 締切に最も近い snapshot) から市場の
  1 着確率を作り、べき乗補正 α と、モデルとの結合係数 b (条件付きロジット) を**前月までで当てはめて翌月で評価**
  (月次履歴 `D:\keirin-ai-data\market_anchor_history.json`、既存月は書き換えない)。b の 95%CI はレース単位
  ブートストラップ。週次メール §10 (`shadow_eval`) にモデル単体 / 市場 / 補正後市場 / 結合の対数損失と b の行。
  初回 (8〜11 月 7,337 R): レース単位 1.4935 / 1.3564 / 1.3540 / 1.3551、b = +0.009 [-0.033, +0.051]。
  方針「オッズ非依存モデルへの追加投資はやめる」を CLAUDE.md に根拠つきで追記 (本線は止めない)
- **実ラインの再評価** `eval_line_market_residual.py` 新設 (測定専用): 市場 logit を init_score にした LightGBM で
  市場残差だけを学習 (〜8/31, 6,337 R)、9 月で係数を当てはめ、10〜11 月 (2,358 R) で評価。残差マージンの係数
  c = +0.92 95%CI [+0.39, +1.52] (0 と区別できる) だが、レース単位対数損失は 市場^α 1.3483 → +ライン 1.3469 と小さい。
  本番の特徴量セットは変えない
- **採用ゲート v3** (`weekly_retrain.py`): 現役モデルを同じ OOS 窓・同じ行で採点し直して比較 (旧: 前回 meta の
  別窓の値と比較)。閾値を詰め (AUC -0.003 / logloss +0.5% / ECE +0.005 で NG、-0.001 / +0.2% / +0.003 で WARN)、
  **WARN は採用しない** (現役が 28 日以上古ければ鮮度優先で採用)。採点できないときは別窓比較へフォールバック。
  retrain_history に gate_mode / champ_*_same 列。ROI 拒否権 (roi_gate.py) は不変。
  再採点: 11/09 回を同窓で厳密に再計算 → 現役 AUC 0.80668 / 候補 0.80681、logloss 0.32502 / 0.32495 で OK (判定不変)。
  過去 25 回のうち別窓比較で WARN 採用だった 2 回 (6/15, 7/20) は v3 なら不採用になった可能性 (当時の現役モデルは
  残っていないため同窓の厳密な再計算は不可)
- **early stopping** (`ml_baseline.train_one`): test を valid に使っていたのを学習期間の末尾 10% に変更
  (過去の picks/probs は書き換えない。次回の学習から)
- tests: `tests/test_accuracy_1112.py` (8 件)

## 2026-11-12 (多角検証 12 指摘への一括対応)

- **欠測の自動補完 (指摘 2)**: `ingest_yesterday.py` に直近 10 日の遡り再取得 (`plan_backfill`/`run_backfill`) を追加。
  race_meta に無い日は全場、race_completion に missing がある日はその場だけ per-race 自己治癒。事後に取れない
  lines/発走前オッズ/shadow は `data_gaps.csv` (`src/gaps.py`) に理由つきで記録。`build_race_quality.py` は
  全開催場が同日に結果ゼロなら `all_canceled` でなく `results_missing` にする。
  `ingest_day.py`: 払戻が既にあるレースは heal 以外でも二重追記しない / lines 空の診断文を実態に修正。
  実データ: 10/14 (8 場 79 レース) と 10/15 (7 場 72 レース) を遡り取得して complete。
- **shadow 前向き評価とゲートB (指摘 1)**: `shadow_eval.py` 新設 (official のみ・predicted_at 最新で重複解決、
  対数損失/的中/ROI、事前登録 30 組の BH-FDR、打ち切り条件)。週次メール §10 に表示。CLAUDE.md を実態に更新。
- **タイミング計測 (指摘 3)**: daemon が `prerace_capture_timing.csv` (完了基準の遅延・所要・HTTP Date) を記録。
  `official_dt` は発走前 API が返さず仕様で空と判明。週次 §4 の実測は秒精度 (旧: 整数丸めで常に 0)。
- **週次メール (指摘 4・8)**: 本文冒頭と件名に「ゲートA n/20」、判定基準にゲートA/B 非含有を明記。日次状態に被覆率閾値
  (ライン < 50% / オッズ < 80% で ⚠️)。集計範囲を 1 日伸ばして前回の月曜を判定に含める。
- **payouts 重複除去 (指摘 5)**: 7/11 の遡り取得で二重化した 70 行を `ledger_check.py --dedupe` で除去
  (1,091,572 -> 1,091,502 行、キー重複 0、払戻合計不変)。週次メールに「キー重複 0」検査。
- **picks/probs の取りこぼし (指摘 6)**: `weekly_retrain.regenerate_missing_races` — 学習終端より後 (OOS) で
  既存 probs に無いレースだけを今回モデルで採点して追記 (再学習はしない。ゲート判定は無変更)。次回の週次再学習から有効。
- **daemon (指摘 8)**: fail は発走 10 秒前までなら 1 回再試行、発走時刻を取得の合間に 1 場ずつ再確認
  (繰上げ/繰下げ・終了・中止に追随)。次回の daemon 起動から有効 (稼働中プロセスは無変更)。
- **再学習の通知 (指摘 9)**: NG / WARN / ROI ERROR・NG / 異常終了で要確認メール (`build_attention_notice`)。
- テスト: `tests/conftest.py` でテスト中の実メール送信を禁止 (autouse)。`tests/test_audit_1112.py` 20 件追加。

## 2026-09-04 (直前オッズ CSV の月次パーティション化)

- **背景**: `race_odds_prerace.csv` が 807MB / 13.6M 行 (+230MB/月) に育ち、週次の
  `ev_prerace_pipeline.py` が毎回全読み (read 17.7s / RAM 1.9GB、1 年で 2.4GB 超見込み)。
- **書き手** `ingest_odds_prerace_daemon.py`: 追記先を `race_odds_prerace_YYYY-MM.csv`
  (**race_date の年月**) に変更。旧単一ファイルにはもう書かない。`src/storage.py` に
  `schema_for()` を追加し、月次名を `race_odds_prerace.csv` のスキーマに読み替える。
  鍵に race_date を選んだ理由: 読み手は全て race_date で期間指定する / dedup 鍵に
  race_date を含むためレースの全 snapshot が同一ファイルに閉じ、月単位の独立 dedup
  (=キャッシュ) が全体 dedup と厳密一致する。
- **読み手** 新設 `src/odds_prerace.py`: `read_prerace(usecols, dtype, start, end)` で
  必要月 (+残っていれば旧ファイル) だけ読む。`load_prerace_dedup()` は確定月 (当月より前)
  の dedup 結果を `DATA_DIR/cache/odds_prerace_dedup_YYYY-MM.parquet` にキャッシュ
  (元ファイルの size/mtime をフィンガープリントに保存し、変化すれば再計算。pyarrow 不在や
  書込失敗時はキャッシュ無しで続行)。dedup の意味論 (odds>0、(race,bet,kumi) 毎に
  snapshot_dt 最新) は不変でテストで固定。呼び替え: `ev_prerace_pipeline.py`
  (`--no-cache` / `--dry-run` を追加)、`weekly_drift_report.py` (2 箇所)、
  `analyze_settlement_wedge.py`、`market_blend_eval.py`、`phase_ac_analysis.py`、
  `scripts/rebuild_snapshot_log.py`。
- **移行** `scripts/partition_odds_prerace.py` (`--dry-run` あり): 逐次ストリームで RAM 一定、
  旧ファイルを `.lock` 下で `race_odds_prerace.csv.bak-<日時>` にリネームしてから月別 `.part`
  に転記 → 最後にまとめて反映。行数一致を検証して `<bak>.migrated.json` を書く。冪等
  (旧ファイルが無ければ no-op、再生成されていれば既存月次へ追記)。本番 D: で実行済み
  (13,603,868 行 → 5 か月、行数一致、21.4s)。
- テスト `tests/test_odds_prerace.py` 12 件追加 (混在読込=単一ファイル dedup 一致 /
  キャッシュ有無一致+無効化 / 移行の行数保存・冪等・dry-run・invalid 行隔離 /
  デーモン追記先が月別)。既存 30 と合わせ 42 全合格。
- 統合監視 (mgmt) `config/projects.yaml` の keirin `backend.files` は
  `race_odds_prerace.csv` を鮮度監視しているため、月次 glob への差し替えが必要 (mgmt 側で対応)。

## 2026-07-19 (採用ゲート v2: ROI 拒否権を「二車複」読み替えで実効化)

- **ROI ゲートの標準買い目を単勝 → 二車複へ読み替え** (f719f97 の移植時は
  競輪単勝非発売のため常時 SKIP の計器のみだった)。`roi_gate.py`
  (evaluate_roi_gate、艦隊共通仕様) は無変更。呼び出し側 `weekly_retrain.py`
  の前処理のみで実効化:
  - 買い目 = 各レース y_win 予測スコア上位 2 車 (同点は車番昇順で決定的) の
    二車複 1 点 100 円。的中判定は payouts.csv の二車複行 (bet_name='二車複'、
    kumi_ban '1=2' 形式) を 小-大 正規化キーで照合し、payout/100 を実質オッズ
    とする。同着等で複数行あるレースは該当組合せの行で照合。
  - 新設 `_parse_quinella_kumi()` (組合せ表記の正規化) と
    `_quinella_roi_frame()` (roi_gate docstring の「1 行 = 1 買い目候補」形へ
    整形: レースごとに champion 買い目行 + candidate 買い目行、指示変数
    p_champion/p_candidate=1.0/0.0 でレース内 argmax が自モデルの買い目を選ぶ)。
  - 除外規則: 二車複払戻が 1 行もないレースは NaN 化して roi_gate のレース単位
    ペア除外に委ねる。スコア非欠損 2 車未満・片側しか買い目を組めないレースも
    両側除外 (同一レース集合でのペア比較を維持)。
  - 閾値・seed は共通仕様のまま不変 (NG=-10pt+CI 有意 / WARN=-5pt /
    min_bets=200 / seed=20260719)。ERROR 素通し・履歴 CSV 記録などの
    フェイルセーフ構造も不変。
  - `tests/test_roi_gate.py` に QuinellaRewriteTests 7 件を追加
    (正規化・的中/外れ精算・同着複数行・払戻欠損除外・2 車未満除外・
    片側除外・champion=candidate フルパス)。既存 23 と合わせ 30 全合格。
  - dry-run (read-only、champion=candidate): OOS 8 週 (2026-05-24..07-18) で
    n_bets=4,212 (= OOS 全 4,212 レース、二車複払戻の被覆 100%)、
    絶対 ROI 73.9% (二車複の控除率 ~25% と整合、サニティ帯 55〜85% 内)、
    dROI=+0.00pt / CI[+0.00,+0.00] / verdict=OK を確認。
  - 本番モデル (*.lgb)・メタ・履歴 CSV は不変。race_odds.csv 不使用。
    pythonw 安全 (reconfigure 追加なし)。

## 2026-07-19 (採用ゲート v2: ROI 劣化拒否権を移植)

- **weekly_retrain.py に「採用ゲート v2: ROI 劣化拒否権」を追加**
  (hokkaido-keiba-ai 3d04c7c の 8PJ 艦隊共通仕様を移植。挙動・閾値・seed は共通)。
  - `roi_gate.py`: 純関数 `evaluate_roi_gate()` (pandas/numpy のみ依存、raise せず
    全例外を verdict="ERROR" に畳む)。champion / candidate の「y_win レース内
    argmax 車の単勝 1 点 100 円」ROI を同一 OOS 窓・同一レース集合でペア比較し、
    dROI <= -10pt かつペアブートストラップ (2000 回, seed=20260719) 95%CI 上限 < 0
    のときのみ NG (拒否権発動)。WARN: dROI <= -5pt / SKIP: n_bets < 200 /
    ERROR: フェイルセーフで精度ゲート判定のまま進行。
  - `weekly_retrain.py`: [4b] として精度ゲート直後・昇格前に最小結合。
    ROI veto NG なら 4 モデルとも昇格見送り (4 本一括採否の構造は不変)。
    --force は ROI 拒否権も無視。retrain_history.csv に roi_* 7 列を追加
    (旧ヘッダは tmp→os.replace で自動移行、旧行は空欄。ラグド CSV にしない)。
    meta / rejected メタに roi_gate を記録。
  - 単勝払戻は payouts.csv (payout/100 = 実質オッズ) から引く設計だが、
    **競輪の払戻データ (JSJ012) に単勝は存在しない**ため、単勝オッズ源が
    できるまでは常に SKIP (計器のみ設置)。race_odds.csv (2026-05-12 更新停止)
    は OOS オッズ源に使わない。
  - `tests/test_roi_gate.py`: 13 tests (NG/WARN/OK/SKIP/ERROR 5 経路 +
    seed 再現性 + ペア除外 + ヘッダ移行後方互換)。既存 10 と合わせ 23 全合格。
  - dry-run (read-only): 実 OOS 8 週 = 4,212 レース。実データは
    SKIP (単勝なし) を確認、合成単勝 + champion=candidate で
    dROI=+0.00pt / CI[+0.00,+0.00] / OK をフルパス確認。
    実 retrain_history.csv のコピーで移行 (8 旧行保持 + 28 列化) を確認。
  - 本番モデル (*.lgb)・メタ・fail-closed 経路は不変。pythonw 安全
    (reconfigure 追加なし、print のみ)。

## 2026-07-12 (Codex 艦隊監査 7/11 の P1/P3 対応)

- **P1: 結果/払戻の取込後に完了台帳 (race_completion.csv) が更新されない問題を修正**
  (`ingest_day.py:15` 指摘)。旧運用は週次 retrain の `--days 30` のみで台帳を再構築
  していたため、取込済みレースが最長 1 週間 `missing_results` のまま残り、
  週次 drift レポート (セクション 5 / gate A の missing_14d) が誤警告していた。
  - `build_race_completion.py` に `refresh(days)` を新設 (CLI と同一ロジックの
    関数化、stdout 出力なし = pythonw/cron 安全) し、取込経路 3 本に接続:
    - `ingest_yesterday.py` (日次 5:00 cron): 全場ループ後に自動 refresh
    - `ingest_day.py main()` (手動 ingest / per-race 自己治癒): results/payouts を
      追記した場合のみ refresh
    - `recover_missing_races.py` (救済 backfill): 実行後に全期間 refresh
      (旧「手動で再実行すること」の運用メモを自動化に置換)
    - refresh 失敗は収集の成否に影響させない (log/警告のみ、次回 cron で再計算)。
    backfill の連続呼び出し (`ingest_one_day` + index) は従来どおり呼ばない。
  - **台帳の実態整合: stale 行 72 件を complete に是正** (missing_results 205→133)。
    全て 2026-07-11 分 (7 場、監査指摘の特定 3 レースを含む。監査時点でデータが
    あったのは 3 レース、残 69 は 7/12 朝 5:00 cron の取込後に stale 化していた)。
    是正後の残 missing 136 件は全て実データ不在の正当な取得漏れ候補
    (2021〜2026-06、直近 14 日は 0 件)。
  - dry-run 確認 (`weekly_drift_report.py --days 7`、メールなし・--out スクラッチ):
    「取得漏れ候補: 0」「直近 14 日の未解決 missing: 0」で誤警告消失。
    gate A の判定基準・凍結基準そのものは不変 (missing の実態が直ったのみ)。
  - 回帰テスト追加: `tests/test_race_completion.py` (取込後 refresh で
    missing_results→complete、days 差分更新の窓外行保持、全期間再構築)。
- **P3: `phase_ac_analysis.py:13` の無効エスケープ (`\k`) を docstring の raw 文字列化で解消**
  (Python 3.12+ の SyntaxWarning。`-W error` でコンパイル確認済)。
- 本番モデル・shadow 予測経路・gate A 基準は不変。スクレイプ/メール送信なし
  (台帳是正は既存 CSV からの再導出のみ)。

## 2026-07-11 (多角監査 P1/P2 対応、Codex 条件付き承認)

- **P1: 週次メール HTML の未定義 `HTML_TD` 参照 3 箇所を修正** (`weekly_drift_report.py`
  959/1004/1011 行 → `HTML_TD_L`。全て文字セル、数値セルは `HTML_TD_R` の既存慣習どおり)。
  7/13(月) 07:30 の週次メール HTML 生成が NameError で死ぬ経路を解消。
  - Codex 条件の残存ゼロテストを追加: `tests/test_weekly_drift_report.py`
    (bare `HTML_TD` のソース検査 + `_html_drift_health` / `_html_gate_a` の
    合成データレンダリング + 実データ dry-run で NameError 消失を確認済)。
- **P2-3: gate A に lag null 率条件を追加** (`evaluate_gate_a`): 秒精度列
  (`capture_lag_sec`) が null の ok 行を黙って素通しする穴に対し、
  **週×offset で null 率 > 50% は「lag 未計測」として fail** を追加。
  分母 = その週×offset の status=ok 行数 (行単位)、ok 行 0 は null 率 100% 扱い。
  事前固定基準 (150/98%/10s/1%/5%) は不変 (緩和ではなく未計測の fail 化)。
  定義は CLAUDE.md「凍結基準」節に固定文書化 (Codex 条件)。
  現況データでは全 offset×週で null 率 0% のため判定への影響なし。
- **P2-4: CLAUDE.md gate A 基準文言を実装に整合**
  (「capture_lag_sec の p95」→「|capture_lag_sec| の p95」)。
- **P2-1: `ingest_day.py` docstring の heal_mode 記述を事実に整合**:
  日次 cron (`ingest_yesterday.py`) は index を渡すため heal は発動しない。
  発動経路は手動 `ingest_day.py` / 手動 `recover_missing_races.py` のみ。
- **P2-2: `daily_shadow_predict.py` の run_type=official に発走時刻ガード追加**:
  無引数でも当日の最早発走時刻 (race_entries の st_time 最小) を過ぎた実行は
  rerun に降格 (事前時点性の主張を守る。predicted_at で事後判別可能)。
- 本番モデル・D: のデータは無変更 (レポート dry-run はスクラッチ領域へ出力)。

## 2026-07-07

- **実ライン (公式並び) を特徴量に本投入 → coef_model 離陸判定 = NO-GO**
  (`build_line_real_features.py` / `eval_line_liftoff.py` 新規)
  - `race_lines.csv` の `narabi_x` (トラック上グローバル位置、欠番=ライン境界) を
    解読し、ライン構成を復元して per-car 実ライン特徴量 13 種を生成
    (line_size/pos/head/tanki/n_lines/... + 脚質交差)。代理ヒューリスティック
    (`build_line_weak_features.py`) の後継。`ml_baseline.py --use-line-real` /
    `weekly_retrain.py` 自動投入で配線。
  - 週次 retrain (品質ゲート有効・force なし): verdict=**OK で 54 特徴量モデル採用**。
    ただし y_win OOS は AUC 0.8074→0.8057 / logloss +0.34% / ECE +0.0009 と横ばい〜微悪化。
    **採用モデルのライン特徴量 gain=0** (5 年学習の 2% 被覆では木が分岐しない)。
  - **coef_model 離陸せず**: 同一 OOS 条件 A/B で coef_model 0.0241→**0.0212** (微減)、
    dlogloss(market→blend) は before/after とも **CI がゼロ跨ぎ**。ブレンド≒市場のまま。
  - 反証テスト: 被覆窓 (2026-04-26〜) 限定学習ではライン gain 4.47%・AUC +0.0063 で
    **特徴量は原理的に有益**。律速は「ラインデータ被覆量」であり、市場は既にライン
    構成を効率的に織込済のため上乗せが出ない。**収益化フェーズには進まない** (離陸が前提)。
  - `market_blend_eval.py` に `--probs` 測定フックを追加 (A/B 用、production 非破壊)。
  - 詳細: `reports/line_liftoff_2026-07-07.md`。

## 2026-07-03

- **市場ブレンド (Benter型) 評価 → 実装中止を判定** (`market_blend_eval.py` 新規)
  - banei/boat で実績のあるモデル×市場のロジスティック結合を競輪に移植する前の
    定量ゲート。競輪は単勝オッズが無いため ST2 (二車単) de-vig の1着 marginal
    (合計=1) を市場勝率として使用。
  - 結果: coef_model ≈ 0 (T-1: +0.014 / T-2: -0.003 / T-5: +0.063)、
    blend vs market の Δlogloss CI は全条件でゼロを跨ぐ →
    **production_no_odds は市場に対する直交情報を持たない。ブレンド ≒ 市場**。
    シャドー配備の価値なしと判定し、中核実装・学習スクリプトは追加しない。
  - 追加発見: 市場勝率のみを Harville に通しても EV≥1 が1.6万点発生 (ROI 68%)
    = **Harville 変換自体が偽 EV を量産** (Phase C の EV 候補汚染の共犯)。
  - 詳細: `reports/market_blend_eval_2026-07-03.md`。モデル改良後は
    `python market_blend_eval.py` の再実行で coef_model の離陸を再判定する。
  - 本番モデル・cron タスク・CSV スキーマ・D: のデータは一切変更なし
    (読み込みのみ、シャドー含め新規経路の追加もなし)。
