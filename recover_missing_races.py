"""過去の取得漏れレースを completion 表駆動で再取得 (2026-07-11 Codex レビュー対応)

race_completion.csv の missing_results / missing_payout レースについて、
ingest_day の per-race 自己治癒 (compute_result_gaps + JSJ012 再取得) を
venue-day 単位で駆動し、結果を recovery_log.csv に 4 分類で固定記録する:

  recovered          : 着順 (missing_payout の場合は払戻) が取得できた
  canceled_confirmed : 再取得したが着順ゼロ = 中止/不成立と確定
  still_missing      : JSJ012 を叩いても行が得られなかった
  parse_error        : 取得/解析で例外

重複安全: ingest_day 側が「results 行が既にあるレースは results を追記しない /
payouts 欠損レースは payouts のみ追記」なので、append のみで置換は不要。
再実行も冪等 (回復済みレースは gap から消えるため再取得しない)。

使い方 (Codex 推奨の順):
  python recover_missing_races.py --sample 25   # 層化サンプル試行 (status×年で層化)
  python recover_missing_races.py --all         # 全件救済 (サンプル検証後)
  python recover_missing_races.py --date 2026-04-05 --place 23   # 個別指定
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src.client import KeirinClient
from src.config import DATA_DIR
from ingest_day import ingest_one_day, _per_race_state

RECOVERY_LOG = DATA_DIR / "recovery_log.csv"
LOG_COLS = ["ts", "race_date", "place_code", "race_no",
            "prev_status", "outcome", "note"]


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_targets(args) -> pd.DataFrame:
    comp = pd.read_csv(DATA_DIR / "race_completion.csv",
                       dtype={"race_date": str, "place_code": str,
                              "race_no": str})
    m = comp[comp["status"].str.startswith("missing")
             & (comp["race_date"] < date.today().isoformat())].copy()
    if args.date:
        m = m[m["race_date"] == args.date]
    if args.place:
        m = m[m["place_code"] == str(args.place)]
    if args.sample:
        # 層化: status × 年 で各層から日付順に均等抽出 (決定的 = 再現可能)
        m["year"] = m["race_date"].str[:4]
        strata = list(m.groupby(["status", "year"]))
        per = max(1, args.sample // max(1, len(strata)))
        picked = pd.concat(
            [g.sort_values(["race_date", "place_code", "race_no"]).head(per)
             for _, g in strata])
        m = picked.head(args.sample) if len(picked) > args.sample else picked
    return m.sort_values(["race_date", "place_code", "race_no"])


def append_log(rows: list[dict]) -> None:
    new_file = not RECOVERY_LOG.exists()
    with open(RECOVERY_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def classify(prev_status: str, race_no: str,
             res_rows: set, settled: set, pays: set) -> str:
    rn = str(race_no)
    if prev_status == "missing_results":
        if rn in settled:
            return "recovered"
        if rn in res_rows:
            return "canceled_confirmed"  # 行は取れたが着順ゼロ = 中止/不成立
        return "still_missing"
    # missing_payout
    if rn in pays:
        return "recovered"
    return "still_missing"


def main() -> None:
    _stdout_utf8()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample", type=int, help="層化サンプル N レースを試行")
    g.add_argument("--all", action="store_true", help="全 missing を救済")
    g.add_argument("--date", help="個別 venue-day 指定 (YYYY-MM-DD、--place 併用)")
    ap.add_argument("--place", help="--date と併用する場コード")
    args = ap.parse_args()

    targets = load_targets(args)
    if len(targets) == 0:
        print("対象なし (race_completion.csv に missing が無いか、フィルタが空)")
        return
    vd = targets.groupby(["race_date", "place_code"])
    print(f"対象: {len(targets)} レース / {vd.ngroups} venue-days")
    print(targets.groupby(["status", targets['race_date'].str[:4]])
          .size().to_string(), "\n")

    client = KeirinClient()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_rows: list[dict] = []
    for (rd, pc), g_races in vd:
        print(f"--- {rd} place={pc} ({len(g_races)} races) ---")
        note = ""
        try:
            ingest_one_day(client, int(pc), rd)   # index 無し = per-race 自己治癒
        except Exception as e:
            note = f"{type(e).__name__}: {e}"[:200]
            print(f"  [error] {note}")
        # 再取得後の状態で分類
        res_rows, settled, pays = _per_race_state(int(pc), rd)
        for _, race in g_races.iterrows():
            outcome = ("parse_error" if note else
                       classify(race["status"], race["race_no"],
                                res_rows, settled, pays))
            log_rows.append({
                "ts": ts, "race_date": rd, "place_code": pc,
                "race_no": race["race_no"], "prev_status": race["status"],
                "outcome": outcome, "note": note,
            })
    append_log(log_rows)

    summary = pd.DataFrame(log_rows)["outcome"].value_counts()
    print("\n=== 結果 (recovery_log.csv に記録) ===")
    print(summary.to_string())
    print("\n次: build_race_completion.py を再実行して completion を更新すること")


if __name__ == "__main__":
    main()
