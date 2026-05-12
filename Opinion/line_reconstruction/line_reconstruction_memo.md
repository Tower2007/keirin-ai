# 過去ライン逆引き feasibility 調査メモ

調査日: 2026-05-12

## 入力と方法

- `D:\keirin-ai-data\race_results.csv`
- `D:\keirin-ai-data\race_entries.csv`
- `D:\keirin-ai-data\race_lines.csv`
- 検証スクリプト: `Opinion/line_reconstruction/analyze_line_reconstruction.py`

依存追加なし、標準ライブラリのみ。`race_lines.csv` が 46 レース / 330 行と小さいため、これを validation set として `race_results.csv` / `race_entries.csv` から同一レースだけ抽出した。

## 主な結果

```text
validation_races=46
validation_rows=330
narabi_y_counts=1:330
line_type_counts=三分戦:15, ライン無し:12, 二分戦:7, 四分戦:7, コマ切れ:5

in_line_jyuni_counts_top=(blank):965889, 3:541, 2:539, 6:506, 4:480, 5:474, 1:439, 7:421, 8:53, 9:23
in_line_nonblank_profile_top=tyaku=blank:3476, state=失格:3476, note=斜行:798, note=内側追い抜き:738, note=押上げ:580, note=内圏線踏み切り:501, note=押圧:431, note=外帯線内進入:190

x_gap_gold_exact_from_naive_pred=2/46
same_line_pair_precision=0.482
same_line_pair_recall=0.538
races_with_any_pair_hit=32/46
```

## 解釈

- `in_line_jyuni` はライン内番手ではない可能性が高い。
- 非空 3,476 行はすべて `tyaku` 空、`kojin_state=失格`。違反理由は斜行、内側追い抜き、押上げ、内圏線踏み切り等。
- したがって `in_line_jyuni` は通常完走者のライン位置ではなく、失格判定時の「走行位置/違反発生時の内線順位」系の補助情報に見える。
- `race_lines.csv` の `narabi_y` は検証範囲ではすべて 1。parser コメントの「ライン番号」としては使えない。
- `narabi_x` は横座標で、x のギャップ > 1 が表示上のライン区切りを表しているように見える。

## 簡易推定の精度

検証用の簡易推定は、`in_line_jyuni` が使えない場合に `prefecture` の地区 + `kyaku` の逃/両先頭候補だけでグルーピングしたもの。

- 完全一致: 2/46
- same-line pair precision: 0.482
- same-line pair recall: 0.538

これは「雑な下限」ではあるが、過去結果/出走表だけで公式ラインを高精度に復元できる見込みは薄い。ML 特徴量として使うなら、完全なライン復元ではなく、信頼度付きの部分特徴に落とすべき。

## 例

```text
2026-04-26 pc=11 R8 exact=True
gold=[[3, 1, 6], [2, 5], [4, 7]]
pred=[[4, 7], [2, 5], [3, 1, 6]]

2026-04-26 pc=11 R6 exact=False
gold=[[1, 5, 4, 6], [7, 2], [3]]
pred=[[1, 5], [2], [6], [4], [3], [7]]
```

## Feasibility 判定

現時点では「過去5年分の公式ライン完全復元」は非推奨。

実用にできそうな範囲:

- `line_type` がライン無しのレース検出
- 地区/県 + 脚質からの粗い同ライン候補
- 先頭候補 (`kyaku=逃/両`, `back_cnt`, `nige_cnt`) の推定
- 「単騎っぽさ」「同地区後位っぽさ」の確率特徴

公式ライン同等の `line_id` / `line_pos` を過去全量へ付与する用途には、現シグナルだけでは不足。
