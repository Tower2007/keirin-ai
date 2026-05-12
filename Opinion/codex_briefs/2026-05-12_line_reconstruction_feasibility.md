# Codex 依頼書: 過去ラインデータの逆引きバックフィル feasibility 調査

依頼日: 2026-05-12 (2)
依頼者: ユーザー → Codex (via Claude が起草、Gemini 提案を受けて)
出力先: `Opinion/CodexOpinion.md` に追記、必要なら `Opinion/line_reconstruction/` に検証スクリプト

---

## 背景

`Opinion/GeminiOpinion.md` (2026-05-12) で Gemini が提案:

> 過去 5 年分の「公式ライン」が存在しない。蓄積を待つだけでは学習データが圧倒的に不足する。
> **結果ページ等から「実際に形成されたライン」を逆引きしてバックフィル**する調査を優先すべき。

keirin の `PJ0305 nInfo` (公式ライン) はレース完了後に空になるため、過去 5 年分の公式
ライン情報は取得不可能。しかし `race_results.csv` には決まり手・着順・ライン内位置等が
記録されており、ここから「実際に形成されたライン」を逆引きできないか検証したい。

逆引きが実用精度で可能なら、過去 5 年分のレースにラインを後付けでき、
ML 特徴量として即利用可能 → Phase 6 のスケジュール短縮 + ライン構造を本格的に活用できる。

---

## 利用可能データ

### race_results.csv (約 60 万行、2021-04 ~ 2026-04)
スキーマ (`src/storage.py:88-95`):
- `race_date`, `place_code`, `race_no`, `tyaku` (着順), `car_no`
- `player_code`, `player_name`, `age`, `prefecture`
- `sotugyouki`, `kyuhan`
- `tyakusa` (着差), `agari` (上がり時間)
- **`kimarite`** (決まり手: 逃げ/まくり/差し/マーク等)
- `bh` (?)
- **`in_line_jyuni`** (レース内位置 / ライン内順位?)
- `kojin_state`, `tyaku_note`
- `tenki`, `husoku`

### race_entries.csv
- **`kyaku`** (脚質: 逃/追/両)
- **`prefecture`** (出身地、地区連携の手がかり)
- `line_type`, `narabi_flg`, `ozz_flg`

### race_lines.csv (検証用ゴールド標準)
- `narabi_x`, `narabi_y` (公式ライン座標)
- ただし**直近 1 ヶ月のみ取れている** (`backfill_odds_done.txt` 期間外)
- 件数は数百レースレベル → validation set として使える

### race_stats.csv
- 戦法回数 (nige_cnt / makuri_cnt / sasi_cnt / mark_cnt / back_cnt)
- 選手の脚質傾向の補強情報

---

## 推定方法の候補 (Codex に評価依頼)

以下のシグナルからライン構造を推定する案。Codex の知見と data 探索で feasibility を見てほしい。

### A. `in_line_jyuni` 直接利用案
- 仮説: `in_line_jyuni` がライン内番手 (1 = 先頭、2 = 番手、3 = 三番手) を直接表す
- データ確認で意味が明確なら、これだけで大筋復元できる可能性あり
- 質問: そもそも `in_line_jyuni` の定義は何か? どう記録されている?

### B. ヒューリスティック復元案
複数シグナルを組み合わせてグルーピング:
- 着順順 + `in_line_jyuni` でレース内位置を辿る
- `kyaku` (逃) を先頭候補に
- 同 `prefecture` を連携候補に
- `kimarite` で流れの当否確認 (例: 逃げ決まりなら 1 番先頭が確定的)
- `tyakusa` (着差) で連の有無 (僅差なら同ライン示唆)
- `agari` (上がり時間) の近さも参考に

### C. 補助シグナル
- `race_stats.csv` の戦法回数で「逃げタイプ」「マークタイプ」を判別
- `payouts.csv` の二車複的中組合せ (同ライン内なら多くは的中する傾向?)

---

## Codex への質問

### Q1. `in_line_jyuni` の定義
- 実データを sampling して `in_line_jyuni` の値分布、ライン公式データとの整合性を確認
- 直近 1 ヶ月の `race_lines.csv` (`narabi_x`, `narabi_y`) と `race_results.csv` の
  `in_line_jyuni` を突合して、両者の関係性を解明
- これが「ライン内番手」を表すなら、過去ライン復元は劇的に簡単

### Q2. 逆引き feasibility
- 上記推定方法 A/B/C のどれが現実的か
- `race_lines.csv` (直近 1 ヶ月) を validation set として、過去ラインを推定するアプローチで
  精度 (recall / precision / 完全復元率) を測れるか
- 想定する精度の閾値: 「ML 特徴量として実用するには ≥ X% 必要」を提示してほしい

### Q3. 推定不能・部分復元のケース
- どんなケースで復元失敗するか (例: 単騎、決まり手が「不明」のレース、ライン崩れ)
- 部分復元しかできない場合、それでも ML 特徴量として価値があるか
  (例: `is_line_leader` フラグだけでも十分か、フルライン情報必要か)

### Q4. 実装の負荷見積もり
- もし実用精度が出る場合、`build_lines_backfill.py` の実装複雑度はどの程度?
- 過去 5 年分の処理時間想定 (race_results.csv 60 万行)

### Q5. 失敗時の代替案
- 逆引き精度が出なかった場合、代替案は?
  - ライン情報なしで Phase 6 を進める (Gemini draft の二層モデル案)
  - 公式ライン蓄積を本格的に待つ (Phase 6c を 6 月下旬以降に)

---

## 期待アウトプット

`Opinion/CodexOpinion.md` に追記:
1. Q1 の `in_line_jyuni` 実データ探索結果 (sampling で十分)
2. Q2 の feasibility 判定 (実用精度の見込み)
3. Q3 の失敗ケース分析
4. Q4 の実装見積もり (概算で良い)
5. Q5 の代替案コメント

必要なら `Opinion/line_reconstruction/` に簡易検証スクリプトや探索結果を置いてほしい。
重い計算は避け、サンプリングで足りるレベルでお願いします。

---

## 参考リンク

- `Opinion/GeminiOpinion.md` 2026-05-12 (ライン逆引き提案の出所)
- `Opinion/CodexOpinion.md` 2026-05-12 (前回 Codex 回答、特徴量設計レビュー)
- `Opinion/ClaudeFeedback.md` 2026-05-12 (2) (Phase 6 計画統合)
- `src/storage.py:88-95` (race_results.csv スキーマ)
- `D:\keirin-ai-data\race_results.csv` (60 万行データ本体)
- `D:\keirin-ai-data\race_lines.csv` (validation set、直近 1 ヶ月)
