# CHANGELOG

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
