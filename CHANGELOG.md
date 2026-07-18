# CHANGELOG

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
