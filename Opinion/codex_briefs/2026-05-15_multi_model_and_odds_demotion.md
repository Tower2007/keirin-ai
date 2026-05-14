# Codex 依頼書: 多モデル化 + odds 降格 + snapshot timing 設計レビュー

依頼日: 2026-05-15
依頼者: ユーザー → Codex (via Claude が起草、Gemini 2 回目レビューを受けて)
出力先: `Opinion/CodexOpinion.md` に追記

---

## 背景

Gemini が 5/15 に 2 回目の大局観レビューを行い (`Opinion/GeminiOpinion.md`)、
姉妹プロジェクトの最新状況を踏まえて以下を強く提案:

1. **「市場 80% 依存モデル」は本番で odds drift により崩壊** (auto の最新事例:
   ROI 132 → 105% に低下、LEAD_MIN を 5→2 分に短縮パッチ済)
2. **多モデル化必須**: W が三連単悪化 = 単一モデルの限界
3. **Codex 第 3 ルート (no_odds_model + odds_for_EV_only) を本番主軸に**
4. RH3/RT3 段階投入は無意味 (drift で消える)
5. `LEAD_MIN` をパラメータ化、5/3/2 分切替可能に

ユーザーは Gemini 提案を全面採用、Phase 6 評価軸を「AUC/ROI 改善」から
「pre-start odds drift 監視 + 観測基盤の堅牢性」に PIVOT した。

Codex に細部設計の妥当性をクロスチェックしてほしい。

---

## Codex への質問 (5 点、ユーザーから明示指定)

### Q1. `no_odds_model + odds_for_EV_only` を本番主軸にする妥当性

5/12 brief で Codex 自身が「3-way 比較」の第 3 ルートとして提案:
- 予測モデルは odds なしで学習
- 券ごとの購入判断だけ odds を使って `model_prob * odds` の EV を計算

これを **本番主軸** (デフォルト) に格上げするのは妥当か?

具体論点:
- odds なし AUC は y_win 0.811、odds あり 0.850 (+0.039)
- odds なしモデルは drift に影響されない強みあり
- ただし「market consensus を学習しない」ため、市場が読めている当然の情報も
  捨てる形になる。これでも EV 計算で十分か?
- バックアップとして odds ありモデルを「upper-bound 参考」として保持する設計でよいか?

### Q2. odds 入りモデルを upper-bound / shadow に降格する判断

Q1 と関連。現在 `--use-odds` で学習している with_odds モデルは、本番では
リーケージで使えない。これを「研究/監査用」に降格する。

- 降格対象: with_odds, with_odds_w, with_odds_line, with_odds_mp
- 残す本線: no_odds (= --use-line のみ or 何もフラグなし)
- ↑これで合っているか? 弱ライン特徴も限定的価値しかない (5/13 検証)、
  本線は何のフラグもない素の baseline で良いか?

### Q3. 順序系/非順序系の多モデル分離

Gemini 提案 (boat の知見ベース):
- **順序系モデル**: ST2 (二車単) / RT3 (三連単) を予測。1-2-3 着の正しい順序を当てる。
  入力に W は入れない (順序判定を破壊するため)。
- **非順序系モデル**: SH2 (二車複) / RH3 (三連複) / W (ワイド) を予測。3 着以内に
  入るかどうか。W も入力に入れて強化。

質問:
- 多モデル分離の具体実装案はどうあるべきか?
  - 単純に 2 つの LightGBM モデル?
  - それとも 1 モデルで「ターゲット別に入力特徴を変える」設計?
- ターゲット定義の変更: 順序系は y_win/y_rank、非順序系は y_top2/y_top3 が自然?
- W で三連単悪化した原因の細部解釈: モデルが y_top2/y_top3 に過剰最適化されて
  y_win 系の判別が劣化した、で合っているか? feature_importance で検証可能?

### Q4. W 追加で三連単悪化の細部解釈

`compare_w_odds.py` の結果:
- 全レース 三連単 ROI: -0.2567 → -0.2697 (-0.013 悪化)
- top10% 三連単 ROI: -0.1997 → -0.2485 (-0.049 悪化、大きい)
- 一方で y_top3 AUC: 0.7889 → 0.7942 (+0.005 改善)

Q4-a: なぜ y_top3 AUC が上がるのに、order-aware の三連単 ROI が悪化するのか?
Q4-b: モデルの decision boundary が 3 着内予測に寄ったために、1-2-3 の確率順位
       (P(win) 大きい順) の質が下がった、という仮説は妥当?
Q4-c: feature_importance で確認できる兆候は? (W 系特徴が y_win/y_top2 でほぼ使われ
       ていなければ仮説と整合)

### Q5. 5 分前 / 3 分前 / 2 分前 snapshot の比較設計

auto が 5→2 分に短縮した経緯がある。keirin でも複数 timing で snapshot を比較
できる仕組みが必要。

質問:
- 現 `ingest_odds_prerace_daemon.py` の `SNAPSHOT_OFFSET_MIN=5` をパラメータ化したい
- 設計案 A: 環境変数 / CLI 引数で 5/3/2 分切替
- 設計案 B: 同時に 3 つ取る (5 分前 + 3 分前 + 2 分前)、saved column で区別
- 設計案 C: cron を 3 つ並列で動かす
- どれが最も妥当?
- 比較は honest backtest 開始後 (n=30 達成後) に行う想定で良いか?

姉妹プロジェクト確認:
- auto は `LEAD_MIN=2` 単一値で運用していそうだが、過去比較のために 5 分前データも
  保持しているのか? それとも 2 分前のみで再構築したか?

---

## 期待アウトプット

`Opinion/CodexOpinion.md` に追記:
1. 上記 Q1-Q5 への賛同/反対 + 理由
2. 多モデル分離の具体実装案 (擬似コード or 設計図、必要に応じて
   `Opinion/multi_model/proposal.md` 等に置く)
3. snapshot timing の推奨設計
4. その他、本 pivot で見落とされている観点

数値根拠は軽め (サンプリングで十分)。Gemini が大局論を出したので、Codex には
**実装の具体性**を期待。

---

## 参考ファイル

- `Opinion/GeminiOpinion.md` 2026-05-15 (今回 pivot のきっかけ)
- `Opinion/ClaudeFeedback.md` 2026-05-15 (Phase 6 pivot 記録)
- `Opinion/CodexOpinion.md` 2026-05-12 (Codex 自身の第 3 ルート提案)
- `ml_baseline.py` 現状の単一モデル設計
- `compare_w_odds.py` / `w_odds_compare_report.md` 三連単悪化の根拠データ
- `ingest_odds_prerace_daemon.py` SNAPSHOT_OFFSET_MIN=5 固定
- 姉妹: `C:\Users\no28a\Claude-project\Auto_racing_AI`, `Boat_racing_AI`
