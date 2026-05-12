# Codex 依頼書: Phase 6 オッズ特徴量設計レビュー

依頼日: 2026-05-12
依頼者: ユーザー → Codex (via Claude が起草)
出力先: `Opinion/CodexOpinion.md` に追記、必要なら `Opinion/odds_features/` に検討用ファイル

---

## 背景

keirin-ai は 2026-05-12 に Phase 3-4 (5 年分のオッズ + 結果データ取得) が完了し、
Phase 6 (LightGBM での予想検証) に入った。ML ベースラインは既に
`ml_baseline.py` に実装済 (AUC 計測 + 5 券種バックテスト) だが、**オッズ特徴量は
未活用**。Phase 6 の最初の改善ステップとして、オッズから per-(race, car) 特徴量を
抽出して ML に統合したい。

利用可能データ:
- `race_odds.csv`: 約 4,790 万行 (2021-04 ~ 2026-04、13,285 venue-day、約 5 万レース)
- カラム: `race_date, place_code, race_no, bet_type, kumi_ban, odds, ?, popularity, official_dt`
  - 例: `2026-03-25,44,1,SH2,1=3,11.3,,5,2026-03-25 11:06:00`
- bet_type: `SH2` (二車複), `ST2` (二車単), `RH3` (三連複), `RT3` (三連単), `W` (ワイド),
  `WH2` (枠複・多くは未発売), `WT2` (枠単・多くは未発売)
- official_dt: オッズの取得時刻 (発走前のスナップショット)

ML 側のデータ単位:
- `ml_baseline.py:155` の `build_dataset()` で **車番単位 (race × car_no) DataFrame**
  を構築している
- 既存特徴量は `race_stats` (戦績) + `race_entries` (脚質・予想印) + `race_meta` (グレード)
- 4 ターゲット: y_win / y_top2 / y_top3 / y_rank

---

## Claude の現時点の設計案 (議論の出発点)

新規 `build_odds_features.py` を作り、`race_odds.csv` から per-(race, car) 集約 CSV を
事前に生成 (出力例: `D:\keirin-ai-data\odds_features.csv`)。ML 側はそれを merge するだけ。

| 特徴量 | 計算方法 | 意図 |
|---|---|---|
| `imp_winprob_st2` | Σ(1/odds) for ST2 with car in 1st pos、レース内で正規化 | 二車単から導く勝率 |
| `imp_winprob_sh2` | Σ(1/odds) for SH2 containing car、レース内で正規化 | 二車複 marginal 勝率 |
| `pop_rank_st2_1st` | ST2 1着付け popularity の min rank | 1着候補としての人気順 |
| `pop_rank_sh2_min` | SH2 含まれる組合せの min popularity | 連対候補としての人気順 |
| `odds_min_st2_1st` | ST2 1着付け odds の min | 最人気 odds |
| `odds_count_st2_1st` | ST2 1着付け候補数 | 売り出し券種数 (キャンセル車検出用) |

`official_dt` は最も新しいスナップショットを使う想定 (確定オッズ近似)。
複数スナップショットを取っているデータがあるかは要確認。

---

## Codex への質問 (3 点)

### Q1. 特徴量の選定は妥当か？
- 上記 6 候補で抜けや過剰がないか
- 暗黙の前提 (例: 「ST2 が SH2 より情報量多い」「popularity が odds より頑健」等) は妥当か
- W (ワイド) は kumi_ban に min/max 形式の独自カラム配置がある (`W` 列は別実装)。
  ML 特徴量として組み込む価値があるか？
- 三連系 (RH3/RT3) は組合せ爆発で per-car 集約が雑になる懸念がある。使う / 使わない の判断材料は？

### Q2. リーケージリスク
- `race_odds.csv` の `official_dt` はレース発走前 (事前オッズ) のはずだが、
  完全に発走前確定か要確認 (走った後にもデータが更新されてないか)
- 確定オッズと事前オッズの混在を疑う必要があるか
- もしリーケージ疑いがあれば、どう切り分けるか (例: `official_dt` フィルタ)

### Q3. 既存ベースラインへの統合手順
- `ml_baseline.py:155 build_dataset()` への merge 設計は妥当か
- AUC / EV 比較は odds なし / あり の 2 モデル走らせる方針で良いか
- 「odds なし版モデル + odds は EV 計算時のみ使用」のような分離設計も検討すべきか
  (本番運用で odds 未取得時の fallback を考慮)

---

## 期待アウトプット

`Opinion/CodexOpinion.md` に追記:
1. 上記 3 質問への回答 (賛同 / 反対 / 別案)
2. 追加で気付いた論点 (例: backtest 設計の落とし穴、過去オッズの欠損パターン)
3. 必要なら `Opinion/odds_features/` 配下に簡易分析スクリプトや擬似コードを置く

数値根拠 (`race_odds.csv` の探索結果等) を添えてもらえると助かる。
ただし重い計算は避け、サンプリングで足りるレベルでお願いします。

---

## 参考リンク

- `CLAUDE.md`: プロジェクト全体
- `AGENTS.md`: Codex の運用ルール
- `ml_baseline.py`: 既存 ML ベースライン (約 500 行)
- `ingest_odds.py`: race_odds.csv の書き込みロジック (schema 確認用)
- `src/netkeirin_parser.py`: オッズ生データの parse (もし schema 不明なら参照)
