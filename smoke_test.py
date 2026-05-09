"""1日分スモークテスト

指定の場・日付で raceprogram + racelist を取得し、
data/smoke/ に HTML と抽出済み JSON を保存。

使い方:
  python smoke_test.py YYYY-MM-DD placeCode
  python smoke_test.py 2026-04-26 42      # 名古屋 2026-04-26
"""

import json
import sys
import logging
from pathlib import Path

from src.client import KeirinClient, VENUE_CODES
from src.parser import (
    extract_json_data,
    parse_program_meta,
    parse_race_entries,
    parse_race_lines,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

from src.config import DATA_DIR
SMOKE_DIR = DATA_DIR / "smoke"


def save_text(filename: str, text: str) -> Path:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    path = SMOKE_DIR / filename
    path.write_text(text, encoding="utf-8")
    logger.info("Saved %s (%d bytes)", path, path.stat().st_size)
    return path


def save_json(filename: str, data: object) -> Path:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    path = SMOKE_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Saved %s (%d bytes)", path, path.stat().st_size)
    return path


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python smoke_test.py YYYY-MM-DD placeCode")
        print("  placeCode: 11=函館, 31=松戸, 42=名古屋, 87=熊本, ...")
        sys.exit(1)

    race_date = sys.argv[1]
    place_code = int(sys.argv[2])
    venue_name = VENUE_CODES.get(place_code, f"code{place_code}")
    prefix = f"{race_date}_{place_code}_{venue_name}"

    client = KeirinClient()
    logger.info("=== Smoke test: %s %s (%d) ===", race_date, venue_name, place_code)

    # 1) JSJ048 (today's race list across all venues)
    try:
        today = client.get_today_race_list()
        save_json(f"{prefix}_JSJ048_today.json", today)
        n_races = len(today.get("RaceList", []))
        logger.info("  JSJ048: %d races today across all venues", n_races)
    except Exception as e:
        logger.warning("  JSJ048 failed: %s", e)

    # 2) Race program HTML
    try:
        prog_html = client.get_raceprogram_html(place_code, race_date)
        save_text(f"{prefix}_raceprogram.html", prog_html)

        pc0201 = extract_json_data(prog_html, "PC0201")
        if not pc0201:
            logger.error("  PC0201 not found in raceprogram HTML")
            sys.exit(1)
        save_json(f"{prefix}_PC0201.json", pc0201)

        program = parse_program_meta(place_code, race_date, pc0201)
        logger.info("  Race meta: place=%s grade=%s race=%s",
                    program["meta"]["place_name"],
                    program["meta"]["grade"],
                    program["meta"]["race_name"])
        logger.info("  %d races available", len(program["races"]))
    except Exception as e:
        logger.error("  raceprogram failed: %s", e)
        sys.exit(1)

    # 3) Race list HTML (uses encParaR from race 1)
    if not program["races"]:
        logger.error("  No races to fetch racelist for")
        sys.exit(1)

    enc_para_r = program["races"][0]["enc_para_r"]
    if not enc_para_r:
        logger.error("  encParaR for race 1 is empty")
        sys.exit(1)

    try:
        racelist_html = client.get_racelist_html(enc_para_r)
        save_text(f"{prefix}_racelist.html", racelist_html)

        pj0305 = extract_json_data(racelist_html, "PJ0305")
        if not pj0305:
            logger.error("  PJ0305 not found in racelist HTML")
            sys.exit(1)
        save_json(f"{prefix}_PJ0305.json", pj0305)

        entries = parse_race_entries(place_code, race_date, pj0305)
        lines = parse_race_lines(place_code, race_date, pj0305)
        logger.info("  Entries: %d player rows", len(entries))
        logger.info("  Lines: %d narabi rows", len(lines))

        # サンプル表示
        if entries:
            sample = entries[0]
            logger.info("  Sample entry: R%d 車%s %s (%s) 脚=%s 印=%s",
                        sample["race_no"], sample["car_no"],
                        sample["player_name"], sample["prefecture"],
                        sample["kyaku"], sample["yosoin"])
    except Exception as e:
        logger.error("  racelist failed: %s", e)
        sys.exit(1)

    print("\n=== Smoke test complete ===")
    print(f"  Output dir: {SMOKE_DIR.resolve()}")


if __name__ == "__main__":
    main()
