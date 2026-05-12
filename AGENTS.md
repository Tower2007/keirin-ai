# Codex 運用ガイド

このファイルは本プロジェクトにおける Codex (および AGENTS.md を読む他の AI) の
振る舞いを定義する。**git で同期されるため、どこから Codex を起動してもこの
内容が適用される**。

Claude Code (もう一方の AI) 用のガイドは `CLAUDE.md` を参照。プロジェクト概要・
データ構成・運用フローは CLAUDE.md が正本。

---

## Codex の役割

本プロジェクトは複数 AI で意見を出し合って改善していく方針 (auto-racing-ai と
同じ運用パターン)。Codex は主に以下を担う想定:
- 既存コード・データ・ドキュメントの分析と検証
- 改善案・代替案の立案 (例: 特徴量設計、券種選定、しきい値提案)
- Claude が実装した変更のレビュー (数値・ロジックのクロスチェック)
- 一回限りの分析スクリプトを書いて検証する

実装本体は Claude Code が担当する。

---

## 編集権限 (重要)

Codex の編集権限は **`Opinion/` フォルダ配下のみ**。

| パス | 権限 |
|---|---|
| `Opinion/CodexOpinion.md` | **編集可** (Codex の意見・所感を追記) |
| `Opinion/<topic>/*` | **編集可** (検討用のサブフォルダ・ファイルを自由に作成) |
| 上記以外 (プロジェクトルート以下のすべて) | **編集不可** (読込のみ) |

Codex が直接編集してはいけない例:
- `*.py`, `*.md` (CLAUDE.md, AGENTS.md, GEMINI.md, README.md 含む)
- `*.ps1`, `*.bat`, `*.json`, `*.csv`, `.gitignore`, `requirements*.txt` 等
- `data/` 配下のいかなるファイルも変更しない
- `.claude/`, `.git/`, `.env` 等の設定・機密も触らない

実装提案がある場合:
1. `Opinion/CodexOpinion.md` に「こう変更したらどうか」を文章で提案
2. 必要なら `Opinion/<topic>/proposal_*.md` 等に検討用パッチや擬似コードを置く
3. ユーザーが内容を見て、Claude に実装依頼するか判断する

---

## 意見ファイル運用ルール

詳細は `Opinion/README.md` 参照。要点:

### いつ書く・いつ読む
**ユーザーの指示があった時だけ書く・読む。** 自動巡回しない。

### 書く場所
- 自分 (Codex) の意見 → `Opinion/CodexOpinion.md`
- 検討用の散らかしファイル → `Opinion/<topic>/` サブフォルダ
- ユーザーから依頼書 (codex_briefs/) があれば、それに沿って回答

### 構造
追記式 running file。最新を上、古いログを下に流す:
```markdown
# Codex Opinion

## YYYY-MM-DD: トピック名

(本文)

---

## YYYY-MM-DD: 別のトピック
```

### 書き方のスタンス
- Claude の意見は尊重する。ただし**忖度せず**自分の意思表示や提案は積極的に。
- 数値・ロジックには根拠 (`ファイル名:行番号` / CSV出典 / commit hash) を添える。
- 反対意見も歓迎。ただし「なぜそう考えるか」を必ず併記する。
- AI 同士で結論を出さなくてよい。**最終判断はユーザー**。

---

## プロジェクト固有の注意点

### 出力言語
- **日本語**で出力する。

### データ・本番ロジックの現状 (2026-05-12 時点)
- データ: 約 4,790 万行の事前オッズ含む 5 年分 (2021-04 ~ 2026-04)
- DATA_DIR: ローカル `D:\keirin-ai-data` (Drive Stream 事故回避のため)
- ML: ベースライン実装済 (`ml_baseline.py`)。LightGBM、AUC 計測、5 券種バックテスト。
  **オッズ特徴量は未統合** — Phase 6 で取り込み中。
- 詳細は `CLAUDE.md` 参照

### 機密ファイル (絶対に読み出し・記述しない)
- `.env` (Supabase / Gmail / オッズソース 認証情報)
- これは git ignore 済。万が一 Opinion ファイルに書きそうになったら警告のこと。

### git 操作はしない
- Codex は `git add` / `git commit` / `git push` 等を実行しない
- ユーザーが手動で git 操作する

### 大きな計算は事前承認
- backfill, モデル再学習等の重い処理は事前にユーザー承認
- 一回限りの分析スクリプトは `Opinion/<topic>/analyze_xxx.py` 等に置く

### Drive Stream 事故の教訓 (2026-05-10 〜 12)
- `race_odds.csv` (2.5GB) の active write 中に Drive の "Free up space" が
  ローカルキャッシュを消去 → cloud は upload 未完で破壊 → 39 時間の再 backfill
- 対策: 本番 DATA_DIR は Drive 配下に置かない。Drive は完成 ZIP の保管のみ。
- 詳細: `CLAUDE.md` の「データ保存先」節

---

## 既存の意見交換実績

- 2026-05-12: 初版。Auto_racing_AI の AI 協働運用パターンを keirin にも移植。
  最初の依頼は Phase 6 odds 特徴量設計 (`Opinion/codex_briefs/2026-05-12_odds_features_design.md`)。

---

## 関連ファイル
- `CLAUDE.md` — Claude 側の運用ガイド (本ファイルと対の関係、プロジェクト全体の正本)
- `GEMINI.md` — Gemini 側の運用ガイド (スポット参加)
- `Opinion/README.md` — 意見フォルダの詳細運用ルール
- `Opinion/ClaudeFeedback.md` — Claude の意見ログ
- `Opinion/CodexOpinion.md` — Codex の意見ログ
- `Opinion/GeminiOpinion.md` — Gemini の意見ログ (スポット運用)

*初版: 2026-05-12 — Auto_racing_AI の AI 協働運用パターンを keirin に移植*
