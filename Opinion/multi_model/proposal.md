# 多モデル化 + odds 降格 + snapshot timing 設計案

作成日: 2026-05-15

## 基本方針

本番主軸は `no_odds_model + odds_for_EV_only`。

- 予測モデル: 発走前に安定して取得できる非 odds 特徴だけで学習
- EV 判定: live snapshot odds を lookup して `p_model * odds` で判定
- odds 入りモデル: `final_odds_upper_bound` / `shadow` / `diagnostic` に降格

理由:

- `race_odds.csv` は post-start/final-like で upper-bound。
- final odds を特徴に入れたモデルは、実運用で `live odds` に置換した瞬間に分布が変わる。
- odds を EV 計算だけに使う形なら、drift の影響範囲を「購入判定」に閉じ込められる。

## モデル構成

### v1: 低リスク最小変更

現 `ml_baseline.py` の 4 ターゲット構成を保ち、feature set だけ分ける。

```text
production_no_odds:
  targets: y_win, y_top2, y_top3, y_rank
  features: FEAT_NUM + FEAT_CAT + optional weak_line
  use_odds: false

shadow_with_final_odds:
  targets: y_win, y_top2, y_top3, y_rank
  features: production features + ST2/SH2/W odds features
  use_odds: true
  purpose: upper-bound / market distillation monitor
```

pick 生成:

```python
ordered:
    first = argmax(p_win)
    second = best p_top2 excluding first
    third = best p_top3 or p_rank excluding first/second

unordered:
    top2 = top2 by p_top2
    top3 = top3 by p_top3
```

### v2: 順序系/非順序系の分離

`train_all_models()` を「ターゲットごとの特徴リスト」に対応させる。

```python
MODEL_SPECS = {
    "order": {
        "targets": ["y_win", "y_rank"],
        "features": BASE + ORDER_FEATURES,
        "odds_features": [],  # production では空
        "shadow_odds_features": ["imp_winprob_st2", "pop_rank_st2_1st", "odds_min_st2_1st"],
    },
    "place": {
        "targets": ["y_top2", "y_top3"],
        "features": BASE + PLACE_FEATURES,
        "odds_features": [],  # production では空
        "shadow_odds_features": ["imp_top2prob_sh2", "imp_top3prob_w"],
    },
}
```

推奨:

- production は v1 から開始。
- v2 は interface だけ先に設計し、live snapshot n>=50 までは shadow 実験扱い。
- ST2/RT3 用に W を入れない、SH2/RH3/W 用には W を入れてよい、という feature set 分離は v2 で実施。

## EV lookup 設計

`pred_p` と `live odds` を分けて保存する。

```python
def expected_return(ticket, preds, live_odds):
    p = ticket_probability(ticket, preds)
    odds = lookup_live_odds(ticket, live_odds)
    return p * odds
```

券種ごとの近似:

- SH2: `P(a top2) * P(b top2)` の独立近似では粗い。まずは pick 生成後の confidence を calibration bin で EV 化する。
- ST2: `P(first win) * P(second top2 | not first)` の近似から開始。
- RH3/W: `top3` モデルの confidence bin を使う。
- RT3: 初期 production では非推奨。shadow のみ。

重要:

- 最初から組合せ確率を厳密化しすぎない。
- live 実績が貯まるまでは「confidence bin の過去平均払戻」と「live odds EV」を並走比較する。

## snapshot timing 設計

現状の `--lead-min` / `KEIRIN_LEAD_MIN` 単一値切替は運用には十分。
比較実験には不足。

推奨は B' 案:

- daemon は通常 1 プロセス
- `--lead-mins 5,3,2` を受け取り、同一レースに複数 snapshot をスケジュール
- 保存列に `target_offset_min` を追加
- 既取得判定キーを `(race_date, place_code, race_no, target_offset_min)` に変更
- `minutes_before_start` は実測値として残す

追加スキーマ:

```text
race_odds_prerace.csv:
  ... existing columns ...
  target_offset_min

prerace_snapshot_log.csv:
  ... existing columns ...
  target_offset_min
```

比較指標:

```text
by target_offset_min:
  n_ok
  n_no_odds
  actual_minutes_before mean/std
  live_ev_mean
  final_ev_mean
  drift_mean = final_ev - live_ev
  threshold_cross_down_rate
  threshold_cross_up_rate
  selected_roi
```

運用案:

- 蓄積期: 5/3/2 を同時取得。ただしリクエスト量が重い日は 5/2 の 2 点。
- 本番候補: 最初は 3 分を default、drift と投票余裕で 2/5 に寄せる。
- n=30: drift 分布を見る。
- n=50: 暫定 freeze。

## W 追加で三連単が悪化した解釈

W は「3着内 marginal」を強くするが、順序情報を持たない。

現 `make_picks()` では RT3 が `p_win` 上位3台の順序で作られる。W が `y_top3` や `y_rank` を改善しても、RT3 に必要な `1着-2着-3着の条件付き順序` は改善しない。さらに W によって `y_rank` / `y_top3` 側の学習が「3着内に堅い車」を厚く評価すると、穴の1着や番手差しの順序が鈍る可能性がある。

確認方法:

- `with_odds_w` で target 別 feature importance を出す。
- W 系特徴の gain が `y_top3/y_rank` に集中し、`y_win` で低ければ仮説と整合。
- RT3 的中レースで、1着車の `p_win` rank が W 追加前後で悪化していないかを見る。

## 実装優先順位

1. `no_odds_model + odds_for_EV_only` の production 名称を固定。
2. `with_odds*` を shadow/upper-bound に rename し、レポート見出しにも明記。
3. snapshot log に `target_offset_min` を追加する設計だけ先に入れる。
4. drift report skeleton を作る。
5. 多モデル v2 は設計だけ置き、live n>=50 まで実装を急がない。
