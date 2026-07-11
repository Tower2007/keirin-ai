# 実ライン本投入 → coef_model 離陸判定 (2026-07-07)

**タスク Ke-1**: 公式並び (`race_lines.csv`) の実データを特徴量に本投入し、
モデルが市場に対して離陸するか (`coef_model` が 0 から有意に正へ動くか) を測定。

## 結論 (先出し)

**離陸せず (NO-GO)。** 実ライン特徴量を本投入しても `coef_model ≈ 0` は動かず、
むしろ微減 (0.0241 → 0.0212)。ブレンド vs 市場の Δlogloss CI は依然ゼロを跨ぐ。
決定的な理由は **ライン被覆が全学習期間の 2.0% しかなく、5 年学習では
LightGBM がライン特徴量で 1 度も分岐しない (gain=0)** こと。
被覆窓だけに絞れば特徴量は使われ AUC は僅かに上がる (0.7810→0.7873) が、
その僅差は市場に対する直交情報にはならなかった (市場はライン構成を効率的に織込済)。

品質ゲートは **OK 判定で新モデル (54 特徴量) を採用**。ただし離陸していないため
**収益化フェーズには進まない** (収益化は coef_model 離陸が前提、ガードレール遵守)。

> **⚠️ 訂正 (2026-07-11 監査 P1-2)**: 上記「54 特徴量を採用」は**同日中に取り消された**。
> 2026-07-07 08:12 に `line_real_features.csv` → `.disabled` 改名、08:48 に `.prev`
> (41 特徴量, 7/6 学習) をコピーバックするロールバックが行われ、54 特徴量モデルは
> 上書き消失した (current/prev の SHA 完全一致で確定)。当時この操作は未記録だった。
> 現在の有効モデルは **41 特徴量 (model_hash 9491d5c9c74f)**。経緯・再実験手順は
> DATA_DIR `weekly_model/MANIFEST.md` を参照。

---

## 1. 並びをどう特徴量投入したか

### 1-1. race_lines.csv のエンコード解読 (調査確定)

列は `race_date, place_code, race_no, car_no, narabi_x, narabi_y`。
- `narabi_y` = ライン束番号 (99.6% が 1、稀に 2/3 = 別枠並び)
- `narabi_x` = **トラック上のグローバル位置 (1..14、欠番で区切り)**

`narabi_x` を昇順に並べたときの **差** がライン境界を表す:
- 差 1 (または 0) → 同一ライン隣接
- 差 ≥2 → ライン境界 (新ライン開始)

例) `11R1`: x=[1,2,3,5,7] car=[1,5,2,3,4] → `[[1,5,2],[3],[4]]`
(3 車ライン + 単騎 + 単騎)。復元後のライン数/人数分布は実競輪と整合
(平均 3.64 ライン/レース、単騎率 17%、平均ライン人数 2.33)。全単騎レースも正しく復元。

これは既存の代理特徴 (`build_line_weak_features.py`: 地区/県ヒューリスティック) と
まったく別物の **公式実データ**。

### 1-2. 生成した実ライン特徴量 (13 種、`build_line_real_features.py` 新規)

- 位置系: `line_size` / `line_pos` / `is_line_head` / `is_line_second` /
  `is_line_third_plus` / `is_tanki` / `pos_ratio`
- 構成系: `n_lines_in_race` / `max_line_size` / `is_in_largest_line` /
  `line_size_rank` / `n_tanki_in_race`
- 脚質×位置交差: `head_nige_propensity` (先頭車の逃げ志向)

出力: `line_real_features.csv` (21,184 行、2026-04-26〜)。

### 1-3. 配管への本投入

- `ml_baseline.py`: `LINE_REAL_NUM_FEATURES` 定義 + `--use-line-real` フラグ +
  `build_dataset` で left merge。**被覆外は NaN のまま** (0 埋め・中央値埋め禁止=
  過去 5 年に「単騎でない」等の誤情報を注入しないため)。LightGBM native 欠損処理に委任。
- `weekly_retrain.py`: `line_real_features.csv` が存在すれば自動で読み込み、
  `feat_cols` に 13 列追加 (41→54)。**品質ゲートは無改変**。
- join 整合性: ライン 2,958 レースは全て stats キーと一致 (欠損 0)。

---

## 2. retrain の品質ゲート結果

`python weekly_retrain.py` を直接実行 (force なし、ゲート有効)。runtime 153.7s。

| 指標 (y_win OOS) | 旧 (41 feat) | 新 (54 feat, 実ライン) | ゲート |
|---|---|---|---|
| AUC | 0.8074 | **0.8057** (−0.0017) | WARN 帯未満 (NG=−0.010) |
| logloss | 0.32509 | **0.32620** (+0.34%) | WARN 帯未満 (WARN=+1%) |
| ECE | 0.0097 | **0.0106** (+0.0009) | WARN 帯未満 (WARN=+0.005) |

**verdict = OK → 新モデル採用。** ただし全指標が旧比 **横ばい〜微悪化**で、
ライン投入による改善は無い (採用は「壊れていない」ことの確認に過ぎない)。

### 決定的診断: 採用モデルのライン特徴量 gain = 0

採用された y_win モデルの feature importance (gain):
- **実ライン 13 特徴量の gain share = 0.00% (全て 0)** — 木が 1 度も分岐しない
- 上位: heikin_tokuten_rank 30.8% / heikin_tokuten_gap 22.0% / syouritu_gap 7.8%

原因は被覆。ライン実データは 2026-04-26〜 のみで、5 年学習の **2.0%** の行にしか
存在しない。`min_data_in_leaf=100` の下では、この希薄さでは有益な分割が作れず、
LightGBM は完全に無視する。

---

## 3. coef_model の before → after (離陸したか)

production の差分再生成は blend 窓のごく一部しか新モデルに置換しないため、
**同一条件 (train_end=2026-05-11、blend 窓全体が完全 OOS) の per-car OOS 確率を
2 系統生成して A/B** (`eval_line_liftoff.py`、production CSV 非破壊)。同一 15,050 行 /
2,119 レース / 市場側同一で比較:

| y_win (T-1) | BEFORE (no-line 41f) | AFTER (line 54f) | 判定 |
|---|---|---|---|
| **coef_model** | **0.0241** | **0.0212** | 離陸せず (微減) |
| coef_market | 1.0252 | 1.0278 | ほぼ不変 |
| model logloss | 0.3324 | 0.3328 | 微悪化 |
| blend logloss | 0.3090 | 0.3090 | = market |
| **dlogloss(market→blend)** | +0.0000, CI[−0.0002,+0.0003] | −0.0000, CI[−0.0003,+0.0003] | **両方 CI 跨ぎ** |

y_top2 (参考) も同様: coef_model 0.0156 → 0.0143 (微減)、market→blend CI 跨ぎ。

**判定基準 (事前固定)**: coef_model が 0 から有意に正 かつ dlogloss(market→blend) の
95% CI 下限 > 0 なら離陸。**→ どちらも満たさず。離陸せず。**

---

## 4. なぜ離陸しないか (根本原因)

1. **被覆律速 (主因)**: ライン実データ 11 週分 = 全学習の 2%。5 年学習では
   希薄すぎて木が使わない (gain=0)。→ **これは特徴量の質ではなくデータ量の問題。**
2. **被覆窓だけなら特徴量は効く (反証テスト)**: 2026-04-26〜 に学習を絞ると
   ライン gain share 4.47%、OOS AUC 0.7810→0.7873 (+0.0063)、logloss 0.3613→0.3587。
   → **実ライン特徴量は原理的には有益。**寄与上位は head_nige_propensity /
   line_size / pos_ratio。
3. **しかし市場を超えない**: その僅かな AUC 改善も市場に対する直交情報にならない。
   ライン構成は発走前に公開される公知情報で、市場が既に効率的に織り込んでいる
   (coef_market ≈ 1.03、市場ほぼ完全較正)。モデルが市場に足せる上乗せはほぼ無い。

---

## 5. 含意と次アクション

- **収益化フェーズには進まない** (ガードレール: 離陸が前提)。coef_model≈0 の
  2026-07-03 判定は実ライン投入後も維持される。
- 配管は残置 (`--use-line-real` / weekly_retrain 自動投入)。ライン被覆が蓄積して
  学習比率が上がれば (数ヶ月〜) gain が乗り始める可能性がある。**被覆律速なので、
  次の律速は「ラインデータの継続収集」**であり追加設計は不要。
- 再判定は `python eval_line_liftoff.py <out>` → `market_blend_eval.py --probs` で
  いつでも同一プロトコルで再測定できる (本レポートの A/B がそのまま回帰材料)。
- 参考: 被覆窓限定モデルは AUC で勝つが、被覆外を NaN 依存にすると全期間モデルが
  勝つ、というトレードオフ。全期間本番モデルにラインを効かせるには被覆蓄積待ちが唯一。

---

## 生成/変更物

- 新規: `build_line_real_features.py`, `eval_line_liftoff.py`, 本レポート
- 変更: `ml_baseline.py` (+`--use-line-real`), `weekly_retrain.py` (自動投入),
  `market_blend_eval.py` (+`--probs` 測定フック)
- D: データ書込: `line_real_features.csv` (新規、読み取り元 race_lines.csv は非改変)、
  週次採用に伴う `weekly_model/*` 更新 + `ml_pred_probs.csv` 差分 (通常の週次と同じ)
