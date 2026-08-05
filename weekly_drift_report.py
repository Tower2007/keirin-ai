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
import os
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


def compute_market_health(start: date, end: date) -> dict:
    """実測ドリフト (settlement wedge) + overround を期間集計 (監査 P1-1 対応)。

    旧 load_prerace_odds の list[dict] 全読みを廃止し、pandas usecols の
    部分読みに変更 (365MB 全行 dict 化はメモリ事故のもと、監査 P2)。

    返り値 dict:
      wedge   : [{bet, offset, n_races, median_pct, gt10_pct, adverse_pct}]
                wedge = 実現配当(payout/100) / snapshot odds - 1 (当選組のみ)
      overround: [{bet, n_races, ov_median}] (最新 snapshot dedup 後の Σ1/odds)
      n_wedge_races: wedge を測れたユニークレース数 (gate 判定用)

    W (ワイド) はレンジオッズで点 wedge が定義できないため除外 (監査 P2)。
    """
    import pandas as pd
    path = DATA_DIR / "race_odds_prerace.csv"
    empty = {"wedge": [], "overround": [], "n_wedge_races": 0}
    if not path.exists():
        return empty
    keys = ["race_date", "place_code", "race_no"]
    jkeys = keys + ["bet_type", "kumi_ban"]
    odds = pd.read_csv(
        path,
        usecols=jkeys + ["odds", "snapshot_dt", "target_offset_min"],
        dtype={k: str for k in jkeys}, low_memory=False)
    odds = odds[(odds["race_date"] >= start.isoformat())
                & (odds["race_date"] <= end.isoformat())]
    odds["odds"] = pd.to_numeric(odds["odds"], errors="coerce")
    odds = odds[(odds["odds"] > 0) & (odds["bet_type"] != "W")]
    if len(odds) == 0:
        return empty

    pay = pd.read_csv(
        DATA_DIR / "payouts.csv", usecols=jkeys + ["payout"],
        dtype={k: str for k in jkeys}, low_memory=False)
    pay = pay[(pay["race_date"] >= start.isoformat())
              & (pay["race_date"] <= end.isoformat())]
    pay["payout"] = pd.to_numeric(pay["payout"], errors="coerce")
    pay = pay[pay["payout"] > 0].drop_duplicates(subset=jkeys)

    # --- wedge (offset 別、当選組) ---
    m = odds.merge(pay, on=jkeys, how="inner")
    m["wedge"] = (m["payout"] / 100.0) / m["odds"] - 1.0
    m["off"] = pd.to_numeric(m["target_offset_min"], errors="coerce")
    wedge_rows = []
    for (bt, off), g in m.groupby(["bet_type", "off"]):
        w = g["wedge"].dropna()
        if len(w) == 0:
            continue
        wedge_rows.append({
            "bet": bt, "offset": _parse_offset(off),
            "n_races": int(g[keys].drop_duplicates().shape[0]),
            "median_pct": round(float(w.median()) * 100, 2),
            "gt10_pct": round(float((w.abs() > 0.10).mean()) * 100, 1),
            "adverse_pct": round(float((w < 0).mean()) * 100, 1),
        })
    wedge_rows.sort(key=lambda r: (r["bet"], -float(r["offset"])))
    n_wedge_races = int(m[keys].drop_duplicates().shape[0])

    # --- overround (最新 snapshot に dedup 後の Σ1/odds) ---
    o = odds.sort_values("snapshot_dt").drop_duplicates(subset=jkeys, keep="last")
    o["imp"] = 1.0 / o["odds"]
    ov = o.groupby(keys + ["bet_type"])["imp"].sum().reset_index(name="ov")
    ov_rows = [
        {"bet": bt, "n_races": int(len(g)),
         "ov_median": round(float(g["ov"].median()), 4)}
        for bt, g in ov.groupby("bet_type")
    ]
    return {"wedge": wedge_rows, "overround": ov_rows,
            "n_wedge_races": n_wedge_races}


GATE_A_BETS = ["SH2", "ST2", "RH3", "RT3"]  # WH2/WT2 は未発売が多く n 不足のため対象外

# ─── 総合状態の単一判定 (2026-08-05 統合監視指摘 P1) ────────────────────
# 件名と本文ヘッダが別ロジックで状態を出し矛盾していた
# (件名 ⚠️ = 取得漏れ検知、本文 🟢 = 日次 status のみ) ため、ここで一元化する。
# 判定条件は本文末尾の「判定基準」ブロックにも同じ文言で出す。
OVERALL_CRITERIA = [
    "🔴 NG: データロスト日 (❌) が1日でもある",
    "🟡 WARN: 警告日 (⚠️) が1日でもある、または取得漏れ候補が1件でもある",
    "🟢 OK: 上記いずれにも該当しない",
    "※ 当日 (🟡 TODAY 進行中) は結果が未確定のため判定対象外。"
    "「OK n/m日」の分母 m も確定日数のみを数える",
]


def compute_overall_status(daily: list[dict], missing: list[dict]) -> dict:
    """週次レポートの総合状態を1箇所で決める (件名・本文の唯一の情報源)。

    返り値: {level, mark, label, color, n_ok_days, n_total_days,
             n_warn, n_nodata, n_missing, n_inprogress}
      level: "OK" | "WARN" | "NG"
      mark : 🟢 / 🟡 / 🔴 (件名・本文で共通利用)

    当日は "🟡 TODAY (進行中)" で結果未確定のため、判定からも
    「OK n/m日」の分母からも除外する (除外しないと全て正常でも
    OK 6/7日 と表示され、受信者が欠損と誤読する)。
    """
    n_inprogress = sum(1 for d in daily if d["status"].startswith("🟡"))
    settled = [d for d in daily if not d["status"].startswith("🟡")]
    n_warn = sum(1 for d in settled if d["status"].startswith("⚠️"))
    n_nodata = sum(1 for d in settled if d["status"].startswith("❌"))
    n_missing = len(missing)
    if n_nodata > 0:
        level, mark, label, color = "NG", "🔴", "NG", "#c62828"
    elif n_warn > 0 or n_missing > 0:
        level, mark, label, color = "WARN", "🟡", "WARN", "#f9a825"
    else:
        level, mark, label, color = "OK", "🟢", "OK", "#2e7d32"
    return {
        "level": level, "mark": mark, "label": label, "color": color,
        "n_ok_days": sum(1 for d in settled if d["status"].startswith("✅")),
        "n_total_days": len(settled),
        "n_warn": n_warn, "n_nodata": n_nodata, "n_missing": n_missing,
        "n_inprogress": n_inprogress,
    }


def evaluate_gate_a(end: date) -> dict:
    """freeze gate A (snapshot timing lock) の自動判定 (Codex 2026-07-11 提案)。

    判定は **offset × bet_type 単位** (Codex 07-11 再レビュー対応)。
    旧実装は offset を全券種 AND で判定したため、RT3 を gate B で戦略候補から
    外しても timing 自体が永続 FAIL する論理衝突があった。共有条件と券種別
    条件を分離し、gate B は必要な券種の PASS だけを参照する。

    共有条件 (offset 単位、4 週すべて):
      - settled races >= 150 / 週 (wedge を実測できたユニークレース数)
      - planned race に対する ok coverage >= 98%
      - 発走後取得 (seconds_before_start < 0) = 0
      - abs(capture_lag_sec) の p95 <= 10 秒 (早すぎる取得も検知)
      - capture_lag_sec の null 率 <= 50% (週×offset 単位、分母は status=ok の
        行数)。null 率 > 50% は「lag 未計測」として fail (2026-07-11 監査:
        秒精度列 null の ok 行が黙って素通しされる穴を塞ぐ。緩和ではなく追加条件)
      - 直近 14 日の未解決 missing = 0
    券種別条件 (offset × bet、4 週すべて):
      - |median wedge| <= 1.0%、P90(|wedge|) <= 5%、P(|wedge|>10%) <= 5%
    PASS = 「その offset × 券種の timing 固定可」であり収益の保証ではない。
    """
    import pandas as pd
    # 進行中の当日を含めると planned に夕方レースが入り coverage が
    # 見かけ上崩れる (snapshot は未来に取れない) ため、完結した日までで評価
    if end >= date.today():
        end = date.today() - timedelta(days=1)
    start28 = end - timedelta(days=27)
    weeks = [(end - timedelta(days=7 * i + 6), end - timedelta(days=7 * i))
             for i in range(3, -1, -1)]  # 古い週 → 新しい週
    keys = ["race_date", "place_code", "race_no"]
    jkeys = keys + ["bet_type", "kumi_ban"]
    out: dict = {"weeks": [w[0].isoformat() + "~" + w[1].isoformat()
                           for w in weeks],
                 "offsets": {}, "missing_14d": 0}

    # planned races (entries ベース) — 28 日分のみ
    ent = pd.read_csv(DATA_DIR / "race_entries.csv", usecols=keys,
                      dtype=str, low_memory=False)
    ent = ent[(ent["race_date"] >= start28.isoformat())
              & (ent["race_date"] <= end.isoformat())].drop_duplicates()

    # snapshot log (秒精度列込み)
    log = pd.read_csv(DATA_DIR / "prerace_snapshot_log.csv",
                      dtype={k: str for k in keys}, low_memory=False)
    log = log[(log["race_date"] >= start28.isoformat())
              & (log["race_date"] <= end.isoformat())]
    log["off"] = pd.to_numeric(log["target_offset_min"], errors="coerce")
    log["sbs"] = pd.to_numeric(log.get("seconds_before_start"), errors="coerce")
    log["lag"] = pd.to_numeric(log.get("capture_lag_sec"), errors="coerce")

    # odds + payouts → wedge (28 日分)
    odds = pd.read_csv(
        DATA_DIR / "race_odds_prerace.csv",
        usecols=jkeys + ["odds", "target_offset_min"],
        dtype={k: str for k in jkeys}, low_memory=False)
    odds = odds[(odds["race_date"] >= start28.isoformat())
                & (odds["race_date"] <= end.isoformat())]
    odds["odds"] = pd.to_numeric(odds["odds"], errors="coerce")
    odds = odds[(odds["odds"] > 0) & odds["bet_type"].isin(GATE_A_BETS)]
    pay = pd.read_csv(DATA_DIR / "payouts.csv", usecols=jkeys + ["payout"],
                      dtype={k: str for k in jkeys}, low_memory=False)
    pay = pay[(pay["race_date"] >= start28.isoformat())
              & (pay["race_date"] <= end.isoformat())]
    pay["payout"] = pd.to_numeric(pay["payout"], errors="coerce")
    pay = pay[pay["payout"] > 0].drop_duplicates(subset=jkeys)
    m = odds.merge(pay, on=jkeys, how="inner")
    m["wedge"] = (m["payout"] / 100.0) / m["odds"] - 1.0
    m["off"] = pd.to_numeric(m["target_offset_min"], errors="coerce")

    # 直近 14 日 missing (completion 表)
    out["missing_14d"] = len(load_completion_missing(
        end - timedelta(days=13), end))

    offsets = sorted(log["off"].dropna().unique(), reverse=True)
    for off in offsets:
        wk_rows = []
        shared_all_pass = True
        bet_all_pass = {bt: True for bt in GATE_A_BETS}
        for ws, we in weeks:
            wsi, wei = ws.isoformat(), we.isoformat()
            lw = log[(log["off"] == off) & (log["race_date"] >= wsi)
                     & (log["race_date"] <= wei)]
            ok = lw[lw["status"] == "ok"]
            ok_races = ok[keys].drop_duplicates().shape[0]
            planned_races = ent[(ent["race_date"] >= wsi)
                                & (ent["race_date"] <= wei)][keys] \
                .drop_duplicates().shape[0]
            coverage = ok_races / planned_races if planned_races else 0.0
            post_start = int((ok["sbs"] < 0).sum())
            # abs(): 予定より早すぎる取得も遅延と同様に検知 (Codex 再レビュー)
            lag_p95 = (float(ok["lag"].abs().quantile(0.95))
                       if ok["lag"].notna().any() else float("nan"))
            # lag null 率 (2026-07-11 監査): 分母 = この週×offset の ok 行数。
            # ok 行が 0 の週は null 率 100% 扱い (lag 未計測として fail。
            # coverage でも fail するが定義として固定)
            lag_null_rate = (float(ok["lag"].isna().mean())
                             if len(ok) else 1.0)
            mw = m[(m["off"] == off) & (m["race_date"] >= wsi)
                   & (m["race_date"] <= wei)]
            settled = mw[keys].drop_duplicates().shape[0]

            # --- 共有条件 (offset 単位) ---
            shared_fails: list[str] = []
            if settled < 150:
                shared_fails.append(f"settled {settled}<150")
            if coverage < 0.98:
                shared_fails.append(f"coverage {coverage:.1%}<98%")
            if post_start > 0:
                shared_fails.append(f"発走後取得 {post_start}")
            if lag_null_rate > 0.50:
                # 秒精度列 null の ok 行を黙って素通しさせない (監査 07-11)。
                # 全行 null (lag_p95=NaN) もここに含まれる
                shared_fails.append(
                    f"lag未計測 (null {lag_null_rate:.0%}>50%)")
            elif not (lag_p95 == lag_p95):  # NaN (防御的、通常は上で捕捉)
                shared_fails.append("lag 未計測")
            elif lag_p95 > 10:
                shared_fails.append(f"lag_p95 {lag_p95:.0f}s>10s")
            if shared_fails:
                shared_all_pass = False

            # --- 券種別条件 (offset × bet 単位) ---
            bet_fails: dict[str, list[str]] = {}
            for bt in GATE_A_BETS:
                bf: list[str] = []
                w = mw[mw["bet_type"] == bt]["wedge"].dropna()
                if len(w) == 0:
                    bf.append("n=0")
                else:
                    med = abs(float(w.median())) * 100
                    p90 = float(w.abs().quantile(0.90)) * 100
                    p10 = float((w.abs() > 0.10).mean()) * 100
                    if med > 1.0:
                        bf.append(f"|med|{med:.1f}%>1%")
                    if p90 > 5.0:
                        bf.append(f"P90 {p90:.1f}%>5%")
                    if p10 > 5.0:
                        bf.append(f">10% {p10:.1f}%>5%")
                if bf:
                    bet_fails[bt] = bf
                    bet_all_pass[bt] = False

            wk_rows.append({
                "week": f"{wsi[5:]}~{wei[5:]}", "settled": settled,
                "coverage_pct": round(coverage * 100, 1),
                "post_start": post_start,
                "lag_p95": round(lag_p95, 1) if lag_p95 == lag_p95 else None,
                "lag_null_pct": round(lag_null_rate * 100, 1),
                "shared_pass": not shared_fails,
                "shared_fails": shared_fails[:3],
                "bet_fails": bet_fails,
            })
        missing_ok = out["missing_14d"] == 0
        bet_verdicts = {
            bt: shared_all_pass and missing_ok and bet_all_pass[bt]
            for bt in GATE_A_BETS
        }
        out["offsets"][_parse_offset(off)] = {
            "weeks": wk_rows,
            "shared_pass": shared_all_pass and missing_ok,
            "bet_verdicts": bet_verdicts,
        }
    return out


def count_unique_ok_races(log_rows: list[dict]) -> int:
    """ok snapshot が 1 つ以上あるユニークレース数 (gate 判定用、監査 P1-1)。

    旧実装は offset 合算の ok 行数 (同一レースを最大 5 重カウント) を
    「n」として freeze 判定していた。独立サンプルはレースなのでこちらを使う。
    """
    return len({
        (r.get("race_date", ""), r.get("place_code", ""), r.get("race_no", ""))
        for r in log_rows if r.get("status") == "ok"
    })


def load_completion_missing(start: date, end: date) -> list[dict]:
    """race_completion.csv から期間内の取得漏れ候補を返す (監査 P1-3)。

    当日は results が構造的に未取得 (翌朝 5:00 cron) なので除外。
    canceled は正当な欠損 (着順なし=中止、全期間で 100% 払戻なしと実証済) なので
    取得漏れには数えない。
    """
    path = DATA_DIR / "race_completion.csv"
    if not path.exists():
        return []
    today_iso = date.today().isoformat()
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rd = r.get("race_date", "")
            if not (start.isoformat() <= rd <= end.isoformat()):
                continue
            if rd >= today_iso:
                continue
            if str(r.get("status", "")).startswith("missing"):
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
    L.append("## 1. データスクレイピング 日次ヘルス (auto/boat 形式)")
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
    L.append("## 2. cron タスク死活")
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
    L.append("## 3. 蓄積カバレッジ (offset 別 status 分布)")
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
    L.append("## 4. 取得タイミング精度 (target_offset_min vs 実測 minutes_before_start)")
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


def _md_drift(health: dict) -> list[str]:
    """セクション 3: 実測 settlement wedge (snapshot odds → 確定配当の乖離)。"""
    L: list[str] = []
    L.append("## 5. 実測ドリフト (settlement wedge, 当選組)")
    L.append("")
    L.append("wedge = 実現配当(payout/100) / snapshot odds − 1。median<0 = 当選側で不利。")
    L.append("W はレンジオッズで点 wedge が定義できないため除外。")
    L.append("")
    if not health["wedge"]:
        L.append("⚠️ 期間内に wedge を測れる (snapshot ∩ 当選組) データなし。")
    else:
        L.append("| 券種 | offset (分前) | n_races | median wedge% | \\|w\\|>10% | 不利% |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for r in health["wedge"]:
            L.append(f"| {r['bet']} | {r['offset']} | {r['n_races']} | "
                     f"{r['median_pct']} | {r['gt10_pct']} | {r['adverse_pct']} |")
    L.append("")
    return L


def _md_market_health(health: dict) -> list[str]:
    """セクション 4: Market Health (overround)。"""
    L: list[str] = []
    L.append("## 6. Market Health (overround)")
    L.append("")
    L.append("最新 snapshot dedup 後の Σ(1/odds)。基準 ≈1.34 (控除率25%)。")
    L.append("大きく外れたら odds 取得/dedup の破損を疑う。")
    L.append("")
    if not health["overround"]:
        L.append("⚠️ 期間内データなし。")
    else:
        L.append("| 券種 | n_races | overround median |")
        L.append("|---|---:|---:|")
        for r in health["overround"]:
            flag = "" if 1.25 <= r["ov_median"] <= 1.45 else " ⚠️"
            L.append(f"| {r['bet']} | {r['n_races']} | {r['ov_median']}{flag} |")
    L.append("")
    return L


def _md_completion(missing: list[dict]) -> list[str]:
    """セクション 5: per-race 完了状態 (取得漏れ候補、監査 P1-3)。"""
    L: list[str] = []
    L.append("## 7. per-race 完了状態 (取得漏れ候補)")
    L.append("")
    L.append("race_completion.csv より。canceled (着順なし=中止) は正当欠損として除外済。")
    L.append("")
    if not missing:
        L.append("✅ 期間内の取得漏れ候補なし。")
    else:
        L.append(f"⚠️ **取得漏れ候補 {len(missing)} 件** (再取得対象):")
        L.append("")
        L.append("| race_date | place | R | status |")
        L.append("|---|---|---|---|")
        for r in missing[:15]:
            L.append(f"| {r['race_date']} | {r['place_code']} | "
                     f"{r['race_no']} | {r['status']} |")
        if len(missing) > 15:
            L.append(f"| ... | | | (+{len(missing) - 15} 件) |")
    L.append("")
    return L


def _md_gate_a(gate: dict) -> list[str]:
    """セクション 7: freeze gate A (snapshot timing lock) 自動判定。

    判定は offset × 券種単位 (Codex 07-11 再レビュー: 全券種 AND だと
    RT3 を gate B で外しても timing が永続 FAIL する衝突があった)。
    """
    L: list[str] = []
    L.append("## 8. freeze gate A 判定 (snapshot timing lock, offset×券種)")
    L.append("")
    L.append("基準: CLAUDE.md「凍結基準」参照。gate B は戦略候補券種の PASS のみ参照する。")
    L.append("**通過 = その offset×券種の timing 固定可であり収益の保証ではない。**")
    L.append("")
    L.append(f"- 直近 14 日の未解決 missing: **{gate['missing_14d']}**")
    L.append("")
    # offset × 券種 の判定マトリクス
    L.append("| offset (分前) | 共有条件 | " + " | ".join(GATE_A_BETS) + " |")
    L.append("|---:|---|" + "---|" * len(GATE_A_BETS))
    for off, data in gate["offsets"].items():
        sh = "✅" if data["shared_pass"] else "❌"
        cells = " | ".join(
            "🟢 PASS" if data["bet_verdicts"].get(bt) else "🔴 FAIL"
            for bt in GATE_A_BETS)
        L.append(f"| {off} | {sh} | {cells} |")
    L.append("")
    # 週別詳細 (共有条件 + 券種別 fail 理由)
    for off, data in gate["offsets"].items():
        L.append(f"### offset {off} 分前 (週別詳細)")
        L.append("")
        L.append("| 週 | settled | cov% | 発走後 | \\|lag\\| p95 | lag null% | 共有 | 券種別 fail |")
        L.append("|---|---:|---:|---:|---:|---:|---|---|")
        for w in data["weeks"]:
            sh = "✅" if w["shared_pass"] else "❌ " + "; ".join(w["shared_fails"])
            bf = "; ".join(f"{bt}: {', '.join(fs)}"
                           for bt, fs in w["bet_fails"].items()) or "—"
            L.append(f"| {w['week']} | {w['settled']} | {w['coverage_pct']} | "
                     f"{w['post_start']} | {w['lag_p95']} | "
                     f"{w.get('lag_null_pct', '—')} | {sh} | {bf} |")
        L.append("")
    return L


def _md_actions(log_rows: list[dict], health: dict) -> list[str]:
    """セクション 6: アクション推奨 + フッタ。

    gate 判定は「ユニークレース数」基準 (監査 P1-1)。旧実装の offset 合算
    ok 行数は同一レースを最大 5 重カウントし観測数を過大表示していた。
    """
    L: list[str] = []
    L.append("## 9. アクション推奨")
    L.append("")
    n_races = count_unique_ok_races(log_rows) if log_rows else 0
    n_wedge = health.get("n_wedge_races", 0)
    L.append(f"- 期間内 ok snapshot ユニークレース数: **{n_races}** "
             f"(wedge 実測可能: {n_wedge})")
    if n_races < 30:
        L.append("- 🟡 蓄積期 (races<30): drift 評価不能、観測継続")
    elif n_races < 50:
        L.append("- 🟡 監査期 (races=30〜50): drift 定量化開始、戦略はまだ固定しない")
    else:
        L.append("- 🟢 観測十分 (races≥50): ただし freeze 判定は本表の実測 wedge / "
                 "overround / 完了状態を見て行う (行数 gate は廃止)")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 判定基準 (件名・本文の総合状態)")
    L.append("")
    L.append("件名と本文ヘッダの状態は同一の判定関数 "
             "(`compute_overall_status`) が決めており、常に一致する。")
    L.append("")
    for c in OVERALL_CRITERIA:
        L.append(f"- {c}")
    L.append("")
    L.append("---")
    L.append("生成: `python weekly_drift_report.py`")
    return L


def render_report(start: date, end: date,
                  log_rows: list[dict],
                  health: dict,
                  missing: list[dict],
                  gate: dict) -> str:
    """マークダウンレポート文字列を生成。"""
    L: list[str] = []
    L.append(f"# Weekly Drift Report ({start.isoformat()} ~ {end.isoformat()})")
    L.append("")
    # 総合状態は件名・HTML と同一関数 (compute_overall_status) から取る
    overall = compute_overall_status(get_daily_ingestion_health(start, end),
                                     missing)
    L.append(f"**総合状態: {overall['mark']}{overall['label']}** "
             f"(OK {overall['n_ok_days']}/{overall['n_total_days']}日 / "
             f"⚠️ {overall['n_warn']}日 / ❌ {overall['n_nodata']}日 / "
             f"取得漏れ {overall['n_missing']}件 / "
             f"進行中 {overall['n_inprogress']}日は判定対象外)")
    L.append("")
    L.append(f"生成時刻: {datetime.now().isoformat(timespec='seconds')}")
    L.append("")
    L.append("Phase 6 R3 (Codex 5/15 提案 P2、Gemini EHI 監視に相当)。")
    L.append("")
    L.extend(_md_daily_health(start, end))
    L.extend(_md_cron_health())
    L.extend(_md_coverage(log_rows))
    L.extend(_md_timing(log_rows))
    L.extend(_md_drift(health))
    L.extend(_md_market_health(health))
    L.extend(_md_completion(missing))
    L.extend(_md_gate_a(gate))
    L.extend(_md_actions(log_rows, health))
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

# セル基本スタイル (属性形ではなく宣言のみ。td() が単一 style= に合成する)
_TD_BASE_L = "text-align:left; padding:6px 10px; border:1px solid #ddd;"
_TD_BASE_R = "text-align:right; padding:6px 10px; border:1px solid #ddd;"
# 条件付き装飾 (2026-08-05 P3: 以前は基本 style と別の style= として出力され、
# HTML が先勝ちで後を捨てるため色・太字が一切効いていなかった)
TD_OK = "color:#2e7d32; font-weight:bold;"
TD_BAD = "color:#d32f2f; font-weight:bold;"
TD_WARN = "color:#f57f17; font-weight:bold;"
TD_MUTED = "color:#999;"


def td(content, extra: str = "", right: bool = False) -> str:
    """<td> を生成。基本 style と条件付き style を **単一の style 属性**に合成する。

    セル生成をこの関数に集約することで style= の二重出力が構造的に起こらない
    (2026-08-05 統合監視 P3 指摘の再発防止)。
    検出: grep -c 'style="[^"]*"\\s*style="' <出力HTML> が 0 になること。
    """
    base = _TD_BASE_R if right else _TD_BASE_L
    style = f"{base} {extra}".strip()
    return f'<td style="{style}">{content}</td>'


def _html_header(start: date, end: date, daily: list[dict],
                 n_snap_total: int, overall: dict) -> list[str]:
    """div 開始 + タイトル h2 + サマリ行。

    状態バッジは compute_overall_status の結果のみを使う (件名と同一の変数)。
    """
    p: list[str] = []
    p.append('<div style="font-family:Arial,sans-serif; font-size:14px; '
             'color:#222; line-height:1.55; max-width:880px;">')
    p.append(
        f'<h2 style="color:{overall["color"]}; margin:0 0 12px 0; '
        f'padding-bottom:6px; border-bottom:2px solid {overall["color"]};">'
        f'🚴 keirin-ai 週次 drift report &nbsp; '
        f'<span style="color:#222; font-weight:normal;">{start} 〜 {end}</span>'
        f' &nbsp; <span style="font-weight:normal;">'
        f'{overall["mark"]}{overall["label"]}</span></h2>'
    )
    p.append(
        f'<p style="margin:0 0 12px 0; color:#555;">'
        f'OK <b>{overall["n_ok_days"]}</b>/{overall["n_total_days"]}日 '
        f'&nbsp;|&nbsp; ⚠️ 警告 <b>{overall["n_warn"]}</b>日 &nbsp;|&nbsp; '
        f'❌ ロスト <b>{overall["n_nodata"]}</b>日 &nbsp;|&nbsp; '
        f'取得漏れ <b>{overall["n_missing"]}</b>件 &nbsp;|&nbsp; '
        f'進行中 <b>{overall["n_inprogress"]}</b>日 (判定対象外) &nbsp;|&nbsp; '
        f'累計 snapshots <b>{n_snap_total:,}</b></p>'
    )
    return p


def _html_daily_health(daily: list[dict]) -> list[str]:
    """セクション 0: データスクレイピング日次ヘルス。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '1. データスクレイピング日次ヘルス</h3>')
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
            st_extra = TD_OK
        elif d["status"].startswith("🟡"):
            st_extra = "color:#666; font-weight:bold;"
        elif d["status"].startswith("⚠️"):
            st_extra = TD_WARN
        else:
            st_extra = TD_BAD
        p.append(
            f'<tr {alt}>'
            + td(d["date"])
            + td(f'{d["venues"]}', right=True)
            + td(f'{d["entries"]:,}', right=True)
            + td(f'{d["lines"]:,}', right=True)
            + td(f'{d["stats"]:,}', right=True)
            + td(f'{d["results"]:,}', right=True)
            + td(f'{d["payouts"]:,}', right=True)
            + td(f'{d["snapshots_ok"]:,}', right=True)
            + td(d["status"], st_extra)
            + '</tr>'
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
             '2. cron タスク死活</h3>')
    p.append(f'<table {HTML_TBL}>')
    p.append(f'<tr><th {HTML_TH}>Task</th><th {HTML_TH}>Last Run</th>'
             f'<th {HTML_TH}>Last Result</th><th {HTML_TH}>Next Run</th>'
             f'<th {HTML_TH}>Status</th></tr>')
    for i, c in enumerate(cron_status):
        alt = HTML_ROW_ALT if i % 2 == 1 else ""
        is_ok = c["last_result"] in CRON_OK_RESULTS
        mark = "✅" if is_ok else "⚠️"
        p.append(
            f'<tr {alt}>'
            + td(f'<b>{c["task"]}</b>')
            + td(c["last_run"])
            + td(f'{mark} {c["last_result"]}',
                 "color:#2e7d32;" if is_ok else "color:#d32f2f;")
            + td(c["next_run"])
            + td(c["status"])
            + '</tr>'
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
             '3. 蓄積カバレッジ (offset 別 status 分布)</h3>')
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
             '4. 取得タイミング精度 (target vs 実測)</h3>')
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


def _html_drift_health(health: dict, missing: list[dict]) -> list[str]:
    """セクション 3-5: 実測 wedge / overround / 完了状態 (監査 P1-1/P1-3)。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '5. 実測ドリフト (settlement wedge, 当選組, W除外)</h3>')
    if not health["wedge"]:
        p.append('<p style="color:#999;">⚠️ 期間内に測定可能データなし</p>')
    else:
        p.append(f'<table {HTML_TBL}>')
        p.append(f'<tr><th {HTML_TH}>券種</th><th {HTML_TH_R}>offset</th>'
                 f'<th {HTML_TH_R}>n_races</th><th {HTML_TH_R}>median wedge%</th>'
                 f'<th {HTML_TH_R}>|w|&gt;10%</th><th {HTML_TH_R}>不利%</th></tr>')
        for r in health["wedge"]:
            med_extra = TD_BAD if r["median_pct"] < -3 else "color:#333;"
            p.append(
                '<tr>'
                + td(r["bet"])
                + td(r["offset"], right=True)
                + td(r["n_races"], right=True)
                + td(f'<b>{r["median_pct"]}</b>', med_extra, right=True)
                + td(r["gt10_pct"], right=True)
                + td(r["adverse_pct"], right=True)
                + '</tr>')
        p.append('</table>')

    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '6. Market Health (overround, 基準≈1.34)</h3>')
    if not health["overround"]:
        p.append('<p style="color:#999;">⚠️ 期間内データなし</p>')
    else:
        cells = " / ".join(
            f'{r["bet"]}: <b>{r["ov_median"]}</b>'
            + ("" if 1.25 <= r["ov_median"] <= 1.45 else " ⚠️")
            for r in health["overround"])
        p.append(f'<p>{cells}</p>')

    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '7. per-race 完了状態 (取得漏れ候補)</h3>')
    if not missing:
        p.append('<p style="color:#2a7;">✅ 期間内の取得漏れ候補なし</p>')
    else:
        items = ", ".join(f'{r["race_date"]}/{r["place_code"]}/{r["race_no"]}'
                          for r in missing[:10])
        more = f" (+{len(missing) - 10})" if len(missing) > 10 else ""
        p.append(f'<p style="color:#c00;">⚠️ <b>{len(missing)} 件</b>: '
                 f'{items}{more}</p>')
    return p


def _html_gate_a(gate: dict) -> list[str]:
    """セクション 7: freeze gate A 判定 (offset×券種マトリクス)。"""
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '8. freeze gate A (snapshot timing lock, offset×券種)</h3>')
    p.append(f'<table {HTML_TBL}>')
    p.append(f'<tr><th {HTML_TH_R}>offset (分前)</th><th {HTML_TH}>共有条件</th>'
             + "".join(f'<th {HTML_TH}>{bt}</th>' for bt in GATE_A_BETS)
             + '</tr>')
    for off, data in gate["offsets"].items():
        sh = ('<span style="color:#2a7;">✅</span>' if data["shared_pass"]
              else '<span style="color:#c00;">❌</span>')
        cells = "".join(
            f'<td {HTML_TD_L}>'
            + ('<b style="color:#2a7;">PASS</b>'
               if data["bet_verdicts"].get(bt)
               else '<b style="color:#c00;">FAIL</b>')
            + '</td>'
            for bt in GATE_A_BETS)
        p.append(f'<tr><td {HTML_TD_R}><b>{off}</b></td>'
                 f'<td {HTML_TD_L}>{sh}</td>{cells}</tr>')
    p.append('</table>')
    p.append(f'<p style="color:#999; font-size:12px;">'
             f'直近14日 missing={gate["missing_14d"]}。基準は CLAUDE.md 凍結基準。'
             f'gate B は戦略候補券種の PASS のみ参照。通過=timing固定可、'
             f'収益保証ではない。</p>')
    return p


def _html_actions(n_races: int, health: dict) -> list[str]:
    """セクション 6 (アクション推奨) + フッタ + div 終了。

    gate はユニークレース数基準 (監査 P1-1: 旧 offset 合算 ok 行数は
    同一レース最大 5 重カウントのため廃止)。
    """
    p: list[str] = []
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">9. アクション推奨</h3>')
    if n_races < 30:
        action = "🟡 蓄積期 (races&lt;30): drift 評価不能、観測継続"
    elif n_races < 50:
        action = "🟡 監査期 (races=30〜50): drift 定量化開始、戦略は未固定"
    else:
        action = ("🟢 観測十分 (races≥50): freeze 判定は実測 wedge / overround / "
                  "完了状態で行う (行数 gate 廃止)")
    p.append(f'<p>期間内 ok snapshot ユニークレース数: <b>{n_races:,}</b> '
             f'(wedge 実測可能: {health.get("n_wedge_races", 0):,})<br>{action}</p>')

    # 判定基準 (件名と本文が同一関数から出ることを明記、2026-08-05 P1)
    p.append('<h3 style="color:#444; margin:18px 0 8px 0;">'
             '判定基準 (件名・本文の総合状態)</h3>')
    p.append('<p style="color:#555; margin:0 0 6px 0;">件名と本文ヘッダの状態は '
             '同一の判定関数が決めており、常に一致する。</p>')
    p.append('<ul style="margin:0 0 8px 0; padding-left:20px; color:#555;">'
             + "".join(f'<li>{c}</li>' for c in OVERALL_CRITERIA)
             + '</ul>')
    p.append(
        '<hr style="border:none; border-top:1px solid #ddd; '
        'margin:18px 0 8px 0;">'
        '<p style="color:#999; font-size:11px; margin:0;">'
        'keirin-ai weekly drift report</p>'
    )
    p.append('</div>')
    return p


def render_report_html(start: date, end: date,
                       log_rows: list[dict],
                       health: dict,
                       missing: list[dict],
                       gate: dict) -> str:
    """Gmail で剥がされない inline-style 版 HTML レポート。

    text 版と同じ情報を見やすい色付き表で表示。
    """
    daily = get_daily_ingestion_health(start, end)
    cron_status = check_cron_health()
    cov = summarize_coverage(log_rows) if log_rows else {}
    timing = summarize_actual_offset(log_rows) if log_rows else {}
    n_snap_total = sum(d["snapshots_ok"] for d in daily)
    n_races = count_unique_ok_races(log_rows) if log_rows else 0
    overall = compute_overall_status(daily, missing)

    p: list[str] = []
    p.extend(_html_header(start, end, daily, n_snap_total, overall))
    p.extend(_html_daily_health(daily))
    p.extend(_html_cron_health(cron_status))
    p.extend(_html_coverage(cov))
    p.extend(_html_timing(timing))
    p.extend(_html_drift_health(health, missing))
    p.extend(_html_gate_a(gate))
    p.extend(_html_actions(n_races, health))
    return "\n".join(p)


def _send_mail_report(report: str, out_path: Path,
                      start: date, end: date,
                      log_rows: list[dict],
                      health: dict,
                      missing: list[dict],
                      gate: dict) -> None:
    """--mail 指定時: サマリ入り件名 + HTML 版を付けてレポートをメール送信。"""
    try:
        from gmail_notify import send_email
    except Exception as e:
        print(f"⚠️ gmail_notify import 失敗: {e}", file=sys.stderr)
        return
    # メール本文は markdown 全文 (Gmail 上は等幅で読めるので十分)
    # 件名の状態は本文ヘッダと同一の compute_overall_status から取る
    # (2026-08-05 統合監視 P1: 件名 ⚠️ / 本文 🟢 の矛盾を解消)。
    # 書式: [keirin] 🚴 週次 <送信日> 🟡WARN (OK 6/7日 / races 472 / 取得漏れ1)
    #   - 日付は期間ではなく送信日 1 つ (期間は本文見出しにある)
    n_races = count_unique_ok_races(log_rows) if log_rows else 0
    daily_health = get_daily_ingestion_health(start, end)
    overall = compute_overall_status(daily_health, missing)
    sent_date = date.today().isoformat()
    detail = (f"OK {overall['n_ok_days']}/{overall['n_total_days']}日 "
              f"/ races {n_races}")
    if overall["n_missing"]:
        detail += f" / 取得漏れ{overall['n_missing']}"
    subject = (f"[keirin] 🚴 週次 {sent_date} "
               f"{overall['mark']}{overall['label']} ({detail})")
    # HTML 版を生成 (Gmail で見栄え良い表に)
    html_body = render_report_html(start, end, log_rows, health, missing, gate)
    try:
        send_email(
            subject=subject,
            body=report,
            html=html_body,
            attachments=[str(out_path)],
        )
        # 送信成功をログに残す (統合監視の突合用)
        to_addr = os.environ.get("NOTIFY_TO", "(NOTIFY_TO 未設定)")
        print(f"[mail] sent to {to_addr}")
    except Exception as e:
        print(f"⚠️ メール送信失敗: {e}", file=sys.stderr)


def main() -> None:
    args = parse_args()
    start, end = get_date_range(args)
    print(f"Range: {start} ~ {end}")

    log_rows = load_snapshot_log(start, end)
    health = compute_market_health(start, end)
    missing = load_completion_missing(start, end)
    gate = evaluate_gate_a(end)
    print(f"snapshot log rows: {len(log_rows)}")
    print(f"wedge 実測可能レース: {health['n_wedge_races']}, "
          f"取得漏れ候補: {len(missing)}")
    for off, d in gate["offsets"].items():
        marks = " ".join(
            f"{bt}={'PASS' if d['bet_verdicts'].get(bt) else 'FAIL'}"
            for bt in GATE_A_BETS)
        print(f"gate A {off}分前: 共有={'OK' if d['shared_pass'] else 'NG'} {marks}")

    report = render_report(start, end, log_rows, health, missing, gate)
    out_path = Path(args.out)
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport: {out_path}")

    if args.mail:
        _send_mail_report(report, out_path, start, end, log_rows,
                          health, missing, gate)


if __name__ == "__main__":
    main()
