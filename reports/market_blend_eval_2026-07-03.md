# 市場ブレンド (Benter型結合) オフライン評価 — 実装中止の判定 (2026-07-03)

## 背景と問い

Phase C (reports/phase_ac_2026-06-10.md) の敗因は「EV選別が拾うのは市場の歪みでなく
モデルの誤差」。banei/boat で実績のある Benter 型ブレンド
(blend_logit = b0 + b1·logit(model_p) + b2·logit(market_p)) はこの病気の直接治療候補。
boat と同じ規律で「実装する前に測る」を実施した (market_blend_eval.py)。

問い: **production_no_odds は直前市場に対して直交情報 (coef_model > 0) を持つか。**

## データ実態

| 項目 | 内容 |
|---|---|
| モデル確率 | ml_pred_probs.csv (production_no_odds の per-car OOS 予測)。5/16〜6/5 は 2025-01-01 分割 baseline、6/6〜6/28 は weekly_retrain の差分生成 (in-sample 汚染なし)。live 発火記録は無いがこれが最も live に近い記録 |
| 市場確率 | race_odds_prerace.csv。**競輪に単勝オッズは無い**ため ST2 (二車単) de-vig の1着 marginal (レース内合計=1) を市場勝率とする。y_top2 は SH2 marginal (合計=2、boat 複勝方式) |
| 結果 | race_results.csv tyaku (1着1人のクリーンなレースのみ) |
| 突合 n | T-1: 11,102行/1,565レース (6/6〜6/28、T-1収集は6/6開始)。T-2/T-5: 18,255行/2,573レース (5/16〜6/28) |
| モデルの主目的 | y_win。本番配管 (ev_prerace_pipeline.py) が p_win → per-race 正規化 q → Harville → EV の順で使うため |

## 結果: walk-forward k=5 OOS logloss + 全期間ブレンド係数

### y_win (model = 正規化 q_win, market = ST2 marginal)

| offset | n | model | market | blend | coef_model | coef_market |
|---|---|---|---|---|---|---|
| T-1 | 11,102 | 0.3369 | 0.3138 | 0.3144 | **+0.014** | 1.012 |
| T-2 | 18,255 | 0.3307 | 0.3069 | 0.3073 | **-0.003** | 1.047 |
| T-5 | 18,249 | 0.3306 | 0.3098 | 0.3098 | **+0.063** | 1.017 |

### y_top2 (model = p_top2, market = SH2 marginal) [参考]

| offset | n | model | market | blend | coef_model | coef_market |
|---|---|---|---|---|---|---|
| T-1 | 11,096 | 0.4945 | 0.4562 | 0.4563 | +0.022 | 1.053 |
| T-2 | 18,243 | 0.4926 | 0.4533 | 0.4539 | +0.006 | 1.056 |
| T-5 | 18,243 | 0.4925 | 0.4580 | 0.4580 | +0.055 | 1.042 |

### Δlogloss のレースクラスタ bootstrap 95% CI (T-1, y_win)

- model → blend: **+0.0226 [+0.0167, +0.0282] 有意** (ブレンドはモデル単独に大勝)
- market → blend: **-0.0005 [-0.0012, +0.0000] CI跨ぎ** (ブレンドは市場単独に勝てない。
  点推定はむしろ僅かに劣後)
- T-2 / T-5、y_top2 でも全て同じパターン (market→blend の CI が全てゼロを跨ぐ)

## 判定: 実装中止 (事前に定めたゲートに該当)

ゲート「coef_market≈0 か Δlogloss CI がゼロを跨ぐなら価値なし」——
**coef_model ≈ 0 (-0.003〜+0.063) かつ market→blend の Δlogloss CI が全条件で
ゼロを跨ぐ**。ブレンドがモデル単独に有意勝ちするのは「ブレンド ≒ 市場そのもの」
だからであり (coef_market≈1.0)、モデルは市場に対する直交情報を一切持たない。
logit 相関 0.88 と高いが、市場を条件付けた瞬間にモデルの残差情報はゼロになる。

boat で blend が効いた前提 (朝モデルが市場と直交する情報を持つ) が競輪では
成立しない。Phase C 追補の診断「モデルは市場に情報劣位」をさらに強い形で確定:
**劣位どころか、市場に含まれない情報が (現行特徴量では) 無い。**

blend を導入しても出力は市場確率に一致し、EV ≈ 1/overround ≈ 0.75 で
全組が EV<1 → 「賭けない」と等価。シャドー配備する意味が無いため中核実装・
学習スクリプト・タスクは追加しない (それが正しい結論)。

## 追加の重要発見: Harville 変換自体が偽の EV>1 を量産する

OOF 確率で Harville を引き直した実払戻 ROI シミュ (T-1, 100円均等):

| 確率ソース | ST2 EV≥1.0 | ROI | SH2 EV≥1.0 | ROI |
|---|---|---|---|---|
| model q_win | n=20,363 | 65.9% | n=9,153 | 68.0% |
| blend (≒市場) | n=14,913 | 65.6% | n=6,592 | 71.8% |
| **市場 marginal のみ** | **n=16,723** | **68.4%** | **n=7,331** | **71.1%** |

市場勝率そのものを Harville に通しても 1.6 万点の「EV≥1」が発生し ROI≈68%。
市場由来の combo EV は定義上 1/overround≈0.75 を超えられないので、これらは
**全て Harville 近似の歪みが製造した偽シグナル**。つまり Phase C の EV 候補の
相当部分はモデル誤差ですらなく組合せ変換誤差であり、車単位の確率をいくら
改善しても Harville を経由する限り EV 選別は汚染される。

## 次のアクション

1. **特徴量強化が引き続き唯一の道** (ライン構造・バンク特性・直近フォーム)。
   モデル改良後に `python market_blend_eval.py` を再実行し、coef_model が
   ゼロから離れるかで「市場に無い情報を獲得したか」を直接測れる
   (AUC 改善より鋭い判定器として使える)。
2. モデル改良と**並行して組合せ確率の構成法を見直す** (Harville → PL-MC
   サンプリング、または combo 直接モデル)。車単位が完璧でも現行変換では
   EV 選別が偽陽性を量産することが市場-only シミュで実証された。
3. 再測定で coef_model が有意に正へ離れたら、その時点で banei/boat の型を
   移植 (中核+学習スクリプト+シャドー列) — 型は枯れており移植は 1 日仕事。

## 再現方法

```
python market_blend_eval.py --self-test   # 合成データ 5 項目
python market_blend_eval.py               # T-1 (本番推奨 offset)
python market_blend_eval.py --offset 2    # T-2
python market_blend_eval.py --offset 5    # T-5
```
読み込みのみ (D: のデータ・本番モデル・タスクは不変)。seed 固定で再現可。
