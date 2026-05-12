# Claude Feedback

Claude (実装担当 AI) の意見・所感を追記式で蓄積する。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` 参照。

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
