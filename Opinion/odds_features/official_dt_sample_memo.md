# official_dt 軽量サンプル調査メモ

調査日: 2026-05-12

## 方法

- `Opinion/odds_features/analyze_official_dt_sample.py` を作成。
- 依存追加なし、標準ライブラリのみ。
- `race_odds.csv` は実体がヘッダなしに見えるため、`src/storage.py` のスキーマ順に明示ヘッダを付与して読む。
- 先頭 200,000 行と末尾 200,000 行だけを調査し、`race_entries.csv` の `st_time` と比較。

## 実行結果

先頭 200,000 行:

```text
odds_rows_sampled=200000
races_sampled=547
races_with_multiple_official_dt=0
races_missing_st_time=0
bet_type_counts=RH3:21254, RT3:127524, SH2:12105, ST2:24192, W:12085, WH2:1000, WT2:1840
official_minus_st_time_minutes_bins=0:38, 1..5:508, >5:1
```

末尾 200,000 行:

```text
odds_rows_sampled=200000
races_sampled=555
races_with_multiple_official_dt=0
races_missing_st_time=0
bet_type_counts=RH3:21273, RT3:127658, SH2:12168, ST2:24336, W:12168, WH2:846, WT2:1551
official_minus_st_time_minutes_bins=0:36, 1..5:519
```

例:

```text
2026-03-25 pc=44 R1 den=11:02 st=11:05 official_dt=2026-03-25 11:06:00 delta_min=1
2025-01-01 pc=22 R1 st=10:56 official_dt=2025-01-01 10:58:00 delta_min=2
2024-01-01 pc=24 R1 st=10:45 official_dt=2024-01-01 10:47:00 delta_min=2
2021-04-06 pc=13 R1 st=15:40 official_dt=2021-04-06 15:41:00 delta_min=1
```

## 所感

- サンプル範囲では同一レース内の複数 `official_dt` は見えず、基本は 1 レース 1 スナップショット。
- `official_dt` は `st_time` と同時または 1〜5 分後に集中しており、「発走前スナップショット」とは言い切れない。
- 結果情報そのものが混入している証拠はないが、実運用で投票前に観測できる特徴量としてはリーケージ疑いが強い。
- `race_odds.csv` を pandas で読む実装を作る場合、現ファイルはヘッダなし前提で `names=[...]` を明示する必要がある。
