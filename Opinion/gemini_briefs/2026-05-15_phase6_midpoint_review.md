# Gemini 依頼書: Phase 6 中盤レビュー (5/13〜5/14 の試行結果 + 蓄積待ち期間の戦略)

依頼日: 2026-05-15
依頼者: ユーザー → Gemini (via Claude が起草)
出力先: `Opinion/GeminiOpinion.md` に追記

---

## 背景

5/12 (月) に Gemini に Phase 6 方針の大局観レビューを依頼し、以下の指摘を受けた:
- **観測基盤がモデルより先**
- auto の `LEAD_MIN=5` 分前 snapshot を採用すべき
- boat の live ↔ final drift 監視も移植
- 競輪は「ラインの読み合い」が構造、特徴量ではない
- shadow 運用 + n=30〜50 gate (boat 流)

これを受けて Claude が 5/12〜5/14 で実装・実験を進めた。その中間結果を踏まえて、
**蓄積待ち期間 (2-4 週間) の戦略**について再度大局観レビューを依頼したい。

---

## 5/12〜5/14 の成果サマリ

### 観測基盤 (Codex/Gemini 提案を受けて実装)
- `ingest_odds_prerace_daemon.py`: 8:00 cron で pythonw 起動、各レース 5 分前に snapshot
- `snapshot_dt` と `official_dt` を分離記録
- `prerace_snapshot_log.csv` で試行ログ管理
- 5/13 は開催なし、5/14 から本格稼働予定

### Phase 6 特徴量設計 (ablation で試行)

| 試行 | 結果 | 評価 |
|---|---|---|
| **ST2 + SH2 odds (第 1 段)** | y_win AUC +0.039 / y_top2 +0.046 | ✅ 採用 |
| **弱ライン特徴量** | AUC ほぼゼロ、top5% 二車単 +0.064 のみ | △ 補助的 |
| **Mispricing (rank diff)** | AUC ゼロ、三連単で逆悪化 | ❌ 採用見送り |
| **W odds (第 2 段)** | y_top3 AUC +0.005、三連複/ワイド ROI +0.02-0.03 | ✅ 採用 |

### Feature Importance 衝撃発見
- モデルは **80%+ 市場依存**
- y_win の 81.8% は `imp_winprob_st2` で説明
- 弱ライン特徴は total 1% 未満 (`nige_propensity` のみ生存)
- 既存 `kyaku`/`prefecture` カテゴリ特徴が地区+脚質情報を吸収済み

### 最良の上限性能 (race_odds.csv ベース、リーケージあり)
- **ワイド top 5% 回収率 90.43%** (損益分岐 100% に -10 pt)
- 三連複 top 5% 回収率 84.48%
- 三連単 top 5% 回収率 75.67% (W 追加で悪化)

---

## Gemini への質問 (大局観チェック)

### Q1. 「市場の蒸留モデル」状態は本番運用上どう評価する?
モデルが市場オッズに 80% 依存しているのは、auto/boat でも同じ状態か?
- 市場依存型は「市場の歪み」を見つけにくい構造になる
- 本番 pre-start odds になると上限性能が下がる前提だが、どの程度の縮退を想定すべきか
- 「odds なしモデル + EV 計算のみ odds」(no_odds_model + odds_for_EV_only) を本線にする選択肢は?
  - これは Codex が初回 brief で提案した第 3 ルート

### Q2. W 追加で三連単悪化の trade-off をどう扱う?
W (ワイド) odds を追加した結果:
- y_top3 AUC +0.005 / 三連複 ROI top5% +0.033 / ワイド +0.023 (改善)
- y_top2 ほぼ動かず
- **三連単 ROI top10% -0.049 (悪化)**

考えられる理由:
- モデルが「3 着内予測」に最適化された結果、「1-2-3 順序」判定が劣化
- 順序系券種 (二車単/三連単) と無順序系 (二車複/三連複/ワイド) で別モデルにすべき?

Q2-a: 単一モデルで全券種を予測する設計は keirin で妥当か?
Q2-b: 三連単戦略には W を使わない multi-model 構成 が現実的か?
Q2-c: auto/boat はこの trade-off にどう対処している?

### Q3. 蓄積待ち期間 (2-4 週間) に何をすべきか
観測基盤は動き始めた。snapshot 蓄積完了まで時間がある。
この期間に何を優先すべきか?

候補:
- (a) RH3/RT3 odds 段階投入 (Codex 提案の第 3-4 段)
- (b) EV 計算 + shadow 運用ロジック実装 (本番投入準備)
- (c) weekly drift 監視レポート実装 (boat 流)
- (d) walk-forward 評価への移行 (現 train/test split → 月次 OOF)
- (e) 多モデル化 (順序系 / 無順序系 別モデル) の試行
- (f) 蓄積されるまでは攻めの実装は止めて、ドキュメント整理に専念

優先順位を大局観で提示してほしい。

### Q4. 「研究フェーズ → 運用フェーズ」の移行判断基準
ユーザーは「本番運用前提」と言っている。研究を打ち切って本番準備に移るべきタイミングは?
- 蓄積が n=30 達成した時点で shadow 運用開始 (boat 流)?
- 蓄積完了 (n=50) で live 投票判断?
- それとも live n=50 → 観察 30 日 → live 判断、の追加 buffer が要る?

### Q5. 姉妹プロジェクト (auto/boat) の最新状況と整合性
auto/boat 側でこの 1-2 週間に Phase 6 相当の進展はあったか?
keirin の方針が auto/boat と乖離している点があれば指摘してほしい。

---

## 期待アウトプット

`Opinion/GeminiOpinion.md` に追記:
- 上記 5 点に対する大局観コメント
- 特に「市場依存 80% モデル」の本番リスク評価
- 三連単悪化 trade-off への大局的見解
- 蓄積待ち期間の優先順位
- 姉妹プロジェクトとの整合性

数値根拠は不要、視点と論理が欲しい。

---

## 参考ファイル

- `Opinion/ClaudeFeedback.md`: 5/12〜5/14 の試行記録
- `Opinion/CodexOpinion.md`: 5/12 Codex 回答 (リーケージ発見、ライン feasibility 調査)
- `Opinion/GeminiOpinion.md`: 5/12 Gemini 過去回答 (auto/boat 整合性確認)
- `D:\keirin-ai-data\*.md`: 各種比較レポート
  - `compare_4way_report.md` (base/+line/+odds/+odds+line)
  - `w_odds_compare_report.md` (W 追加効果)
  - `mispricing_compare_report.md` (失敗した N3)
  - `feature_importance_report.md` (importance 分析)
- 姉妹: `C:\Users\no28a\Claude-project\Auto_racing_AI`, `Boat_racing_AI`
