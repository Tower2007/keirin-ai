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
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev

from src.config import DATA_DIR

sys.stdout.reconfigure(encoding="utf-8")

REPORT_PATH = DATA_DIR / "weekly_drift_report.md"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="過去 N 日を対象 (default 7)")
    ap.add_argument("--since", default=None,
                    help="集計開始日 (YYYY-MM-DD)、--days より優先")
    ap.add_argument("--out", default=str(REPORT_PATH))
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


def summarize_coverage(log_rows: list[dict]) -> dict[int, dict[str, int]]:
    """offset 別の status counts を集計。"""
    by_offset: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in log_rows:
        try:
            off = int(r["target_offset_min"])
        except (ValueError, KeyError, TypeError):
            # 旧スキーマ (target_offset_min 無し) は default 5 扱い
            off = 5
        status = r.get("status", "unknown")
        by_offset[off][status] += 1
    return {k: dict(v) for k, v in by_offset.items()}


def summarize_actual_offset(log_rows: list[dict]) -> dict[int, dict[str, float]]:
    """target_offset_min vs actual minutes_before_start を集計。

    狙いと実測のズレを定量化 (auto の LEAD_MIN 短縮判断の根拠データ)。
    """
    by_offset: dict[int, list[int]] = defaultdict(list)
    for r in log_rows:
        if r.get("status") != "ok":
            continue
        try:
            off = int(r["target_offset_min"])
            actual = int(r["minutes_before_start"])
        except (ValueError, KeyError, TypeError):
            continue
        by_offset[off].append(actual)
    out: dict[int, dict[str, float]] = {}
    for off, vals in by_offset.items():
        if not vals:
            out[off] = {"n": 0, "mean": float("nan"), "std": float("nan")}
            continue
        m = mean(vals)
        s = stdev(vals) if len(vals) > 1 else 0.0
        out[off] = {"n": len(vals), "mean": m, "std": s}
    return out


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

    # ─── 1. 蓄積カバレッジ ────────────────────────────────
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

    # ─── 2. 取得タイミング精度 ────────────────────────────
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

    # ─── 3. live odds vs final odds drift ────────────────
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

    # ─── 4. Market Health (EHI 風) ────────────────────────
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

    # ─── 5. アクション推奨 ────────────────────────────────
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
    return "\n".join(L)


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


if __name__ == "__main__":
    main()
