# Codex Opinion

Codex の意見・所感を追記式で蓄積する。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` および `AGENTS.md` を参照。

---

## 2026-05-15: 多モデル化 + odds 降格 + snapshot timing 設計レビュー

依頼書: `Opinion/codex_briefs/2026-05-15_multi_model_and_odds_demotion.md`

Gemini 5/15 エントリ、ClaudeFeedback の pivot 記録、`ml_baseline.py`、`ingest_odds_prerace_daemon.py`、`D:\keirin-ai-data\w_odds_compare_report.md`、`feature_importance_report.md` を確認した。詳細な設計メモは `Opinion/multi_model/proposal.md` に置いた。

結論: Phase 6 pivot は妥当。`no_odds_model + odds_for_EV_only` を本番主軸に格上げし、`with_odds*` は upper-bound/shadow へ降格でよい。ただし「多モデル化」は即フル実装ではなく、まず production の命名・評価レイヤを切り分け、live snapshot が n=30〜50 貯まってから v2 に進むのが安全。

### Q1. `no_odds_model + odds_for_EV_only` を本番主軸にする妥当性

賛成。理由はシンプルで、予測モデルと市場価格を分離しないと drift の原因分析ができないから。

`with_odds` の y_win AUC 0.850、`no_odds` の y_win AUC 0.811 という差は大きいが、これは `feature_importance_report.md` の通り `imp_winprob_st2` が y_win gain の 81.83% を占める「市場の蒸留」で得た差。final-like odds を特徴として入れたモデルは、実運用では「5分前/3分前/2分前の市場」を「発走直後/最終市場」の代替として食わせることになり、入力分布そのものが変わる。

本番主軸は以下の切り分けがよい。

```text
production_no_odds:
  学習入力: race_entries + race_stats + race_meta + optional weak_line
  odds入力: なし
  用途: 選手能力/レース構造の自力予測

ev_layer:
  入力: production_no_odds の予測 + race_odds_prerace.csv の live snapshot
  用途: 購入判定、threshold crossing、drift 監視
```

市場が読んでいる当然の情報を捨てる懸念はある。ただし、それは EV layer で live odds として再導入すればよい。モデル内に odds を入れると「市場が読んでいる情報」と「モデルが読んでいる情報」が混ざり、どこで edge が出たのか追えなくなる。本番運用では、予測器は市場非依存、購入判定は市場依存、という責務分離を優先すべき。

実装上の呼称も固定したい。`no_odds` を単なる baseline ではなく `production_no_odds` と呼び、`with_odds` は `shadow_final_odds` / `upper_bound_final_odds` と名前から用途を明示する。

### Q2. odds 入りモデルの upper-bound / shadow 降格

降格判断に賛成。対象は `with_odds`, `with_odds_w`, `with_odds_line`, `with_odds_mp` の全て。これらは研究価値はあるが、production pick の根拠にしない。

ただし「本線は何のフラグもない素の baseline で良いか?」には少しだけ補足したい。弱ライン特徴は 5/13 検証では gain 1% 未満で強くないが、drift しない非 odds 特徴なので、production 候補から完全排除する必要はない。推奨は二段階。

```text
production_v0 = base
production_v0_line_shadow = base + weak_line
```

まずは `base` を canonical として固定し、`weak_line` は shadow。live n>=50 で `base` と `base+weak_line` の drift 後 ROI/的中率を比較して採否を決める。過去 final odds の top5% ROI だけで採用しない。

レポート出力も以下のように分けると混乱が減る。

```text
Production candidates:
  base
  base_line_shadow

Diagnostics only:
  final_odds_upper_bound
  final_odds_w_upper_bound
  final_odds_line_upper_bound
  final_odds_mp_upper_bound
```

### Q3. 順序系/非順序系の多モデル分離

方向性は賛成。ただし「2つの LightGBM モデル」にいきなり分けるより、まず `target -> feature_set` を分離できる設計へリファクタするのがよい。現 `ml_baseline.py` は `train_all_models()` が全ターゲットに同じ `feat_cols` を渡す構造なので、最小改修はここ。

擬似コード:

```python
FEATURE_SETS = {
    "production_base": BASE_NUM + BASE_CAT,
    "order_shadow": BASE_NUM + BASE_CAT,  # production では odds なし
    "place_shadow": BASE_NUM + BASE_CAT,
    "order_final_odds_upper": BASE + ST2_ODDS,  # W は入れない
    "place_final_odds_upper": BASE + SH2_ODDS + W_ODDS,
}

TARGET_SPECS = {
    "y_win":  {"family": "order", "features": "order_shadow"},
    "y_rank": {"family": "order", "features": "order_shadow"},
    "y_top2": {"family": "place", "features": "place_shadow"},
    "y_top3": {"family": "place", "features": "place_shadow"},
}
```

最初の production は、モデルオブジェクトとしては従来通り 4 target でよい。ただし feature list を target 別に渡せるようにしておく。次の段階で以下に分ける。

```text
Order family:
  targets: y_win, y_rank
  用途: ST2, RT3 の1着/順序生成
  odds shadow: ST2系のみ。Wは入れない。

Place family:
  targets: y_top2, y_top3
  用途: SH2, RH3, W の組合せ生成
  odds shadow: SH2 + W を許可。
```

券種生成も family を分ける。

```python
ST2:
    first = argmax(order.p_win)
    second = argmax(place.p_top2 excluding first)

RT3:
    first = argmax(order.p_win)
    second = argmax(place.p_top2 excluding first)
    third = argmax(place.p_top3 excluding first/second)

SH2:
    top2 = top2 by place.p_top2

RH3/W:
    top3 = top3 by place.p_top3
```

ここで重要なのは、RT3 を `p_win` 上位3台だけで作る現行 `make_picks()` より、2/3着を place family から取る方が自然なこと。三連単は「1着候補」と「ヒモ候補」を分けた方がよい。これは Gemini の boat 知見と整合する。

### Q4. W 追加で三連単悪化の細部解釈

仮説は概ね妥当。W は「3着以内に入るか」の marginal 情報であって、「1-2-3 の順序」をほぼ持たない。`w_odds_compare_report.md` でも、y_top3 AUC は +0.005 改善し、三連複/ワイドの top5/top10 ROI は改善。一方で三連単 top10 ROI は -0.0488 と大きく悪化している。

ただし、細部は「y_top2/y_top3 に過剰最適化されたから y_win が劣化した」と断定するより、次のように見る方が正確。

1. W は top3 membership を強くする。
2. 現 `make_picks()` の RT3 は `p_win` 順上位3台をそのまま `1-2-3` にする。
3. W 追加で `y_top3` / `y_rank` の順位付けが改善しても、RT3 に必要な条件付き順序、特に「2着/3着の入れ替わり」や「番手差しの1着」は改善しない。
4. その結果、三連複は当たりやすくなるが、三連単の並びは崩れうる。

feature importance で確認すべき兆候:

```text
with_odds_w の target 別 importance を出す。
期待される整合パターン:
  W系特徴 gain: y_top3/y_rank で高い
  W系特徴 gain: y_win で低い
  ST2系特徴 gain: y_win で高い
```

現 `feature_importance_report.md` は `with_odds_line` のものなので、W 系の importance はまだ見えていない。これは次に `analyze_feature_importance.py --use-odds --use-w-odds` 相当で再出力する価値がある。加えて、RT3 悪化の直接検証として、W 追加前後で「実際の1着車の `p_win` rank」が top10% selected races で悪化していないかを見るとよい。

### Q5. 5分/3分/2分 snapshot 比較設計

現 `ingest_odds_prerace_daemon.py` は既に `--lead-min` と `KEIRIN_LEAD_MIN` に対応している。これは単一 timing 運用には十分。ただし 5/3/2 の比較実験には足りない。現状の `load_captured()` は `(race_date, place_code, race_no)` 単位で status=ok を既取得扱いにするため、同じレースで5分前を取ると3分前/2分前は skip される。

推奨は設計案 B の改良版、つまり「1 daemon で複数 offset を同時スケジュール」。

```text
CLI:
  --lead-min 3          # 本番単一運用
  --lead-mins 5,3,2     # 比較蓄積モード

保存列:
  target_offset_min     # 狙った offset: 5/3/2
  minutes_before_start  # 実測 offset

既取得キー:
  (race_date, place_code, race_no, target_offset_min)
```

`race_odds_prerace.csv` と `prerace_snapshot_log.csv` に `target_offset_min` を足すのが望ましい。`minutes_before_start` は sleep ズレや取得遅延後の実測値なので、狙い値とは分けるべき。

案 C の cron 3本並列は避けたい。理由は、同時刻付近で同一レース/APIにアクセスし、ログ競合・重複判定・netkeirin負荷が増えるから。案 A の単一切替は運用には良いが、比較データが同日同条件で揃わない。したがって、蓄積期間は B'、本番移行後は A がよい。

比較開始は n=30 からでよいが、n の定義を「snapshot 行数」ではなく「BUY 判定候補が発生したレース数」に寄せたい。最低限は:

```text
offset別:
  n_ok_races
  n_selected
  threshold_cross_down_rate = live EV >= thr かつ final EV < thr
  threshold_cross_up_rate   = live EV < thr かつ final EV >= thr
  live_ev_mean / final_ev_mean / drift_mean
  selected ROI
```

auto は現状 `LEAD_MIN=2` 単一値運用で、`odds_snapshots.csv` には `captured_at` が残る。過去の5分前データは「当時の snapshot」としては残るが、同一レースで 5/3/2 を並列比較する設計ではない。keirin は今から設計するなら、`target_offset_min` を持たせて並列比較可能にした方が後で強い。

### 追加の見落とし候補

1. EV 計算は券種ごとに確率校正が必要。`p_win`, `p_top2`, `p_top3` の AUC が良くても、`p * odds` で使うには calibration が重要。production では isotonic / Platt / bin calibration のいずれかを入れたい。

2. RT3 は当面 production BUY から外し、shadow にした方がよい。W 追加で三連単が悪化しただけでなく、RT3 は確率推定誤差と odds drift の両方に最も弱い。

3. `with_odds*` の実験自体は止めなくてよい。ただし目的は「本番候補」ではなく「市場が何を読んでいるかの監査」。レポート名と見出しに必ず `final_odds_upper_bound` を入れる。

4. `line` は production 本線に即採用しない。前回検証どおり弱いので、`base_line_shadow` として drift 後に採否判断する。

実装優先度:

```text
P0: production_no_odds / final_odds_upper_bound の命名整理
P1: snapshot schema に target_offset_min を追加する設計
P2: weekly drift report skeleton
P3: target別 feature_set を受けられる train_all_models へ小改修
P4: order/place family の shadow 評価
```

総評: Gemini の pivot は大枠で正しい。ただし多モデル化を急いで大改修するより、まず「odds を特徴から降格」「snapshot timing を比較可能にする」「drift report に集約」の3点を固めるのが先。モデルの腕力より、観測設計を壊さないことが今は一番大事。

---

## 2026-05-12: 過去ライン逆引きバックフィル feasibility

依頼書: `Opinion/codex_briefs/2026-05-12_line_reconstruction_feasibility.md`

結論から言うと、`in_line_jyuni` 直接利用で過去ラインを復元する道はほぼ閉じている。現データ上の `in_line_jyuni` は「ライン内番手」ではなく、失格時の違反/走行位置に付随する補助情報に見える。したがって、過去5年分へ公式ライン同等の `line_id` / `line_pos` を後付けするのは非推奨。やるなら「信頼度付きの部分特徴」に落とすべき。

検証スクリプトとメモ:

- `Opinion/line_reconstruction/analyze_line_reconstruction.py`
- `Opinion/line_reconstruction/line_reconstruction_memo.md`

### 1. `in_line_jyuni` の定義について

`race_results.csv` 全体を軽く走査したところ、行数は 969,365 行。そのうち `in_line_jyuni` 非空は 3,476 行だけだった。分布は以下。

```text
in_line_jyuni_counts_top=(blank):965889, 3:541, 2:539, 6:506, 4:480, 5:474, 1:439, 7:421, 8:53, 9:23
in_line_nonblank_profile_top=tyaku=blank:3476, state=失格:3476, note=斜行:798, note=内側追い抜き:738, note=押上げ:580, note=内圏線踏み切り:501, note=押圧:431, note=外帯線内進入:190
```

重要なのは、非空 3,476 行がすべて `tyaku` 空、`kojin_state=失格` だったこと。通常完走者にはほぼ入っていない。たとえば `2026-03-26,61,2` では失格車だけ `in_line_jyuni=1` を持ち、完走した1〜5着車は空。これはライン内番手ではなく、失格判定時の走行位置/内線順位系の情報だと見るのが自然。

したがって、仮説 A「`in_line_jyuni` が 1=先頭、2=番手... を直接表す」は不成立。

### 2. `race_lines.csv` との突合

`race_lines.csv` は現ファイルでは 2026-04-26 のみ、46レース/330行。場は 11/31/42/53/87。直近1ヶ月というより、現時点では 1 日ぶんの小さな validation set と見た方がよい。

`narabi_y` は 330行すべて `1` だった。`src/parser.py` のコメントでは `narabiY` はライン番号とされているが、少なくとも現データではライン番号として使えない。一方 `narabi_x` は横座標で、`x` の空白がライン区切りを表しているように見える。例: `2026-04-26,42,1` は x 順に `5-2-3 / 1-4` と読め、出走表の `line_type=二分戦` と整合する。

検証では `narabi_x` を昇順に並べ、隣接 x の差が 2 以上ならライン区切りとするものを gold とした。

### 3. 逆引き feasibility

結果/出走表だけでの簡易ヒューリスティックを試した。内容はかなり単純で、`in_line_jyuni` が使えない場合に `prefecture` の地区 + `kyaku` の逃/両を先頭候補、追を後位候補としてグルーピングするもの。

結果:

```text
validation_races=46
line_type_counts=三分戦:15, ライン無し:12, 二分戦:7, 四分戦:7, コマ切れ:5
x_gap_gold_exact_from_naive_pred=2/46
same_line_pair_precision=0.482
same_line_pair_recall=0.538
races_with_any_pair_hit=32/46
```

これは「雑な下限」ではあるが、過去結果/出走表だけで公式ラインを完全復元できる見込みは薄い。完全一致 2/46 は実用外。same-line pair の precision/recall が 0.5 前後なので、同ライン候補を特徴量としてそのまま信用するにも弱い。

ML 特徴量として公式ライン同等に使うなら、最低でも same-line pair F1 0.8 以上、または主要特徴である `is_line_leader` / `line_pos` の正解率 80〜90% 程度が欲しい。現状の簡易復元はそこに届いていない。

### 4. 推定不能ケースと部分復元の価値

推定不能になりやすいケース:

- 単騎が多い `ライン無し` / `コマ切れ`
- 同地区でも別線に分かれるケース
- 遠征・混成ライン
- 先行型が複数いて、誰がライン先頭か出走表だけでは決まらないケース
- 結果でラインが崩れたケース。結果から「実際の並び」を推定すると、事前ラインとは別物になり得る。

部分復元には価値がある。ただし「復元ライン」と名乗るより、以下のような probabilistic / weak feature として使うべき。

- `is_probable_line_leader`: 脚質、バック数、逃げ回数からの先頭候補
- `same_region_support_count`: 同地区の追/両が何人いるか
- `has_clear_regional_partner`: 同県/同地区の後位候補がいるか
- `is_likely_tanki`: 同地区相手が少ない、またはライン無し開催で単騎っぽい
- `line_type` が過去 entries にあるなら、二分戦/三分戦/ライン無しだけをそのまま使う

この形なら、間違っても LightGBM が重みを調整できる。逆に、誤った `line_id` を断定的に入れるとノイズが強く、特に EV 判定では危ない。

### 5. 実装負荷

公式ライン同等の `build_lines_backfill.py` は、精度を出そうとすると中〜高負荷。地区辞書、同県優先、脚質、競走種別、級班、レース種別、ライン無し判定、単騎判定、複数先行型の分岐などが必要になる。それでも検証 gold が現状 46レースしかないため、改善したかどうかの評価も不安定。

一方、部分特徴生成なら低〜中負荷で実装可能。`race_entries.csv` + `race_stats.csv` から per-car の「先頭候補」「支援候補数」「単騎疑い」を作るだけなら、過去5年 100万行弱でも処理時間は数分〜十数分程度に収まるはず。全量 CSV を一度読むだけなので、LightGBM 学習よりは軽い。

### 6. 代替案

逆引き完全復元は採用しない方がよい。代替案は二層モデルが現実的。

1. 短期本線: ラインなし/弱ライン特徴モデル  
   過去5年を最大限使う。`line_type`、脚質、地区支援数、先頭候補などの弱特徴に留める。

2. 中期 shadow: 公式ラインありモデル  
   2026-04-26 以降の朝取得ラインを蓄積し、pre-start odds snapshot と同じく本番観測データとして評価する。件数が増えるまでは本線にしない。

3. gold 増強: `race_lines.csv` の蓄積を最優先  
   公式ラインは後から取れないので、毎朝の取得失敗監視が重要。ライン取得失敗日は「取り返し不能な欠損」と扱う。

総評: `in_line_jyuni` に期待した大逆転はなかった。ここで無理に過去ラインを復元するより、短期は「ラインなし + 弱特徴 + odds upper/live 比較」で進め、公式ラインは今後の蓄積データで shadow 評価するのが安全。

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
