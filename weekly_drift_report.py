"""Weekly drift monitoring report (Phase 6 R3, 2026-05-15)

pre-start odds snapshot の蓄積状況と、live odds / final odds の drift を
週次で監視する。Codex 提案 P2 / Gemini 提案の EHI 監視に相当。

設計:
- 集計範囲: 過去 7 日間 (--days で変更可)
- 入力:
    - race_odds_prerace.csv (live snapshot、target_offset_min 別)
    - race_odds.csv (final-like odds、post-start)
    - prerace_snapshot_log.csv (取得試行ログ)
- 出力指標 (offset 別):
    - 蓄積カバレッジ: n_ok / n_no_odds / n_fail per offset
    - actual minutes_before_start の分布 (mean, std)
    - drift: live_odds vs final_odds の差分 (蓄積後、ST2 1着付け min odds で)
    - market health: 1 番人気 popularity の集中度 (boat の EHI 風)
- 蓄積が n<10 の段階では「未蓄積」と明示し、表は空でも箱は作る

使い方:
  python weekly_drift_report.py             # 過去 7 日
  python weekly_drift_report.py --days 30   # 過去 30 日
  python weekly_drift_report.py --since 2026-05-13  # 開始日指定

⚠️ 5/15 時点では snapshot 未蓄積。skeleton として空レポートを生成する。
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev

from src.config import DATA_DIR

sys.stdout.reconfigure(encoding="utf-8")

REPORT_PATH = DATA_DIR / "weekly_drift_report.md"

# Windows schtasks の正常コード:
# 0 = success / 267009 = currently running / 267011 = last result not set yet
CRON_OK_RESULTS = {"0", "-", "267009", "267011"}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="過去 N 日を対象 (default 7)")
    ap.add_argument("--since", default=None,
                    help="集計開始日 (YYYY-MM-DD)、--days より優先")
    ap.add_argument("--out", default=str(REPORT_PATH))
    ap.add_argument("--mail", action="store_true",
                    help="生成したレポートを .env の NOTIFY_TO へメール送信")
    return ap.parse_args()


def get_date_range(args) -> tuple[date, date]:
    today = date.today()
    if args.since:
        start = date.fromisoformat(args.since)
    else:
        start = today - timedelta(days=args.days - 1)
    return start, today


def load_snapshot_log(start: date, end: date) -> list[dict]:
    path = DATA_DIR / "prerace_snapshot_log.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rd = date.fromisoformat(r["race_date"])
            except (ValueError, KeyError):
                continue
            if start <= rd <= end:
                rows.append(r)
    return rows


def load_prerace_odds(start: date, end: date) -> list[dict]:
    """live snapshot odds (race_odds_prerace.csv) を期間で絞り込み読み込み。

    ⚠️ 蓄積が進むと race_odds_prerace.csv は数 GB になり得る。
    最初の skeleton では全行読みで OK だが、後で chunk 読みに切り替えたい。
    """
    path = DATA_DIR / "race_odds_prerace.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rd = date.fromisoformat(r["race_date"])
            except (ValueError, KeyError):
                continue
            if start <= rd <= end:
                rows.append(r)
    return rows


def _parse_offset(raw) -> float | int:
    """target_offset_min を float でパースし、整数値は int に正規化 (5.0→5, 0.5→0.5)。"""
    f = float(raw)
    return int(f) if f.is_integer() else f


def summarize_coverage(log_rows: list[dict]) -> dict[float, dict[str, int]]:
    """offset 別の status counts を集計。"""
    by_offset: dict[float, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in log_rows:
        try:
            off = _parse_offset(r["target_offset_min"])
        except (ValueError, KeyError, TypeError):
            # 旧スキーマ (target_offset_min 無し) は default 5 扱い
            off = 5
        status = r.get("status", "unknown")
        by_offset[off][status] += 1
    return {k: dict(v) for k, v in by_offset.items()}


def summarize_actual_offset(log_rows: list[dict]) -> dict[float, dict[str, float]]:
    """target_offset_min vs actual minutes_before_start を集計。

    狙いと実測のズレを定量化 (auto の LEAD_MIN 短縮判断の根拠データ)。
    """
    by_offset: dict[float, list[int]] = defaultdict(list)
    for r in log_rows:
        if r.get("status") != "ok":
            continue
        try:
            off = _parse_offset(r["target_offset_min"])
            actual = int(r["minutes_before_start"])
        except (ValueError, KeyError, TypeError):
            continue
        by_offset[off].append(actual)
    out: dict[float, dict[str, float]] = {}
    for off, vals in by_offset.items():
        if not vals:
            out[off] = {"n": 0, "mean": float("nan"), "std": float("nan")}
            continue
        m = mean(vals)
        s = stdev(vals) if len(vals) > 1 else 0.0
        out[off] = {"n": len(vals), "mean": m, "std": s}
    return out


def get_daily_ingestion_health(start: date, end: date) -> list[dict]:
    """各日の各 CSV 行数 + 健全性ステータスを返す (auto の get_recent_days_status 風)。

    出力 (日付降順): [
      {date, venues, entries, lines, stats, results, payouts, snapshots_ok, status}
    ]
    """
    csv_keys = [
        ("venues", "race_meta.csv"),   # 1 場 = 1 row
        ("entries", "race_entries.csv"),
        ("lines", "race_lines.csv"),
        ("stats", "race_stats.csv"),
        ("results", "race_results.csv"),
        ("payouts", "payouts.csv"),
    ]
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {k: 0 for k, _ in csv_keys}
    )

    # 各 CSV を date 列で集計 (重い race_odds_prerace.csv は除外)
    for label, fname in csv_keys:
        path = DATA_DIR / fname
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rd = r.get("race_date", "")
                    if not rd:
                        continue
                    try:
                        d = date.fromisoformat(rd)
                    except ValueError:
                        continue
                    if start <= d <= end:
                        counts[rd][label] += 1
        except Exception:
            pass  # 読めなくても他の集計は続ける

    # snapshot 数は snapshot_log の status=ok から取る (race_odds_prerace.csv 全読み回避)
    log_path = DATA_DIR / "prerace_snapshot_log.csv"
    snap_ok: dict[str, int] = defaultdict(int)
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rd = r.get("race_date", "")
                if not rd:
                    continue
                try:
                    d = date.fromisoformat(rd)
                except ValueError:
                    continue
                if start <= d <= end and r.get("status") == "ok":
                    snap_ok[rd] += 1

    today = date.today()
    rows = []
    d = end
    while d >= start:
        ds = d.isoformat()
        c = counts.get(ds, {k: 0 for k, _ in csv_keys})
        snaps = snap_ok.get(ds, 0)
        # status 判定
        if c["entries"] == 0:
            status = "❌ NO_DATA"
        elif d == today:
            status = "🟡 TODAY (進行中)"
        elif c["results"] == 0:
            status = "⚠️ RESULTS_MISSING"
        elif c["lines"] == 0:
            status = "⚠️ LINES_MISSING"
        elif snaps == 0:
            status = "⚠️ NO_SNAPSHOT"
        else:
            status = "✅ OK"
        rows.append({
            "date": ds,
            "venues": c["venues"],
            "entries": c["entries"],
            "lines": c["lines"],
            "stats": c["stats"],
            "results": c["results"],
            "payouts": c["payouts"],
            "snapshots_ok": snaps,
            "status": status,
        })
        d -= timedelta(days=1)
    return rows


def check_cron_health() -> list[dict]:
    """schtasks /query で各 cron task の Last Run / Last Result / Next Run を取得。"""
    tasks = [
        "keirin-ai-results",
        "keirin-ai-lines",
        "keirin-ai-prerace-daemon",
        "keirin-ai-weekly-report",
    ]
    # 日本語 / 英語ロケール両対応の key set
    key_aliases = {
        "Last Run Time": "last_run", "前回の実行時刻": "last_run",
        "Last Result": "last_result", "前回の結果": "last_result",
        "Next Run Time": "next_run", "次回の実行時刻": "next_run",
        "Status": "status", "状態": "status",
    }
    out = []
    for t in tasks:
        info = {"task": t, "last_run": "-", "last_result": "-",
                "next_run": "-", "status": "-"}
        try:
            raw = subprocess.run(
                ["schtasks", "/query", "/tn", t, "/v", "/fo", "list"],
                capture_output=True, timeout=10,
            )
            text = raw.stdout.decode("cp932", errors="replace")
            for line in text.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k in key_aliases:
                    info[key_aliases[k]] = v
        except Exception as e:
            info["status"] = f"query failed: {e}"
        out.append(info)
    return out


def _md_daily_health(start: date, end: date) -> list[str]:
    """セクション 0: データスクレイピング日次ヘルス。"""
    L: list[str] = []
    L.append("## 0. データスクレイピング 日次ヘルス (auto/boat 形式)")
    L.append("")
    daily = get_daily_ingestion_health(start, end)
    if not daily:
        L.append("(集計不能)")
    else:
        L.append("| 日付 | 場 | entries | lines | stats | results | payouts | snapshots | status |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for d in daily:
            L.append(
                f"| {d['date']} | {d['venues']} | {d['entries']} | "
                f"{d['lines']} | {d['stats']} | {d['results']} | "
                f"{d['payouts']} | {d['snapshots_ok']} | {d['status']} |"
            )
    L.append("")
    L.append("**status 凡例**: ✅ OK / 🟡 TODAY (進行中) / "
             "⚠️ LINES_MISSING (narabi 取り逃し) / "
             "⚠️ RESULTS_MISSING / ⚠️ NO_SNAPSHOT / ❌ NO_DATA")
    L.append("")
    return L


def _md_cron_health() -> list[str]:
    """セクション 0-2: cron タスク死活。"""
    L: list[str] = []
    L.append("## 0-2. cron タスク死活")
    L.append("")
    cron_status = check_cron_health()
    L.append("| Task | Last Run | Last Result | Next Run | Status |")
    L.append("|---|---|---|---|---|")
    for c in cron_status:
        ok_marker = "✅" if c["last_result"] in CRON_OK_RESULTS else "⚠️"
        L.append(
            f"| {c['task']} | {c['last_run']} | "
            f"{ok_marker} {c['last_result']} | {c['next_run']} | {c['status']} |"
        )
    L.append("")
    L.append("**Last Result**: 0=成功 / 267009=実行中 (daemon は朝〜夜走る) / "
             "267011=未実行。それ以外はエラー。"
             "**Last Run が古すぎる** = cron 止まっている疑い。")
    L.append("")
    return L


def _md_coverage(log_rows: list[dict]) -> list[str]:
    """セクション 1: 蓄積カバレッジ (offset 別 status 分布)。"""
    L: list[str] = []
    L.append("## 1. 蓄積カバレッジ (offset 別 status 分布)")
    L.append("")
    if not log_rows:
        L.append("⚠️ **未蓄積**: `prerace_snapshot_log.csv` に対象期間のデータが無い。")
        L.append("- 5/13 開催なし、5/14 から daemon 稼働開始")
        L.append("- 開催あり日が来れば自動的に行が増える")
    else:
        cov = summarize_coverage(log_rows)
        L.append("| offset (分前) | ok | no_odds | fail | total |")
        L.append("|---:|---:|---:|---:|---:|")
        for off in sorted(cov.keys(), reverse=True):
            s = cov[off]
            ok = s.get("ok", 0)
            no_odds = s.get("no_odds", 0)
            fail = s.get("fail", 0)
            total = ok + no_odds + fail
            L.append(f"| {off} | {ok} | {no_odds} | {fail} | {total} |")
    L.append("")
    return L


def _md_timing(log_rows: list[dict]) -> list[str]:
    """セクション 2: 取得タイミング精度。"""
    L: list[str] = []
    L.append("## 2. 取得タイミング精度 (target_offset_min vs 実測 minutes_before_start)")
    L.append("")
    if not log_rows:
        L.append("⚠️ 未蓄積。蓄積開始後に offset 別の実測ズレ (mean ± std) を表示。")
    else:
        timing = summarize_actual_offset(log_rows)
        L.append("| offset (狙い) | n_ok | 実測 mean (分前) | 実測 std |")
        L.append("|---:|---:|---:|---:|")
        for off in sorted(timing.keys(), reverse=True):
            t = timing[off]
            L.append(f"| {off} | {t['n']} | {t['mean']:.2f} | {t['std']:.2f} |")
    L.append("")
    return L


def _md_drift(odds_rows: list[dict]) -> list[str]:
    """セクション 3: live odds vs final odds drift。"""
    L: list[str] = []
    L.append("## 3. live odds ↔ final odds drift")
    L.append("")
    if not odds_rows:
        L.append("⚠️ 未蓄積。蓄積開始後、target_offset_min 別に")
        L.append("`live_odds_min_st2_1st` と `final_odds_min_st2_1st` の比較表を表示。")
        L.append("")
        L.append("期待される表 (将来):")
        L.append("```")
        L.append("| offset | n_races | live_ev_mean | final_ev_mean | drift_mean |")
        L.append("|        | _ratio  |              |               | (live-final)|")
        L.append("```")
    else:
        # TODO: race_odds.csv (final) と race_odds_prerace.csv (live) を join し、
        #       ST2 1着付け min odds をレース別 + offset 別で比較する集計
        L.append(f"prerace odds 行数: {len(odds_rows):,} (集計ロジックは蓄積後実装)")
    L.append("")
    return L


def _md_market_health(odds_rows: list[dict]) -> list[str]:
    """セクション 4: Market Health (EHI 風)。"""
    L: list[str] = []
    L.append("## 4. Market Health (EHI: Edge Health Index)")
    L.append("")
    if not odds_rows:
        L.append("⚠️ 未蓄積。蓄積開始後、Boat の `ehi_monitor.py` を参考に以下を表示:")
        L.append("- 1 番人気の過剰投票 (Overround) 検出")
        L.append("- 市場全体の implied prob 合計が 1.0 から離れる度合い")
        L.append("- 単独人気 1 倍台レース比率")
    else:
        L.append("(EHI 計算は蓄積後実装)")
    L.append("")
    return L


def _md_actions(log_rows: list[dict]) -> list[str]:
    """セクション 5: アクション推奨 + フッタ。"""
    L: list[str] = []
    L.append("## 5. アクション推奨")
    L.append("")
    n_ok_total = sum(
        v.get("ok", 0) for v in summarize_coverage(log_rows).values()
    ) if log_rows else 0
    L.append(f"- 現在の累計 ok snapshot 数: **{n_ok_total}**")
    if n_ok_total < 30:
        L.append("- 🟡 蓄積期 (n<30): drift 評価不能、観測継続")
    elif n_ok_total < 50:
        L.append("- 🟡 監査期 (n=30〜50): drift 定量化開始、戦略はまだ固定しない")
    else:
        L.append("- 🟢 戦略仮固定期 (n≥50): 券種/モデル/EV 閾値の暫定 freeze を検討")
    L.append("")
    L.append("---")
    L.append("生成: `python weekly_drift_report.py`")
    return L


def render_report(start: date, end: date,
                  log_rows: list[dict],
                  odds_rows: list[dict]) -> str:
    """マークダウンレポート文字列を生成。"""
    L: list[str] = []
    L.append(f"# Weekly Drift Report ({start.isoformat()} ~ {end.isoformat()})")
    L.append("")
    L.append(f"生成時刻: {datetime.now().isoformat(timespec='seconds')}")
    L.append("")
    L.append("Phase 6 R3 (Codex 5/15 提案 P2、Gemini EHI 監視に相当)。")
    L.append("")
    L.extend(_md_daily_health(start, end))
    L.extend(_md_cron_health())
    L.extend(_md_coverage(log_rows))
    L.extend(_md_timing(log_rows))
    L.extend(_md_drift(odds_rows))
    L.extend(_md_market_health(odds_rows))
    L.extend(_md_actions(log_rows))
    return "\n".join(L)


# ─── HTML レポート共通 inline スタイル (Gmail で剥がされない) ───────────
HTML_TBL = ('border="1" cellpadding="6" cellspacing="0" '
            'style="border-collapse:collapse; border-color:#bbb; '
            'font-family:Arial,sans-serif; font-size:13px; margin-bottom:8px;"')
HTML_TH = ('style="background:#e8e8e8; text-align:left; padding:6px 10px; '
           'font-weight:bold; border:1px solid #bbb;"')
HTML_TH_R = ('style="background:#e8e8e8; text-align:right; padding:6px 10px; '
             'font-weight:bold; border:1px solid #bbb;"')
HTML_TD_L = 'style="text-align:left; padding:6px 10px; border:1px solid #ddd;"'
HTML_TD_R = 'style="text-align:right; padding:6px 10px; border:1px solid #ddd;"'
HTML_ROW_ALT = 'style="background:#fafafa;"'


def _html_header(start: date, end: date, daily: list[dict],
                 n_snap_total: int) -> list[str]:
    """div 開始 + タイトル h2 + サマリ行 (件名と整合)。"""
    n_total = len(daily)
    n_ok_days = sum(1 for d in daily if d["status"].startswith("✅"))
    n_warn = sum(1 for d in daily if d["status"].startswith("⚠️"))
    n_nodata = sum(1 for d in daily if d["status"].startswith("❌"))
    if n_nodata > 0:
        overall = ("🔴 異常", "#c62828")
    elif n_warn > 0:
        overall = ("🟡 要確認", "#f9a825")
    else:
        overall = ("🟢 正常", "#2e7d32")

    p: list[str] = []
    p.append('<div style="font-family:Arial,sans-serif; font-size:14px; '
             'color:#222; line-height:1.55; max-width:880px;">')
    p.append(
        f'<h2 style="color:{overall[1]}; margin:0 0 12px 0; '
        f'padding-bottom:6px; border-bottom:2px solid {overall[1]};">'
        f'🚴 keirin-ai 週次 drift report &nbsp; '
        f'<span style="color:#222; font-weight:normal;">{start} 〜 {end}</span>'
        f' &nbsp; <span style="font-weight:normal;">{overall[0]}</span></h2>'
    )
    p.append(
        f'<p style="margin:0 0 12px 0; color:#555;">'
        f'OK <b>{n_ok_days}</b>/{n_total}日 &nbsp;|&nbsp; '
        f'⚠️ 警告 <b>{n_warn}</b>日 &nbsp;|&nbsp; '
        f'❌ ロスト <b>{n_nodata}</b>日 &nbsp;|&nbsp; '
        f'累計 snapshots <b>{n_snap_total:,}</b></p>'
    )
    return p


def _html_daily_health(daily: list[dict]) -> list[str]:
    """セクション 0: データスクレイピング日次ヘルス。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '0. データスクレイピング日次ヘルス</h3>')
    p.append(f'<table {HTML_TBL}>')
    p.append(
        f'<tr><th {HTML_TH}>日付</th><th {HTML_TH_R}>場</th>'
        f'<th {HTML_TH_R}>entries</th><th {HTML_TH_R}>lines</th>'
        f'<th {HTML_TH_R}>stats</th><th {HTML_TH_R}>results</th>'
        f'<th {HTML_TH_R}>payouts</th><th {HTML_TH_R}>snapshots</th>'
        f'<th {HTML_TH}>status</th></tr>'
    )
    for i, d in enumerate(daily):
        alt = HTML_ROW_ALT if i % 2 == 1 else ""
        if d["status"].startswith("✅"):
            color = "#2e7d32"
        elif d["status"].startswith("🟡"):
            color = "#666"
        elif d["status"].startswith("⚠️"):
            color = "#f57f17"
        else:
            color = "#c62828"
        status_cell = (f'<td {HTML_TD_L} style="text-align:left; padding:6px 10px; '
                       f'border:1px solid #ddd; color:{color}; '
                       f'font-weight:bold;">{d["status"]}</td>')
        p.append(
            f'<tr {alt}>'
            f'<td {HTML_TD_L}>{d["date"]}</td>'
            f'<td {HTML_TD_R}>{d["venues"]}</td>'
            f'<td {HTML_TD_R}>{d["entries"]:,}</td>'
            f'<td {HTML_TD_R}>{d["lines"]:,}</td>'
            f'<td {HTML_TD_R}>{d["stats"]:,}</td>'
            f'<td {HTML_TD_R}>{d["results"]:,}</td>'
            f'<td {HTML_TD_R}>{d["payouts"]:,}</td>'
            f'<td {HTML_TD_R}>{d["snapshots_ok"]:,}</td>'
            f'{status_cell}'
            f'</tr>'
        )
    p.append('</table>')
    p.append('<p style="color:#666; font-size:12px; margin:4px 0 0 0;">'
             '✅ OK / 🟡 TODAY (進行中) / '
             '⚠️ LINES_MISSING (narabi 取り逃し) / '
             '⚠️ RESULTS_MISSING / ⚠️ NO_SNAPSHOT / ❌ NO_DATA</p>')
    return p


def _html_cron_health(cron_status: list[dict]) -> list[str]:
    """セクション 0-2: cron タスク死活。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '0-2. cron タスク死活</h3>')
    p.append(f'<table {HTML_TBL}>')
    p.append(f'<tr><th {HTML_TH}>Task</th><th {HTML_TH}>Last Run</th>'
             f'<th {HTML_TH}>Last Result</th><th {HTML_TH}>Next Run</th>'
             f'<th {HTML_TH}>Status</th></tr>')
    for i, c in enumerate(cron_status):
        alt = HTML_ROW_ALT if i % 2 == 1 else ""
        is_ok = c["last_result"] in CRON_OK_RESULTS
        mark = "✅" if is_ok else "⚠️"
        color = "#2e7d32" if is_ok else "#c62828"
        p.append(
            f'<tr {alt}>'
            f'<td {HTML_TD_L}><b>{c["task"]}</b></td>'
            f'<td {HTML_TD_L}>{c["last_run"]}</td>'
            f'<td {HTML_TD_L} style="text-align:left; padding:6px 10px; '
            f'border:1px solid #ddd; color:{color};">'
            f'{mark} {c["last_result"]}</td>'
            f'<td {HTML_TD_L}>{c["next_run"]}</td>'
            f'<td {HTML_TD_L}>{c["status"]}</td>'
            f'</tr>'
        )
    p.append('</table>')
    p.append('<p style="color:#666; font-size:12px; margin:4px 0 0 0;">'
             'Last Result: 0=成功 / 267009=実行中 (daemon は朝〜夜稼働) / '
             '267011=未実行 / それ以外=エラー</p>')
    return p


def _html_coverage(cov: dict[float, dict[str, int]]) -> list[str]:
    """セクション 1: 蓄積カバレッジ。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '1. 蓄積カバレッジ (offset 別 status 分布)</h3>')
    if not cov:
        p.append('<p style="color:#999;">⚠️ 未蓄積</p>')
    else:
        p.append(f'<table {HTML_TBL}>')
        p.append(f'<tr><th {HTML_TH_R}>offset (分前)</th><th {HTML_TH_R}>ok</th>'
                 f'<th {HTML_TH_R}>no_odds</th><th {HTML_TH_R}>fail</th>'
                 f'<th {HTML_TH_R}>total</th></tr>')
        for off in sorted(cov.keys(), reverse=True):
            s = cov[off]
            ok = s.get("ok", 0); no_odds = s.get("no_odds", 0); fail = s.get("fail", 0)
            total = ok + no_odds + fail
            p.append(
                f'<tr><td {HTML_TD_R}><b>{off}</b></td><td {HTML_TD_R}>{ok}</td>'
                f'<td {HTML_TD_R}>{no_odds}</td><td {HTML_TD_R}>{fail}</td>'
                f'<td {HTML_TD_R}>{total}</td></tr>'
            )
        p.append('</table>')
    return p


def _html_timing(timing: dict[float, dict[str, float]]) -> list[str]:
    """セクション 2: 取得タイミング精度。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '2. 取得タイミング精度 (target vs 実測)</h3>')
    if not timing:
        p.append('<p style="color:#999;">⚠️ 未蓄積</p>')
    else:
        p.append(f'<table {HTML_TBL}>')
        p.append(f'<tr><th {HTML_TH_R}>offset (狙い)</th><th {HTML_TH_R}>n_ok</th>'
                 f'<th {HTML_TH_R}>実測 mean (分前)</th>'
                 f'<th {HTML_TH_R}>実測 std</th></tr>')
        for off in sorted(timing.keys(), reverse=True):
            t = timing[off]
            p.append(
                f'<tr><td {HTML_TD_R}><b>{off}</b></td>'
                f'<td {HTML_TD_R}>{t["n"]}</td>'
                f'<td {HTML_TD_R}>{t["mean"]:.2f}</td>'
                f'<td {HTML_TD_R}>{t["std"]:.2f}</td></tr>'
            )
        p.append('</table>')
    return p


def _html_actions(n_snap_total: int) -> list[str]:
    """セクション 3-4 (未実装表示) + 5 (アクション推奨) + フッタ + div 終了。"""
    p: list[str] = []
    # 3-4. drift / EHI は未実装メッセージのみ
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '3-4. live↔final drift / Market Health (EHI)</h3>')
    p.append('<p style="color:#999;">(蓄積進行後に実装予定)</p>')

    # 5. アクション
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">5. アクション推奨</h3>')
    if n_snap_total < 30:
        action = "🟡 蓄積期 (n&lt;30): drift 評価不能、観測継続"
    elif n_snap_total < 50:
        action = "🟡 監査期 (n=30〜50): drift 定量化開始、戦略は未固定"
    else:
        action = "🟢 戦略仮固定期 (n≥50): 暫定 freeze を検討"
    p.append(f'<p>累計 ok snapshot 数: <b>{n_snap_total:,}</b><br>{action}</p>')

    p.append(
        '<hr style="border:none; border-top:1px solid #ddd; '
        'margin:18px 0 8px 0;">'
        '<p style="color:#999; font-size:11px; margin:0;">'
        'keirin-ai weekly drift report</p>'
    )
    p.append('</div>')
    return p


def render_report_html(start: date, end: date,
                       log_rows: list[dict]) -> str:
    """Gmail で剥がされない inline-style 版 HTML レポート。

    text 版と同じ情報を見やすい色付き表で表示。
    """
    daily = get_daily_ingestion_health(start, end)
    cron_status = check_cron_health()
    cov = summarize_coverage(log_rows) if log_rows else {}
    timing = summarize_actual_offset(log_rows) if log_rows else {}
    n_snap_total = sum(d["snapshots_ok"] for d in daily)

    p: list[str] = []
    p.extend(_html_header(start, end, daily, n_snap_total))
    p.extend(_html_daily_health(daily))
    p.extend(_html_cron_health(cron_status))
    p.extend(_html_coverage(cov))
    p.extend(_html_timing(timing))
    p.extend(_html_actions(n_snap_total))
    return "\n".join(p)


def _send_mail_report(report: str, out_path: Path,
                      start: date, end: date,
                      log_rows: list[dict]) -> None:
    """--mail 指定時: サマリ入り件名 + HTML 版を付けてレポートをメール送信。"""
    try:
        from gmail_notify import send_email
    except Exception as e:
        print(f"⚠️ gmail_notify import 失敗: {e}", file=sys.stderr)
        return
    # メール本文は markdown 全文 (Gmail 上は等幅で読めるので十分)
    # 件名にサマリを入れる (健全日数 + snapshot 数で一目検知)
    n_ok = sum(
        v.get("ok", 0) for v in summarize_coverage(log_rows).values()
    ) if log_rows else 0
    daily_health = get_daily_ingestion_health(start, end)
    n_total = len(daily_health)
    n_ok_days = sum(
        1 for d in daily_health if d["status"].startswith("✅")
    )
    n_warn_days = sum(
        1 for d in daily_health if d["status"].startswith("⚠️")
    )
    n_no_data = sum(
        1 for d in daily_health if d["status"].startswith("❌")
    )
    # 件名: 全部 OK なら ✅、警告/エラーあれば ⚠️/❌
    if n_no_data > 0:
        mark = "❌"
    elif n_warn_days > 0:
        mark = "⚠️"
    else:
        mark = "✅"
    subject = (
        f"{mark} [keirin-ai] 週次 {start}~{end} "
        f"OK {n_ok_days}/{n_total}日 / snapshots {n_ok}"
    )
    # HTML 版を生成 (Gmail で見栄え良い表に)
    html_body = render_report_html(start, end, log_rows)
    try:
        send_email(
            subject=subject,
            body=report,
            html=html_body,
            attachments=[str(out_path)],
        )
    except Exception as e:
        print(f"⚠️ メール送信失敗: {e}", file=sys.stderr)


def main() -> None:
    args = parse_args()
    start, end = get_date_range(args)
    print(f"Range: {start} ~ {end}")

    log_rows = load_snapshot_log(start, end)
    odds_rows = load_prerace_odds(start, end)
    print(f"snapshot log rows: {len(log_rows)}")
    print(f"prerace odds rows: {len(odds_rows)}")

    report = render_report(start, end, log_rows, odds_rows)
    out_path = Path(args.out)
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport: {out_path}")

    if args.mail:
        _send_mail_report(report, out_path, start, end, log_rows)


if __name__ == "__main__":
    main()
