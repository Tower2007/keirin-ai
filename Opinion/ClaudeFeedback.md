# Claude Feedback

Claude (実装担当 AI) の意見・所感を追記式で蓄積する。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` 参照。

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
