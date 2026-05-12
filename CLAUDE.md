# keirin-ai 運用ガイド

## プロジェクト概要

競輪（keirin.jp）のデータ蓄積・可視化・ML検証アプリ。
姉妹プロジェクト: auto-racing-ai (オートレース)、boat-racing-ai (競艇)。

## 技術スタック

- Python 3.13
- DB: ローカル CSV (DATA_DIR 配下) — Phase 5 で Supabase 移行予定
- データ取得: keirin.jp（公式）の HTML 内埋め込み JSON を抽出 / オッズは netkeirin
- ML: LightGBM (ml_baseline.py / ml_ev_backtest.py 実装済)

## AI 協働運用

複数 AI で意見を出し合って改善する体制 (Auto_racing_AI と同パターン)。

| AI | 役割 | 編集権限 |
|---|---|---|
| Claude Code | 実装本体 | プロジェクト全体 |
| Codex | 分析・数値検証・代替案立案 | `Opinion/` のみ |
| Gemini | 大局観・第三者レビュー (スポット) | `Opinion/` のみ |

詳細: `AGENTS.md` (Codex 用), `GEMINI.md` (Gemini 用), `Opinion/README.md`

## データ保存先 (DATA_DIR)

`.env` の `DATA_DIR` で指定。省略時はプロジェクト直下 `data/`。
**重要: 大容量ファイル (race_odds.csv 約 2.5 GB) の active write 中に
Google Drive Stream を使うと事故る** (Drive の "Free up space" が
ローカルキャッシュを消去 → cloud は upload 未完で壊れる)。

推奨運用:
- **本番 DATA_DIR はローカル SSD/HDD** (例: `D:\keirin-ai-data`)
- **Drive は完成 ZIP の保管庫** (`scripts/backup_to_drive.ps1` 経由)
- **別 PC への引き渡しは ZIP 経由** (ブラウザ DL → ローカル展開)

## ディレクトリ構成

```
src/
  client.py             # KeirinClient (cookie session, GET only)
  parser.py             # HTML 内 jsonData 抽出 + flat dict 変換
  storage.py            # CSV 読み書き (data/ 配下)
smoke_test.py           # 1日分スモークテスト (HTML/JSON 保存)
ingest_day.py           # 1日分データ取得 → CSV 保存 (差分対応)
ingest_today_lines.py   # 当日全場のライン取得 (cron 朝実行用)
backfill.py             # 過去データ一括取得
data/                   # CSV + スモーク (.gitignore)
```

## 運用フロー

ingest_day.py は **差分取り込み** に対応しているため、同じ venue-day を複数回呼べる:

| タイミング | スクリプト | 取得されるもの |
|---|---|---|
| 朝 7:00 (cron) | `ingest_today_lines.py` | 当日開催の全場の meta/entries/**lines**/stats |
| 翌朝 5:00 (cron) | `ingest_day.py YESTERDAY VENUE` を全場ループ | results/payouts のみ追加 |

**必ず朝に lines を取得**: PJ0305 の nInfo は完了後に空になるため。

### Windows タスクスケジューラ登録例
```cmd
schtasks /create /tn "keirin-ai-lines" /sc daily /st 07:00 ^
  /tr "python C:\Users\user\Claude-Project\keirin-ai\ingest_today_lines.py"
```

## CSV ファイル構成 (data/)

| ファイル | 内容 | キー | 取得タイミング |
|---------|------|------|------|
| race_meta.csv | 開催メタ（場・グレード・レース名） | race_date + place_code | レース前後どちらでも |
| race_entries.csv | 出走表（選手・脚質・予想印） | race_date + place_code + race_no + car_no | レース前後どちらでも |
| race_lines.csv | ライン情報 (narabiX/narabiY) | race_date + place_code + race_no + car_no | **レース前のみ**（完了後は nInfo 空配列） |
| race_stats.csv | 選手成績（勝率/連対率/戦法回数等） | race_date + place_code + race_no + car_no | レース前後どちらでも (JSJ002) |
| race_results.csv | 着順・決まり手・天気 | race_date + place_code + race_no + car_no | **レース完了後のみ** (JSJ012) |
| payouts.csv | 払戻金 (7券種) | race_date + place_code + race_no + bet_type + kumi_ban | **レース完了後のみ** (JSJ012) |

## keirin.jp スクレイピング仕様

公式サイトは **JSF (WebOTX) で HTML レンダリング** だが、
ページ HTML 内に `jsonData['XXXXX'] = {...};` の形でデータが埋め込まれているため、
正規表現抽出で JSON 同等の処理が可能。

### 主要エンドポイント (全て GET、CSRF 不要)

| 用途 | URL | データ |
|---|---|---|
| 当日全場レース一覧 | `GET /pc/json?type=JSJ048&...` | 純JSON |
| 開催プログラム | `GET /pc/dfw/dataplaza/guest/raceprogram?KCD=&KST=YYYYMMDD` | HTML 内 `jsonData['PC0201']` |
| 出走表一覧（全12R） | `GET /pc/racelist?encp={encParaR}` | HTML 内 `jsonData['PJ0305']` |
| 選手成績 (全12R分) | `GET /pc/json?type=JSJ002&encp={encParaR}` | 純JSON、`raceInfo[12].sensyuTypeInfo[]` |
| 結果 + 払戻 (1R分) | `GET /pc/json?type=JSJ012&encp={encParaR}` | 純JSON、`tyakujyunItemSubData` + `haraiGakuSubData` |

- セッションCookie 必須: 初回 `/pc/top` を踏んで取得
- リクエスト間隔: 0.8秒 (`.env` の `KEIRIN_REQUEST_DELAY_SEC`)
- `encParaR` は raceprogram の PC0201 内 `C0201race[].encParaR` から取得
- 1ページで12レース分の出走表が取れる（autorace の per-race POST より効率的）

### 場コード (jocd / KCD / naibuKeirinCd)

2桁コード。北から:
11 函館, 12 青森, 13 いわき平,
21 弥彦, 22 前橋, 23 取手, 24 宇都宮, 25 大宮, 26 西武園, 27 京王閣, 28 立川,
31 松戸, 32 千葉, 34 川崎, 35 平塚, 36 小田原, 37 伊東, 38 静岡,
42 名古屋, 43 岐阜, 44 大垣, 45 豊橋, 46 富山, 47 松阪, 48 四日市,
51 福井, 53 奈良, 54 向日町, 55 和歌山, 56 岸和田,
61 玉野, 62 広島, 63 防府,
71 高松, 73 小松島, 74 高知, 75 松山,
81 小倉, 83 久留米, 84 武雄, 85 佐世保, 86 別府, 87 熊本

## コーディング規約

- 出力言語: 日本語
- docstring: 日本語
- 変数名: snake_case (英語)
- 進捗ログ: ASCII のみ（Windows cp932 対策。Python の `print` 経由で日本語出すときは
  `sys.stdout.reconfigure(encoding='utf-8')` を呼ぶ）
- 全角→半角は parser で NFKC 正規化（「Ｓ級一般」→「S級一般」）

## Phase ロードマップ

- ✅ **Phase 1**: 開催メタ・出走表・ライン情報 → CSV
- ✅ **Phase 2**: 選手成績 (JSJ002)・結果＋払戻 (JSJ012) (現在地)
- ⏳ **Phase 3**: 事前オッズ取得 (要ログイン or netkeirin等の代替)
- ⏳ **Phase 4**: backfill.py (5年分一括取得)
- ⏳ **Phase 5**: Supabase 移行 (auto-racing-ai と同じ構成)
- ⏳ **Phase 6**: LightGBM での予想検証

## 払戻券種コード

| code | 名称 |
|---|---|
| WH2 | 枠複（多くは未発売） |
| WT2 | 枠単（多くは未発売） |
| SH2 | 二車複 |
| ST2 | 二車単 |
| RH3 | 三連複 |
| RT3 | 三連単 |
| W | ワイド（3組合せ返却） |

## 既知の注意点

- HTML 内 jsonData は単引用符・二重引用符 両方で出現する（parser は両対応）
- HTTP 5xx は urllib3 Retry で自動再試行（最大5回、指数バックオフ）
- 重複投入防止: ingest_day は `race_meta.csv` に同じ race_date+place_code があれば skip
- 未開催日（KCD+KST に開催なし）: PC0201 が空または `C0201data` 不在で判定
