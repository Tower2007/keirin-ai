"""5年分オッズバックフィル (netkeirin)

race_quality.csv で status='normal' の venue-day をループし、
全 race のオッズを netkeirin から取得 → race_odds.csv に追記。

中断・再開対応: data/backfill_odds_done.txt に完了 venue-day を記録。
ネットワーク障害時は urllib3 Retry で自動再試行 (最大5回、指数バックオフ)。

使い方:
  python backfill_odds.py 2021-01-01 2026-12-31
  python backfill_odds.py 2025-01-01            # 単一引数 = それ以降全部

ETA 試算:
  status=normal venue-day 13,285 件 × 12レース × 0.8秒 + parse 〜 13秒/venue-day
  → 約 48 時間 (悲観値)。途中中断・再開可能。
"""

from __future__ import annotations

import json
import sys
import time
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd

from src.config import DATA_DIR as DATA
from src.netkeirin_client import NetkeirinClient
from ingest_odds import ingest_one_day

DONE_FILE = DATA / "backfill_odds_done.txt"
STATS_FILE = DATA / "backfill_odds_stats.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA / "backfill_odds.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def load_done() -> set[str]:
    if not DONE_FILE.exists():
        return set()
    with open(DONE_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_done(key: str) -> None:
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(key + "\n")


def save_stats(stats: dict) -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python backfill_odds.py START_DATE [END_DATE]")
        print("  example: python backfill_odds.py 2021-01-01 2026-12-31")
        sys.exit(1)
    start_date = sys.argv[1]
    end_date = sys.argv[2] if len(sys.argv) > 2 else "2099-12-31"

    # 対象 venue-day: race_quality.csv の status='normal' のみ
    quality = pd.read_csv(DATA / "race_quality.csv")
    targets = quality[
        (quality["status"] == "normal")
        & (quality["race_date"] >= start_date)
        & (quality["race_date"] <= end_date)
    ].sort_values(["race_date", "place_code"]).reset_index(drop=True)

    done = load_done()
    todo: list[tuple[str, int, str]] = []
    for _, row in targets.iterrows():
        rd = row["race_date"]
        pc = int(row["place_code"])
        key = f"{rd}|{pc}"
        if key not in done:
            todo.append((rd, pc, str(row["place_name"])))

    logger.info("=" * 60)
    logger.info("Backfill targets: %d total, %d already done, %d TODO",
                len(targets), len(done), len(todo))
    logger.info("Period: %s 〜 %s", start_date, end_date)
    logger.info("=" * 60)
    if not todo:
        logger.info("All venue-days already processed.")
        return

    client = NetkeirinClient()
    t_start = time.time()
    n_done = 0
    n_errors = 0
    n_rows = 0
    n_no_data = 0  # 全レース NG だった venue-day (古すぎる/未保持)

    started_at = datetime.now().isoformat(timespec="seconds")

    try:
        for i, (rd, pc, name) in enumerate(todo, 1):
            try:
                # done.txt で既に TODO 絞り込み済みのため skip_existing 不要。
                # has_race_day による 1.85GB CSV フルスキャン (約80秒) を回避。
                result = ingest_one_day(client, pc, rd, skip_existing=False)
                rows = result.get("rows_added", 0)
                n_rows += rows
                if result.get("skipped"):
                    pass  # already in CSV (resumed mid-job)
                elif rows == 0:
                    n_no_data += 1
                n_done += 1
                append_done(f"{rd}|{pc}")
            except Exception as e:
                logger.error("FAIL %s pc=%d (%s): %s", rd, pc, name, e)
                n_errors += 1

            if i % 25 == 0 or i == len(todo):
                elapsed = time.time() - t_start
                rate = i / elapsed if elapsed else 0
                eta_h = (len(todo) - i) / rate / 3600 if rate else 0
                logger.info(
                    "[%d/%d] done=%d err=%d no_data=%d rows=%d "
                    "rate=%.2f/s ETA=%.1fh",
                    i, len(todo), n_done, n_errors, n_no_data, n_rows,
                    rate, eta_h,
                )
    finally:
        elapsed = time.time() - t_start
        stats = {
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds": int(elapsed),
            "elapsed_hours": round(elapsed / 3600, 2),
            "n_total_target": len(todo),
            "n_completed": n_done,
            "n_errors": n_errors,
            "n_no_data": n_no_data,
            "n_rows_added": n_rows,
            "period_start": start_date,
            "period_end": end_date,
        }
        save_stats(stats)
        logger.info("Saved stats to %s", STATS_FILE)
        logger.info("Final: %s", stats)


if __name__ == "__main__":
    main()
