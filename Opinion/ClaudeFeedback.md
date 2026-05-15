# Claude Feedback

Claude (実装担当 AI) の意見・所感を追記式で蓄積する。

最新を上、古いログを下。日付つき見出しで区切る。
詳細運用は `Opinion/README.md` 参照。

---

## 2026-05-16: 🔴 重大バグ発見 — lines cron が 4 日連続取り逃し

### 発覚の経緯
ユーザーが「データ収集継続してる? タイミングは?」と質問。確認したところ
5/13〜5/16 が全て「No venues open」= 4 日連続開催ゼロ表示。競輪が 1 週間
開催ゼロは通常ありえない → 測定スクリプトのバグを疑い調査。

### 根本原因
JSJ048「今日のレース一覧」は **朝 7:00 時点ではまだ前日分**を返す:
- 5/16 07:52 に叩く → kaisaiDate=20260515 (前日、8 場)
- 5/16 08:04 に叩く → kaisaiDate=20260516 (当日、6 場) に切替

`ingest_today_lines.py` の `kaisaiDate == today` フィルタが朝 7:00 で空になり
"No venues open" を誤出力 → race_entries 書かれず → daemon が snapshot 0 件。

### 被害の確定 (results cron との突合で判明)
results cron は明示日付指定なので正常動作していた:
| 日 | 実際の開催 | lines cron 判定 |
|---|---|---|
| 5/13 | 7 場 (completed=7) | ❌ "No venues open" |
| 5/14 | 6 場 (completed=6) | ❌ "No venues open" |
| 5/15 | 8 場 (completed=8) | ❌ "No venues open" |
| 5/16 | 6 場 | ❌ → **手動救出成功** |

- **5/13〜5/15 のライン/出走表は永久ロスト** (PJ0305 nInfo は完了後空)
- snapshot 蓄積が完全停止していた (Phase 6 の根幹が動いてなかった)
- backfill 期間中 (〜5/3) と results は無事、被害は lines/entries/snapshot のみ

### 対応
1. **即時**: 5/16 08:04 に手動 `ingest_today_lines.py` 実行 → 6 場 442 entries 救出
2. daemon の retry ロジック (50 分粘る) が自動的に 08:05 で entries 検出 →
   126 captures (42 races × 5/3/2 offset) スケジュール成功
3. **恒久修正**: `ingest_today_lines.py` に JSJ048 retry 追加
   - cron 時 (引数なし) のみ最大 12 回 × 300 秒 = 60 分 retry
   - 当日 kaisaiDate が出るまで粘る (daemon と同じ思想)
   - 手動日付指定時は retry しない
   - 環境変数 KEIRIN_LINES_RETRY_MAX / KEIRIN_LINES_RETRY_SEC で調整可

### 教訓
- **AGENTS.md / CLAUDE.md の「センセーショナルな結果は測定バグを疑う」が的中**
- results (明示日付) と lines (JSJ048 today 依存) の二重系があったから即検証できた
- 「No venues open」を 4 日も見逃した = ログ監視の甘さ。weekly_drift_report の
  「未蓄積」アラートをもっと早く確認すべきだった
- 観測基盤を作っても、その上流 (lines 取得) が壊れていたら無意味。
  Gemini の「観測基盤こそ最優先」がより重い意味を持つ

### Phase 6 への影響
- 蓄積開始が実質 5/16 から (5/13〜5/15 のロスは取り返せない)
- n=30/n=50 到達が当初想定より 3 日遅れ
- 5/16 から正常蓄積開始、daemon は 5/3/2 offset で 126 captures/日ペース

---

## 2026-05-15: Gemini 中盤レビュー受領 → Phase 6 方針 PIVOT

### Gemini からの衝撃的な指摘
`Opinion/GeminiOpinion.md` 2026-05-15 エントリ。姉妹プロジェクトの最新状況
を踏まえた極めて重要な大局観コメント。

#### 衝撃の情報源データ
- **Auto の最新事態 (5/14)**:
  - 発走 5 分前 EV ≥ 1.50 の買い目が、確定オッズでは **57% が EV < 1.50 に転落**
  - ROI 132% → **105%** に低下
  - 急遽 `LEAD_MIN=5 → 2` に短縮パッチを昨日適用
- **Boat の現状 (5/26 SG 笹川賞前)**:
  - **ML 修正を Freeze**、`scripts/ehi_monitor.py` で市場の歪み (EHI) リアルタイム監視に移行
  - 「予測が当たるか」ではなく「市場環境が健全か」で運用可否を判断

#### Gemini 核心メッセージ
> 今の Keirin は、Auto/Boat が 2 週間前に通過した「**幻の Upper-bound ROI**」に
> 喜んでいる状態である。数日後に pre-start odds での絶望的な性能低下を目の当たり
> にするだろう。

### Phase 6 評価軸の PIVOT

**Before (5/12〜5/14)**:
- 評価軸: AUC 改善 / ROI 改善 (upper-bound 性能の押し上げ)
- 戦略: 特徴量段階投入 (ST2/SH2 → W → RH3/RT3 ...)
- 単一モデルで全券種

**After (5/15〜)**:
- 評価軸: **pre-start odds drift の縮小 + 観測基盤の堅牢性**
- 戦略: **観測 → drift 監視 → 多モデル化 → EHI 監視**
- **多モデル化** (順序系 / 無順序系の分離)
- **no_odds_model + odds_for_EV_only を本番主軸** (Codex 第 3 ルート格上げ)

### 計画修正 (Gemini 提案を全面採用)

| 旧計画 | 新計画 | 理由 |
|---|---|---|
| RH3/RT3 段階投入 (N5) | **中止** | 市場過学習を深めるだけ、drift で消える |
| 単一 LightGBM 全券種 | **多モデル化** (順序系 vs 非順序系) | W で三連単悪化 = 単一モデル限界 |
| LEAD_MIN=5 固定 | **5/3/2 分パラメータ化** | auto は昨日 5→2 短縮、柔軟性必要 |
| 蓄積待ちで休む | **drift report skeleton 実装** | 蓄積 0 でも箱を作って運用ログ集約 |
| odds ありモデル本線 | **odds なしモデル本線**、odds は EV 計算のみ | Drift に影響されない本線 |
| n=30〜50 で shadow 開始 | **n=30 で drift 定量化開始**、n=50 で戦略 freeze | 蓄積待ちは「監査期間」 |

### 自分の反省
- 私の予定していた N5 (RH3/RT3 追加) は **明確に間違い**だった
- 多モデル化の発想がなかった = 大局観の盲点
- Codex の第 3 ルート提案 (5/12 brief) を「サブ案」扱いしていたが、これが本命だった
- Gemini が 2 度目のレビューで「Auto の最新事態 ROI 132→105」を持ち込んでくれたことで、
  「市場 80% 依存」の本当の意味がやっと見えた

### 直近の作業順 (ユーザー承認 2026-05-15)
1. **Q1**: 本エントリで pivot 記録 (完了)
2. **Q5**: Codex に多モデル化 + odds 降格の妥当性レビュー依頼
3. **Q2**: LEAD_MIN パラメータ化 (daemon 改修、低リスク高リターン)
4. Codex 回答後に **Q3** (drift report skeleton) と **Q4** (多モデル化設計) を着手

### TODO ロックリスト (5/15 時点)
- ❌ RH3/RT3 段階投入 (中止)
- ❌ Mispricing 系再試行 (中止、5/13 失敗確定)
- ⚠️ AUC/ROI の upper-bound 数値を「進捗」として喜ばない (Gemini 警告)
- 🟢 観測基盤強化が最優先

---

## 2026-05-13 (2): N2 W odds 追加 成功 + 段階投入のまとめ

### N2: W (ワイド) odds 特徴量追加 — 成功

`build_odds_features.py` 拡張で W から 5 特徴量を抽出:
- `imp_top3prob_w`: W contains-car の 3 着内圏 marginal (正規化)
- `pop_rank_w_min`: W 含む組合せの min popularity
- `w_odds_min_avg`: W min odds 平均 (下限期待値)
- `w_odds_range_avg`: W (max - min) odds 平均 (不確実性指標)
- `has_w_odds`: 欠損フラグ

`ml_baseline.py` に `--use-w-odds` フラグ追加。

### 結果 (compare_w_odds_compare_report.md)

**AUC**:
| Target | with_odds | with_odds_w | Δ |
|---|---|---|---|
| y_win | 0.8505 | 0.8506 | +0.0001 |
| y_top2 | 0.8255 | 0.8257 | +0.0002 |
| y_top3 | 0.7889 | **0.7942** | **+0.0053** ⭐ |
| y_rank MAE | 1.3853 | **1.3724** | **-0.0129 改善** ⭐ |

→ y_top3 で AUC +0.005 = W は 3 着内予測に効く (Codex 予想通り)

**Top 5% ROI 改善 (with_odds_w - with_odds)**:
| 券種 | top5% | top10% | top25% |
|---|---|---|---|
| 二車単 | +0.035 ⭐ | +0.008 | +0.002 |
| 三連複 | +0.033 ⭐ | +0.026 ⭐ | +0.018 ⭐ |
| ワイド | +0.023 ⭐ | +0.019 ⭐ | +0.006 |
| 三連単 | -0.008 | -0.049 ⚠️ | -0.027 |

→ 三連複/ワイド/二車単で一貫改善、**三連単のみ悪化**

**的中率改善**:
- 三連複: 0.2244 → 0.2436 (+0.019、相対 +8.5%)
- ワイド: 0.411 → 0.4222 (+0.011)

### 三連単悪化の解釈
W 情報でモデルが「3 着内」予測に最適化された結果、「1 → 2 → 3 の順序」判定が
弱まった可能性。「順序予測には W は不向き」という trade-off。

### Phase 6 特徴量段階投入のサマリ (5/12〜5/13)

| 試行 | 内容 | AUC 寄与 | ROI 寄与 | 評価 |
|---|---|---|---|---|
| ST2 + SH2 | 第 1 段、Codex 6 候補 | y_win +0.039 / y_top2 +0.046 | 二車単/三連単 top10% +0.10 | ✅ 採用 |
| 弱ライン特徴 | nige_propensity 等 6 候補 | ほぼゼロ | top5% 二車単 +0.064 のみ | △ 補助的 |
| Mispricing | rank diff 系 6 候補 | ゼロ | 三連単 -0.024 (悪化) | ❌ 採用見送り |
| W odds | 第 2 段、Codex 5 候補 | y_top3 +0.005 | 三連複/ワイド top5% +0.02-0.03 | ✅ 採用 |

採用基準: **AUC か ROI のいずれかで一貫改善** + 既存特徴量にない情報。

### 学び
1. **市場系特徴量 (ST2/SH2/W) は強い**: 上限性能押し上げに直接効く
2. **派生・組み合わせ特徴量 (mispricing) は LightGBM が既に内部学習している**
3. **弱特徴量は AUC では無価値、top X% で限定的に効く**
4. **trade-off に注意**: W で y_top3 向上 ↔ 三連単悪化
5. **段階投入は正解**: 一気に入れずに ablation で寄与度を見ると trade-off が判明

### 次のステップ (P4 = 休む)
今日のセッション終了。明日 5/14 (木) 朝の daemon 起動状況確認が次の優先事項。

5/14 確認項目:
1. `logs/cron_prerace_daemon_2026-05-14.log` が生成されるか
2. 5/14 開催が複数あれば snapshot 蓄積開始
3. `race_odds_prerace.csv` に行が積み上がるか
4. `prerace_snapshot_log.csv` の status 分布

### TODO (時間あれば)
- RH3 / RT3 odds 特徴量追加で y_top3/y_rank をさらに強化検討
- ただし市場依存度が更に上がる → 本番リーケージ問題が拡大する懸念
- pre-start odds 蓄積後の honest backtest が本命

---

## 2026-05-13: N3 Mispricing 失敗 + Feature Importance 分析

### Feature Importance 分析 (M1)
`analyze_feature_importance.py` で with_odds_line 構成のモデルから gain
importance を抽出 (`feature_importance_report.md` 参照)。

**衝撃の発見**: モデルは 80%+ 市場依存:
- y_win の 81.83% は `imp_winprob_st2` で説明
- y_top2 の 84.30% は `imp_top2prob_sh2` で説明
- y_top3 の 79.49% は `imp_top2prob_sh2` で説明

弱ライン特徴量は total 1% 未満:
- `nige_propensity` 0.97% (唯一の生存)
- 他の 5 特徴は全部 0.25% 未満
- `has_clear_regional_partner` / `is_likely_tanki` はほぼゼロ

→ Gemini 予言通り「市場の蒸留モデル」になっている

### N3 Mispricing 試行と失敗
市場 vs 戦績の食い違いを派生特徴量化:
- `pop_minus_syouritu_rank` (rank-of-rank 正規化)
- `pop_minus_rentairitu2_rank` / `pop_minus_heikin_tokuten_rank`
- `imp_winprob_residual` (確率残差)
- `is_underpriced_stats_top` / `is_overpriced_market_top` フラグ

with_odds vs with_odds_mp 比較結果:

| 指標 | 結果 |
|---|---|
| AUC 動き | ほぼゼロ (±0.0001) |
| 全レース 三連単 ROI | **-0.024 悪化** |
| top 5% 三連単 ROI | **+0.027 改善** |
| top 25% 三連単 ROI | -0.028 悪化 |

→ 一貫した improvement なし、過学習傾向あり

**結論: mispricing 採用見送り**

### 失敗の解釈
- 単純な rank diff では LightGBM がすでに内部で学習している
- 既存 odds + stats が同情報を吸収済み
- 「市場の歪み」をもっと深く設計しないと edge は出ない
  (例: 過去 N レース内での同条件成績、最有力候補とのギャップ等)

### 教訓
- 派生特徴量は「LightGBM が組み合わせ学習で取れない情報」でないと意味なし
- rank diff は素直すぎる
- 試行錯誤コストは半日 + 7-8 分 compute、得るものは「方向性として違う」確認

### 次のアクション (5/13 夜)
N2: W (ワイド) odds 特徴量を追加 (当初予定通り Codex 段階投入の次フェーズ)。
y_top3 で効く可能性。market の蒸留度はさらに上がる懸念もあるが、ablation で見る。

---

## 2026-05-12 (7): Phase 6 中間まとめ (1 日の成果整理)

### 今日 1 日の成果サマリ

長丁場だった (Drive 事故対応 → AI 協働導入 → Phase 6 着手 → cron UX 改善)。
データロスは完全復旧し、Phase 6 観測基盤と ML 拡張がひと段落。

#### データ復旧 (5/10〜5/12 早朝)
- Google Drive Stream の "Free up space" が race_odds.csv (2.5GB) を破壊
- Drive `lost_and_found` から救出 + 39 時間の再 backfill で完全復活
- DATA_DIR をローカル D:\keirin-ai-data に移動、Drive は ZIP バックアップ専用に変更
- CLAUDE.md / scripts/backup_to_drive.py で運用ルール明文化

#### AI 協働体制導入 (5/12)
- Claude / Codex / Gemini の 3 体制を Auto_racing_AI から移植
- AGENTS.md / GEMINI.md / Opinion/README.md / 各意見ファイル整備
- Codex / Gemini 双方から Phase 6 設計レビュー受領

#### Phase 6 観測基盤 + ML 拡張
- `ingest_odds_prerace_daemon.py`: 朝 8:00 起動の pre-start odds snapshot daemon
- `build_odds_features.py`: race_odds.csv → odds_features.csv (958k 行、2 分)
- `build_line_weak_features.py`: race_entries + race_stats → line_features.csv
- `ml_baseline.py` 拡張: --use-odds / --use-line フラグで 4-way 比較対応
- `compare_4way.py`: base / +line / +odds / +odds+line を自動比較

### 4-way 比較結果 (D:\keirin-ai-data\compare_4way_report.md)

#### AUC (y_win) per config
| Config | AUC | Δ vs base |
|---|---|---|
| base | 0.8115 | — |
| +line | 0.8116 | +0.0001 (実質ゼロ) |
| +odds | 0.8505 | +0.039 |
| +odds+line | 0.8506 | +0.0001 vs odds |

#### 全レース ROI 差分 (line 純効果、with_line - base)
| 券種 | Δ ROI |
|---|---|
| 二車複 | -0.007 |
| 二車単 | -0.011 |
| 三連複 | -0.003 |
| 三連単 | +0.003 |
| ワイド | +0.001 |

→ ノイズレベル、全レース戦略では line 不要

#### 高確信レース (top 5%) では line が効く
| 券種 | base top5% | +line top5% | Δ |
|---|---|---|---|
| 二車単 | -0.197 | **-0.133** | +0.064 ⭐ |
| 三連単 | -0.322 | -0.288 | +0.034 |
| ワイド | -0.173 | -0.166 | +0.007 |

→ 弱ラインは「予測の校正」役割。高確信レース絞り込みで価値あり。

### 解釈と次の方針

1. **既存の `kyaku` / `prefecture` 特徴量が地区+脚質情報をほぼ吸収済み**
   - LightGBM が既に「同地区+追い」を組み合わせ学習している
   - 弱ライン特徴量を明示的に渡しても新情報なし

2. **odds は明確に有用、ただし upper-bound**
   - 二車単 top10%/25% で +0.1 以上の ROI 改善 (リーケージあり)
   - pre-start snapshot 蓄積後に honest backtest で再評価

3. **本物のライン構造モデルは公式 race_lines.csv 蓄積を待つ**
   - 弱特徴量では届かない領域 (Gemini 指摘の「ラインは構造情報」)
   - 数ヶ月分の蓄積後に line_id 直接利用モデルを試行

### Phase 6 進捗ステータス (5/12 23:30 時点)

| 段階 | 内容 | 状態 |
|---|---|---|
| 6a-1 | 観測基盤 (daemon + snapshot log + schema) | ✅ 完了、cron 登録済 |
| 6a-2 | 弱ライン特徴量 | ✅ 完了、補助的価値あり |
| 6a-3 | upper-bound 検証 (odds 3-way 比較) | ✅ 完了、odds 有効性確認 |
| 6b | snapshot + shadow 蓄積 | 🕒 明日 5/13 朝から自動開始 |
| 6c | shadow → live 移行判断 | 🕒 n=30〜50 達成後 (6 月下旬) |

### 明日 5/13 以降の確認事項
1. 朝 8:00 daemon が正常起動するか (logs/cron_prerace_daemon_2026-05-13.log)
2. race_odds_prerace.csv に snapshot 行が積み上がるか
3. prerace_snapshot_log.csv に status=ok が増えていくか
4. 数日後: snapshot 取得タイミングのズレ (minutes_before_start 分布) を監査

### 反省
- cron UX (毎分起動 → CMD 点滅) を最初に考慮できず、ユーザーに指摘された
- 「実装が楽」だけで判断せず、運用上の影響を必ず確認する
- Auto_racing_AI に dynamic_scheduler.py という先行事例があったのに参照不足
- 今後同様の設計時は UX への影響を必ず確認してから提案する

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
