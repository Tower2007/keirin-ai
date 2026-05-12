# Claude Feedback

Claude (実装担当 AI) の意見・所感を追記式で蓄積する。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` 参照。

---

## 2026-05-12 (6): 観測基盤を毎分 cron → デーモン方式に置換

### 経緯
ユーザーから「毎分起動はウザい (CMD ウィンドウが点滅して使用感を損なう)」と
指摘あり。実装の手抜きを反省。auto は `dynamic_scheduler.py` で per-race
ワンショット方式、これに近い設計に変更すべきだった。

### 修正内容
- `ingest_odds_prerace.py` (毎分 cron 用) 削除
- `scripts/cron_odds_prerace.bat` 削除
- 新規 `ingest_odds_prerace_daemon.py`:
  - 1 日 1 回 (8:00) に pythonw 起動でコンソール非表示
  - 内部で各レースの 5 分前まで `time.sleep()` 待機 → capture → 次レースへ
  - 最終レース後に exit
  - entries 未準備時は最大 50 分 retry
- cron 入れ替え:
  - 旧: `keirin-ai-prerace-odds` (every minute) → 削除
  - 新: `keirin-ai-prerace-daemon` (daily 8:00) → 登録

### UX 改善
- 起動回数/日: 1440 → 1
- CMD 点滅: 毎分 → なし (pythonw)
- 取得精度: ±1 分 → ±数秒

### 比較結果記録 (G2 で生成)
- `D:\keirin-ai-data\odds_comparison_report.md`
- AUC 差分 (no_odds → with_odds):
  - y_win: 0.811 → 0.850 (+0.039)
  - y_top2: 0.780 → 0.826 (+0.046)
  - y_top3: 0.750 → 0.789 (+0.039)
  - y_rank MAE: 1.486 → 1.385 (-0.101 改善)
- ROI: 全部マイナスだが、三連単 top10-25% で +0.10 以上の改善
- 結論: odds 情報は明確に有用、ただし upper-bound 性能。pre-start snapshot
  蓄積後の honest backtest で確認が必要。

---

## 2026-05-12 (5): 6a-3 odds features + 3-way 比較実装 (G2)

### 実装内容
- `build_odds_features.py`: race_odds.csv (47.9M 行) → per-(race, car) 特徴量 CSV
  生成 (958,600 行、約 2 分)。Codex 提案の 6 候補 + log scale + flag 系を実装。
  ST2/SH2 のみ初版 (W, RH3, RT3 は段階投入で後段)。
- `ml_baseline.py` 拡張: `--use-odds` フラグ追加。`load_data` / `build_dataset` /
  `train_all_models` が odds 特徴量を optional で受け入れ。出力ファイルは
  `_with_odds` suffix で区別。
- `compare_odds_baseline.py`: no_odds / with_odds 両方を走らせて
  `odds_comparison_report.md` を生成。

### 副次的なバグ修正
- race_meta.csv のスキーマドリフト修正 (header 8 列 / 一部 row 9 列の混在)。
  `is_canceled` 列を全行に追加 (古い 13,437 行は False、新しい 59 行は既存値)。

### 設計上の注意
- `odds_features.csv` は race_odds.csv (post-start = リーケージあり) から作成
- ML に入れて測る AUC/ROI は upper-bound 性能の参考値
- 本番性能の証明にはならない (Codex/Gemini 共通指摘)
- 本番運用は `race_odds_prerace.csv` (pre-start snapshot) を別途使う

### 次のアクション
1. ユーザーが `python compare_odds_baseline.py` 実行 (約 16-40 分、4 ターゲット × 2 run)
2. 結果 `odds_comparison_report.md` を見て odds features の効果を確認
3. その後 G1 (6a-2: 弱ライン特徴) 実装に移る

---

## 2026-05-12 (4): 観測基盤 6a-1 実装着手 (F1)

### Codex 回答 (ライン逆引き feasibility) 受領
`Opinion/CodexOpinion.md` 2026-05-12 (line reconstruction) 参照。

結論:
- `in_line_jyuni` はライン内番手ではない (非空 3,476 行が全て失格者の違反位置)
- 簡易ヒューリスティック: 完全一致 2/46、same-line precision 0.48 → 実用不可
- 「弱ライン特徴量」アプローチに切り替え:
  - `is_probable_line_leader` (脚質、back_cnt、nige_cnt)
  - `same_region_support_count` (同地区追/両人数)
  - `has_clear_regional_partner`
  - `is_likely_tanki`
  - `line_type` (entries に既存)
- 二層モデル: 短期 (ラインなし + 弱特徴) / 中期 (公式ライン蓄積後の shadow)

### F1 実装: 観測基盤 6a-1
ユーザー判断 (F1) で「今日中に観測基盤を最小実装、蓄積開始」と決定。
6a-2 (弱ライン特徴) と 6a-3 (upper-bound 検証) は別タスクへ。

実装内容:
- `ingest_odds_prerace.py`: 毎分 cron で起動、本日のレースで [4, 6] 分前窓のレースの
  odds を netkeirin から取得して `race_odds_prerace.csv` に追記
- `scripts/cron_odds_prerace.bat`: cron ラッパー
- `src/storage.py` に schema 追加: `race_odds_prerace.csv` + `prerace_snapshot_log.csv`
- `CLAUDE.md` 運用フローに pre-start snapshot の節を追加

設計上の重要事項 (Codex/Gemini 指摘を反映):
- `official_dt` (netkeirin 表示時刻) と `snapshot_dt` (こちら観測時刻) を分離記録
- `minutes_before_start` も併記
- 既存 `race_odds.csv` は post-start = リーケージのため upper-bound 用途のみ

### 次のアクション
1. ユーザーが cron 登録:
   `schtasks /create /tn "keirin-ai-prerace-odds" /sc minute /mo 1 /tr "C:\Users\no28a\Claude-project\keirin-ai\scripts\cron_odds_prerace.bat"`
2. 翌日 (5/13) から live 蓄積開始 (5/12 は既に開催終了)
3. 平行で 6a-2 (`build_line_weak_features.py`) と 6a-3 (`build_odds_features.py`) を着手

### TODO (6a-2 / 6a-3)
- 6a-2: 過去 5 年の `race_entries` + `race_stats` から weak line features 5 候補を生成
- 6a-3: ST2/SH2 から odds features 6 候補生成 + `ml_baseline.py` に USE_ODDS フラグ追加で 3-way 比較
- 6b 蓄積後: weekly drift report (live ↔ final 突合) 実装

---

## 2026-05-12 (3): Gemini 回答受領 + ライン逆引き調査を Codex に依頼

### Gemini 回答の評価
`Opinion/GeminiOpinion.md` 2026-05-12 エントリ参照。

新規価値:
1. **Auto の ROI 数字を引用**: 最終オッズ 132% → スナップショット 105% (25 pt 減)
   = リーケージの実害が auto で実証済 → keirin で蓄積待ち必須の強い根拠
2. **ライン逆引きバックフィル提案**: 過去 `race_results.csv` の決まり手・着順から
   実際に形成されたラインを推定 → 過去 5 年に遡ってライン特徴量付与可能?
3. **auto+boat 融合戦略**: 9 車立て三連単 504 通り = 「市場の歪み残りやすい」 →
   boat 型高配当狙いが刺さる可能性 (draft の「auto型のみ推奨」と対立)

### draft (出所未確認の旧 GeminiOpinion) との差分
- draft: 「観測基盤がモデルより先」「boat 型は危険」と保守的設計重視
- Gemini 本物: 「ライン逆引き」という新規 actionable idea + 戦略融合提案

両者ともに価値がある。draft は `Opinion/GeminiOpinion_draft_2026-05-12.md` に退避済、
後の運用設計フェーズで参照。

### Phase 6 計画の再更新 (3 段階を 5 段階に詳細化)

| 段階 | 内容 | 所要 |
|---|---|---|
| **6a-1** | 観測基盤構築 (`ingest_odds_prerace.py` + drift log + weekly report) | 1-2 日 |
| **6a-2** | ライン逆引き feasibility 調査 (Codex 担当) | 半日 |
| **6a-3** | upper-bound 検証 (Codex 提案の 3-way 比較) | 1 週間 |
| **6b** | snapshot + shadow 蓄積 (live ↔ final 突合、live n=30〜50 gate) | 2-4 週間 |
| **6c** | 戦略確定 + shadow 運用開始判断 | 6 月下旬 |

### 次のアクション
ユーザー判断で「E2 → E1」(Codex に逆引き調査依頼 → その結果で観測基盤実装方針決定)。

新 brief: `Opinion/codex_briefs/2026-05-12_line_reconstruction_feasibility.md`
- `in_line_jyuni` の定義確認
- `race_lines.csv` (直近 1 ヶ月、validation set) との突合
- 逆引き実用精度の見込み
- 失敗時の代替案

逆引きが実用精度で可能なら Phase 6 のスケジュール短縮 + ライン構造を本格活用できる。
逆引き不能なら、Gemini の二層モデル案 (短期: ラインなし本線 / 中期: ラインあり別枠) に従う。

---

## 2026-05-12 (2): Codex 回答受領 → Phase 6 計画大幅更新 (本番運用前提)

### Codex 回答の評価
`Opinion/CodexOpinion.md` 2026-05-12 エントリ + `Opinion/odds_features/` 参照。
ほぼ全面的に賛同。特に以下 2 点は重大:

**🔴 1. リーケージ発見**
- `Opinion/odds_features/analyze_official_dt_sample.py` で先頭/末尾 各 20 万行を確認
- `official_dt` は `st_time + 0〜5 分` に集中 = **発走後/締切直後のスナップショット**
- 結果: 現 `race_odds.csv` は本番で投票前に観測できない → 単純に ML 特徴量にすると**運用リーケージ**
- 影響: 「odds ありモデル」は upper bound 扱い、本番性能の証明にはならない

**🟡 2. race_odds.csv はヘッダ無し**
- `ingest_odds.py` 経由で append のみ、ヘッダ書き込みされていない
- pandas read 時は `names=[...]` 明示必須

### Phase 6 ゴール再確認 (ユーザー判断 2026-05-12)
- **本番運用前提** (研究検証ではなく実運用に向けた整備)
- = pre-start odds snapshot の蓄積を**今すぐ始める**必要あり

### 修正後の Phase 6 計画 (3 段階)

| 段階 | 内容 | 開始 | 完了見込み |
|---|---|---|---|
| **6a** | 既存 odds で upper-bound 検証 + 3-way 比較 | 即時 | 1 週間 |
| **6b** | **pre-start odds snapshot 取得 cron 構築** | 即時 | 蓄積に 2-4 週間必要 |
| **6c** | snapshot 充足後、honest backtest + 本番モデル選定 | 6/中旬予定 | - |

### Codex 提案の受諾事項
- 特徴量命名: `imp_winprob_sh2` → `imp_top2prob_sh2`
- `odds_count_st2_1st` → `is_incomplete_st2` フラグ
- `log(odds_min)` 系の raw scale 列も保持
- 段階投入: ST2/SH2 → +W → +RH3/RT3
- WH2/WT2 は初期対象外
- 欠損は median + `has_*` フラグ (0 埋め禁止)
- 3-way 比較: `no_odds` / `with_final_odds` / `no_odds_model + odds_for_EV_only`

### 6b の設計案 (草稿、Gemini レビュー対象)
- 新規 `ingest_odds_prerace.py`
- sweep 方式: 15 分おきに「次 60 分以内に発走するレース」のオッズを取得
- 保存先: `race_odds_prerace.csv` (新ファイル、既存 race_odds.csv と分離)
  - 既存カラム + `snapshot_dt` + `minutes_before_start`
- Windows Task Scheduler: レース時間帯 (9:00-22:00) で 15 分おき発火
- 2-3 週間蓄積後に Phase 6c で honest 検証

### 次のアクション
Gemini に Phase 6 方針大局観レビューを投げる (既存 brief を更新)。
新 brief の追加論点:
- 「auto も pre-start odds 取得しているか? どのタイミングで取っているか?」
- 「2-3 週間の snapshot 蓄積を待つ価値はあるか? それとも upper-bound だけで前進?」
- 「本番投票実装まで視野に入れるなら、何を最初に整えるべきか?」

---

## 2026-05-12: AI 協働体制を keirin にも導入 + Phase 6 開始

`Auto_racing_AI` の AI 協働運用パターン (Claude/Codex/Gemini) を keirin にも移植した。
初版として AGENTS.md / GEMINI.md / Opinion/README.md / 本ファイルを作成。

### 直近の状況サマリ (2026-05-12 08:00 時点)
- Phase 3 完了: netkeirin から 5 年分の事前オッズ取得 (約 4,790 万行)
- Phase 4 完了: 5 年分バックフィル (race_meta / entries / stats / results / payouts)
- Phase 6 着手中: オッズ特徴量を ML に統合

### 5/10〜5/12 の事故と教訓
- Google Drive Stream の "Free up space" 機能が `race_odds.csv` (2.5 GB) の
  ローカルキャッシュを active write 中に消去 → cloud 側は upload 未完で壊滅
- Drive の `lost_and_found` フォルダから断片救出 + 39 時間の再 backfill で復旧
- 教訓: **大容量ファイルの active write 中は Drive Stream を使わない**
- 対策: DATA_DIR を `D:\keirin-ai-data` (ローカル SSD/HDD) に移動。Drive は ZIP 保管庫専用。
- 詳細: `CLAUDE.md` の「データ保存先」節、`scripts/backup_to_drive.py`

### Phase 6 の方針 (初版)
ml_baseline.py は完成しているがオッズ未活用。
オッズから per-(race, car) 特徴量を抽出して merge する設計を検討中。
候補特徴量:
- `imp_winprob_st2`: 二車単 1着付け odds から導出する勝率
- `imp_winprob_sh2`: 二車複 marginal から導出
- `pop_rank_st2_1st`: 1着候補としての人気順
- `odds_min_st2_1st`: 最人気 odds

Codex に設計レビューを依頼する brief を `codex_briefs/2026-05-12_odds_features_design.md` に投入予定。
