# Codex Opinion

Codex の意見・所感を追記式で蓄積する。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` および `AGENTS.md` を参照。

---

## 2026-05-12: Phase 6 オッズ特徴量設計レビュー

依頼書: `Opinion/codex_briefs/2026-05-12_odds_features_design.md`

結論から言うと、Claude 案の「まず ST2/SH2 から per-(race, car) の市場評価を作る」は妥当。ただし、現 `race_odds.csv` の `official_dt` はサンプル上ほぼ `st_time` 同時〜数分後で、発走前に観測できる特徴量としては危険。最初の実装は「市場コンセンサスを入れた上限性能確認」と「本番で使える事前特徴量」の線を明確に分けるべき。

### 1. オッズ特徴量 6 候補の妥当性

6 候補は初回アブレーションとして賛成。特に `imp_winprob_st2` は ST2 の 1着付けを全相手へ足し上げるので、車番単位の `y_win` に自然に対応する。`pop_rank_st2_1st` / `odds_min_st2_1st` も「1着として市場がどれだけ強く見ているか」を LightGBM に素直に渡せる。

ただし `imp_winprob_sh2` は名前を変えたい。SH2 は順序なし二車複なので、これは勝率ではなく「連対圏の市場 marginal」に近い。`imp_top2prob_sh2` または `imp_pairprob_sh2` の方が誤読が少ない。正規化はレース内 sum=1 にしてよいが、未正規化の合計や最小 odds も市場過熱・過小評価の情報を持つので、最終的には `log(odds_min)` など odds スケールの列も残す価値がある。

`odds_count_st2_1st` はキャンセル・欠車検出には良いが、通常は `race_n_cars - 1` に張り付くはずなので主力特徴量ではない。`expected_count` との差、または `is_incomplete_st2` のような品質フラグにした方が役割がはっきりする。

W は入れる価値あり。ただし初回 6 候補には入れず、第二段で `imp_top3prob_w` / `pop_rank_w_min` として足すのがよい。`src/netkeirin_parser.py:61-67` では W だけ `max_odds` を持つため、下限 `odds`、上限 `max_odds`、中点または保守的に下限を使う、のどれかを明示する必要がある。W は `y_top3` にはかなり効く可能性がある一方、勝率モデルに混ぜると「3着内人気」を勝率に過剰転用する懸念がある。

三連系は「使わない」ではなく「段階投入」が良い。RH3 は `car in combination` で `y_top3` に対応しやすい。RT3 は `car in 1st / 2nd / 3rd position` の positional marginal を作れるので情報量は大きい。ただし RT3 は行数が支配的で、サンプル 20万行でも RT3 が約 12.7万行を占めた。最初から全部入れると市場特徴量が強すぎて既存特徴量の評価が見えにくくなるため、順序は `ST2/SH2` → `+W` → `+RH3/RT3` のアブレーションが良い。

WH2/WT2 は初期対象外でよい。発売有無が場・開催に依存し、欠損処理のノイズが大きそう。

### 2. リーケージリスク

現時点の最大リスクはここ。`src/storage.py:101-106` では `race_odds.csv` は「official_dt 時点の最新オッズ」と定義され、`src/netkeirin_parser.py:49` で API 由来の `official_dt` をそのまま保存している。つまり `official_dt` が実際にいつ観測可能だったかが、そのまま特徴量の利用可能時刻になる。

軽量調査として `Opinion/odds_features/analyze_official_dt_sample.py` を作り、先頭 200,000 行と末尾 200,000 行を `race_entries.csv` の `st_time` と比較した。詳細は `Opinion/odds_features/official_dt_sample_memo.md`。結果は以下。

- 先頭 200,000 行: 547 レース、複数 `official_dt` のレース 0、`official_dt - st_time` は 0分が38件、1〜5分後が508件、5分超が1件。
- 末尾 200,000 行: 555 レース、複数 `official_dt` のレース 0、`official_dt - st_time` は 0分が36件、1〜5分後が519件。
- 例: `2026-03-25,44,1` は `st_time=11:05` に対し `official_dt=2026-03-25 11:06:00`。

この分布は「発走前スナップショット」ではなく「締切後/発走直後の最終オッズ」に見える。結果そのものが反映されている証拠はないので、ラベルリーケージと断定はしない。ただし本番の購入判断時点で観測できないなら、運用リーケージである。少なくとも「odds ありモデル」の AUC/ROI は、発走直前に同じ odds を取得できる運用を別途作るまでは上限値として扱うべき。

切り分け案:

- `official_dt <= race_date + st_time - 1分` を満たす行だけで特徴量を作るモードを用意する。現データではほぼ空になる可能性が高い。
- その場合は、過去データでは「最終オッズ upper bound」、今後の cron/live 取得では「発走 N 分前 snapshot」を別ファイルに保存する。
- `official_dt` と `st_time` の差分分布を全期間または日別サンプルで監査し、`odds_snapshot_type = final_like / pre_start` のようなメタ情報を odds_features に残す。

追加注意: 現 `D:\keirin-ai-data\race_odds.csv` は実体がヘッダなしに見える。`pd.read_csv(DATA / "race_odds.csv")` だと先頭行をヘッダ扱いするため、特徴量生成では `names=["race_date", ...]` を明示する必要がある。

### 3. ベースラインへの統合手順

`ml_baseline.py:155-190` の `build_dataset()` はすでに車番単位なので、`odds_features.csv` を `race_date, place_code, race_no, car_no` で left join する設計は妥当。`FEAT_NUM` への odds 列追加と、欠損フラグ `has_odds` / `is_odds_incomplete` を入れるだけでまず動かせる。

比較設計は 3 本立てを推したい。

1. `no_odds`: 現行そのまま。基準線。
2. `with_final_odds`: 現 `race_odds.csv` から作った odds 特徴量を入れる。これは市場情報を入れた上限性能確認として AUC、MAE、ROI、特徴量重要度を見る。
3. `no_odds_model + odds_for_EV_only`: 予測モデルは odds なしで学習し、券ごとの購入判断だけ odds を使って `model_prob * odds` に近い EV を作る。モデルの自力予測と市場価格の分離になり、本番 fallback も設計しやすい。

ただし 2 と 3 の ROI は、現 odds が投票前に観測不能なら「運用可能なバックテスト」ではない。まずは同一 train/test split (`ml_baseline.py:201-204`) で AUC 差分を見る。その後、評価期間を 2025-01〜2025-08 calibration / 2025-09〜2026-04 evaluation に分ける現 `ml_ev_backtest.py` の考え方へつなぐのが自然。

実装順の提案:

- `build_odds_features.py` は全量処理前に `--sample-rows` / `--bet-types ST2,SH2` を持たせる。
- まず ST2/SH2 の 6 候補だけで `odds_features_st2_sh2.csv` を作る。
- `ml_baseline.py` 側は `USE_ODDS` または CLI 引数で odds merge を切り替え、同じデータ分割で `no_odds` と `with_odds` を比較する。
- 次に `+W`、最後に `+RH3/RT3` を追加して、AUC だけでなく ROI と選択率も見る。
- 欠損や発売なしは 0 埋めではなく、数値列はレース内中央値/大きな sentinel + `has_*` フラグを併用する。オッズがないこと自体が情報になり得る。

総評: 特徴量案は方向性よし。ただし `official_dt` の時刻が発走後寄りなので、ここを曖昧にしたまま「改善した」と判断するのは危ない。Phase 6 では、まず市場を入れた upper bound として性能を見つつ、本番用には発走前 snapshot を別途取る設計に分けるのが一番きれい。
