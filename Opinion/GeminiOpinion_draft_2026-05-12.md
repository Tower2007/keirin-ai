# Gemini Opinion

Gemini の意見・所感を追記式で蓄積する (スポット参加)。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` および `GEMINI.md` を参照。

---

## 2026-05-12: Phase 6 方針の大局観レビュー

依頼書: `Opinion/gemini_briefs/2026-05-12_phase6_direction_review.md`

GEMINI.md と依頼書、Codex の `Opinion/CodexOpinion.md` 2026-05-12 エントリ、Claude の `Opinion/ClaudeFeedback.md` 2026-05-12 (2) を読んだ。加えて姉妹プロジェクトの `Auto_racing_AI/CLAUDE.md`、`Auto_racing_AI/daily_predict.py`、`Boat_racing_AI/strategy_config.py`、`Boat_racing_AI/weekly_status.py`、`Boat_racing_AI/CHANGELOG.md` を軽く確認した。

結論: Claude/Codex の方針は概ね正しい。ただし、keirin は「pre-start odds を 2-4 週間貯めれば解決」では少し楽観的。auto/boat の教訓は「発走前 odds を取る」だけでなく、「live odds と final odds の selection drift を本番後も監視し続ける」ことまで含んでいる。keirin でも最初から `snapshot取得 -> 推奨判定 -> final odds/払戻との突合 -> drift週次監視` を一つの運用単位として設計した方がよい。

### 1. リーケージ問題への対応方針

2-4 週間の蓄積遅れは許容すべき。むしろ本番運用前提なら、ここを待たずに upper-bound だけで意思決定する方が危ない。

Codex が見つけた `official_dt = st_time + 0〜5分` は、研究上の「軽い注意」ではなく、運用設計上の境界線だと思う。`with_final_odds` は市場の強さを測る upper-bound として価値があるが、本番可否の証拠にはならない。ここを混同すると、auto/boat で既に通った closing odds 問題を keirin で再発させる。

姉妹プロジェクト確認:

- auto は `Auto_racing_AI/CLAUDE.md` で、各レース発走 `LEAD_MIN=5` 分前に one-shot 予測し、`daily_predict.py` で発火時の全車 odds snapshot を `odds_snapshots.csv` に保存する設計になっている。さらに 10分前では late money 前で EV が上振れし、5分前へ短縮した経緯も明記されている。
- boat は `Boat_racing_AI/strategy_config.py` に「ROI は closing/final odds OOS、本番 live odds とは selection drift の可能性」と明記し、`weekly_status.py` で live_ev と final_ev の drift を週次監視する設計になっている。`CHANGELOG.md` v0.8.9 でも「本番初節後 live n=30〜50 蓄積するまで期待値として扱わない」とされている。

したがって keirin も「2-4週間待って honest backtest」だけでなく、最初から以下を保存すべき。

- `snapshot_dt`
- `minutes_before_start`
- その snapshot での推奨有無
- 後日突合する final odds / 払戻 / 実結果
- live EV と final EV の閾値跨ぎ件数

待つ期間は最低 2-4 週間でよいが、結論を出すには「件数」で管理した方がよい。全43場で開催数は多いので、カレンダー日数より `live snapshot が取れた正常レース数` と `BUY候補数` を gate にするのが安全。

### 2. 競輪とオートレースの根本的違い

ここは軽視しない方がいい。auto の成功パターンを移植する価値はあるが、keirin は「個体能力の順位付け」だけではなく「展開構造の読み」が入る。特にラインは、単なる追加特徴量ではなく、勝ち方・連対の相関構造そのものを変える情報。

auto は8車の独立競走に近く、試走/近況/オッズでかなり説明できる。一方 keirin は、同じ選手能力でもライン先頭か番手か、同県/地区連携か、単騎かで、勝率・連対率・三着内率の意味が変わる。ST2/SH2/RH3/RT3 のオッズから per-car marginal を作るだけだと、市場が読んでいるライン構造を後追いで吸っているだけになる可能性がある。

大局的には、keirin の本命は「odds 特徴量で AUC を上げる」より、「odds に対してモデルがどこで異議を唱えられるか」を見つけることだと思う。そのためには将来的に以下が必要。

- ライン人数、番手/三番手/先頭/単騎の位置
- ライン内の脚質整合性
- 先行候補の数、主導権争いの激しさ
- 地区・競走得点・脚質がライン内でどう噛み合うか

この領域を入れないと、keirin は auto のコピーというより「市場オッズの蒸留モデル」になりやすい。それ自体は upper-bound として有用だが、長期 EV の源泉としては弱い。

### 3. データ取得タイミングの整合性

ライン情報の過去欠損は、odds リーケージと同じくらい重要。`PJ0305 nInfo` がレース完了後に空になるなら、過去5年のラインを後から復元できない前提で考えるべき。

ここで避けたいのは、「過去5年 final odds あり・ラインなし」で作った強いモデルを、今後の「pre-start odds あり・ラインあり」にそのまま接続すること。データ生成過程が変わるので、評価の意味がぶれる。

提案は二層構え。

- 短期: ラインなしモデルを本線にする。これは過去5年を活かせるし、pre-start odds 蓄積後の honest 評価にも早く乗る。
- 中期: ラインありモデルは、pre-start odds snapshot と同じ開始日以降の「新鮮な本番データ」だけで別枠評価する。

つまり、ライン蓄積まで本番検証を全部止める必要はない。ただし「ラインなし本番候補」と「ラインあり次世代候補」は別モデルとして扱うべき。ラインありを早く試したいなら、A/B というより shadow で良い。

### 4. 本番運用の到達点

keirin の到達点は、まず auto 型に寄せるべき。すなわち「メール/ダッシュボードで購入候補提示、ユーザー手動投票、自動投票はしない」。競輪は法的・規約面だけでなく、締切間際のオッズ変動と買い目確認の負荷が大きいので、最初から自動化に寄せるより、観測と規律を優先した方がよい。

boat 型の 3点BUY 発想は、keirin では誘惑が強いが危険。三連単は払戻が大きいので目立つが、9車の組合せ爆発により的中頻度が低く、少数ヒット依存の偽 edge が起きやすい。boat でも `strategy_config.py` で三連単/三連複の偶然大ヒットに釣られない規律が明文化されている。keirin はさらにこの心理リスクが強いはず。

最初の本番到達点はこう定義するのがよい。

- 対象: まず SH2/ST2/W など、的中頻度がある程度ある券種中心
- 運用: 発走5分前前後の live odds で EV 判定し、候補のみ通知
- 記録: 通知した候補、通知しなかったが final では条件を満たした候補、実際に買った/買わなかった履歴を分けて保存
- 評価: ROI だけでなく、selection drift、通知漏れ、締切間に合わず、オッズ低下を週次で見る

本番運用前提で最初に整えるべき順番は、モデル改善より観測基盤だと思う。`pre-start odds cron`、`snapshot log`、`final突合`、`weekly drift report`、`手動投票ログ` が先。モデルはその上で改善すればよい。

### 5. 姉妹プロジェクト間の知見流通

auto/boat の知見は移植すべき。ただし「戦略そのもの」ではなく「検証規律」を移植するのが本質。

移植価値が高いもの:

- LightGBM + isotonic 校正
- walk-forward / OOF 予測での評価
- final odds と live odds を明示的に分ける表記
- odds drift 監視
- canonical 戦略と shadow 戦略の分離
- 週次レポートで「未蓄積」「まだ期待値として扱わない」を明記する運用

そのまま移植すると危ないもの:

- auto の `top-1 + 複勝 EV` を keirin の W/二車複に機械的対応させること
- boat の 3点BUY 的な複数券種セット購入
- closing/final odds OOS の ROI を本番期待値として読むこと

auto/boat の pre-start odds 取得方法は確認価値が高い、というより必須に近い。今回軽く見た範囲では、auto は発走5分前の per-race 動的発火、boat は締切数分前の live odds check と final drift 監視を持っている。keirin もこの二つの折衷で、最初から「5分前 snapshot + final drift」を設計に入れるべき。

### 追加で気になった論点

1つ目は、netkeirin の `official_dt` の意味を外部サイト依存のまま信用しないこと。取得時刻 `snapshot_dt` はローカルで必ず記録するべき。`official_dt` はソース側の表示時刻、`snapshot_dt` はこちらが観測した時刻として分ける。

2つ目は、発走時刻の精度。`st_time` が実発走なのか締切なのか、場や開催でズレるなら、`minutes_before_start` の解釈が揺れる。auto が発走時刻を Program/Print から明示取得しているように、keirin でも発走/締切のどちらを基準にするかを固定した方がよい。

3つ目は、6月中旬に一度結論を出すとしても、それは「運用開始可否」ではなく「shadow 運用開始可否」にした方がよい。実弾の判断は、少なくとも live snapshot ベースの BUY 候補が一定数たまってから。boat の「live n=30〜50 まで期待値扱いしない」は keirin にも合う。

総評: Phase 6a/6b/6c の段階分けは妥当。ただし keirin 独自の本番到達点は「強いモデルを作る」ではなく、「発走前に見える情報だけで、ライン/odds drift/手動投票の運用誤差込みで勝てるかを観測できる状態」に置くべき。ここを先に定義しておけば、auto/boat の成功体験を活かしつつ、keirin 固有の罠に飲まれにくくなる。
