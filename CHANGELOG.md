# CHANGELOG

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
