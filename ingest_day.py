"""1日分のレースデータを取得 → CSV 保存

Phase 1: 開催メタ + 出走表 + ライン情報のみ
Phase 2 で予定: オッズ・結果・払戻

使い方:
  python ingest_day.py YYYY-MM-DD placeCode
  python ingest_day.py 2026-04-26 42      # 名古屋
"""

import sys
import logging

from src.client import KeirinClient, VENUE_CODES
from src.parser import (
    extract_json_data,
    parse_program_meta,
    parse_race_entries,
    parse_race_lines,
)
from src.storage import append_row, append_rows, has_race_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def ingest_one_day(client: KeirinClient, place_code: int, race_date: str) -> dict:
    """1日分のデータを取得して CSV に保存。戻り値は投入行数サマリー。"""

    # 重複チェック (race_meta.csv をキーに)
    if has_race_day("race_meta.csv", place_code, race_date):
        logger.warning("Already ingested: %s place=%d, skipping", race_date, place_code)
        return {"skipped": True}

    venue = VENUE_CODES.get(place_code, f"code{place_code}")
    logger.info("=== Ingest: %s %s (%d) ===", race_date, venue, place_code)

    counts: dict[str, int] = {}

    # 1) 開催プログラム取得
    try:
        prog_html = client.get_raceprogram_html(place_code, race_date)
    except Exception as e:
        logger.error("raceprogram fetch failed: %s", e)
        return counts

    pc0201 = extract_json_data(prog_html, "PC0201")
    if not pc0201 or not pc0201.get("C0201data"):
        logger.info("  No race on %s at %s", race_date, venue)
        return counts

    program = parse_program_meta(place_code, race_date, pc0201)
    if not program["races"]:
        logger.info("  No races available")
        return counts

    counts["meta"] = append_row("race_meta.csv", program["meta"])
    logger.info("  Meta: %s / grade=%s / %d races",
                program["meta"]["place_name"],
                program["meta"]["grade"],
                len(program["races"]))

    # 2) 出走表一覧 (1ページで全12R分が取れる)
    enc_para_r = program["races"][0]["enc_para_r"]
    if not enc_para_r:
        logger.error("  encParaR not found, cannot fetch racelist")
        return counts

    try:
        racelist_html = client.get_racelist_html(enc_para_r)
    except Exception as e:
        logger.error("racelist fetch failed: %s", e)
        return counts

    pj0305 = extract_json_data(racelist_html, "PJ0305")
    if not pj0305:
        logger.error("  PJ0305 not found in racelist HTML")
        return counts

    entries = parse_race_entries(place_code, race_date, pj0305)
    lines = parse_race_lines(place_code, race_date, pj0305)
    counts["entries"] = append_rows("race_entries.csv", entries)
    counts["lines"] = append_rows("race_lines.csv", lines)

    logger.info("=== Done: %s %s ===", race_date, venue)
    for name, count in sorted(counts.items()):
        logger.info("  %s: %d rows", name, count)
    return counts


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python ingest_day.py YYYY-MM-DD placeCode")
        print("  placeCode 例: 11=函館, 31=松戸, 42=名古屋, 87=熊本")
        sys.exit(1)

    race_date = sys.argv[1]
    place_code = int(sys.argv[2])
    client = KeirinClient()
    ingest_one_day(client, place_code, race_date)


if __name__ == "__main__":
    main()
