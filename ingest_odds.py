"""1 venue-day 分のオッズを netkeirin から取得 → race_odds.csv 保存

各 venue-day には最大 12 レースあり、それぞれを per-race API で取得する。
race_no は race_meta.csv にある venue-day の場合は既存 entries.race_no を使い、
無ければ 1〜12 を試す。

使い方:
  python ingest_odds.py YYYY-MM-DD placeCode
  python ingest_odds.py 2026-04-26 42        # 名古屋
"""

from __future__ import annotations

import sys
import logging

from src.netkeirin_client import NetkeirinClient
from src.netkeirin_parser import parse_odds_lists
from src.storage import append_rows, has_race_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def ingest_one_day(
    client: NetkeirinClient, place_code: int, race_date: str,
    skip_existing: bool = True,
) -> dict:
    """1 venue-day のオッズを取得して race_odds.csv に追記。

    skip_existing=True なら、既に race_odds.csv に同 venue-day がある場合スキップ。
    戻り値: {"races_fetched": N, "rows_added": M, "skipped": bool}
    """
    if skip_existing and has_race_day("race_odds.csv", place_code, race_date):
        logger.info("Already have odds for %s pc=%d, skipping", race_date, place_code)
        return {"skipped": True, "races_fetched": 0, "rows_added": 0}

    races_fetched = 0
    rows_added = 0
    all_rows = []

    for race_no in range(1, 13):  # 1〜12R
        race_id = NetkeirinClient.make_race_id(race_date, place_code, race_no)
        try:
            odds_data = client.get_odds(race_id)
        except Exception as e:
            logger.error("  R%d (%s) FETCH ERROR: %s", race_no, race_id, e)
            continue
        if odds_data is None:
            # NG = レース無し or データ未保持。R1 から無ければ venue-day 自体無し
            logger.debug("  R%d (%s): NG (no race or no odds)", race_no, race_id)
            continue
        rows = parse_odds_lists(place_code, race_date, race_no, odds_data)
        if rows:
            races_fetched += 1
            all_rows.extend(rows)
            logger.info("  R%d: %d rows (dt=%s)", race_no, len(rows),
                        odds_data.get("official_dt", ""))

    if all_rows:
        rows_added = append_rows("race_odds.csv", all_rows)
    logger.info("=== Done: %s pc=%d / %d races / %d rows ===",
                race_date, place_code, races_fetched, rows_added)
    return {"skipped": False, "races_fetched": races_fetched, "rows_added": rows_added}


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python ingest_odds.py YYYY-MM-DD placeCode")
        print("  example: python ingest_odds.py 2026-04-26 42")
        sys.exit(1)

    race_date = sys.argv[1]
    place_code = int(sys.argv[2])
    client = NetkeirinClient()
    ingest_one_day(client, place_code, race_date)


if __name__ == "__main__":
    main()
