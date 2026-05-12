"""発走 5 分前 pre-start odds snapshot 取得 (cron 駆動、毎分実行)

本日のレースで「st_time - now が [4, 6] 分」窓に入っている未取得レースの
odds を netkeirin から取得して race_odds_prerace.csv に追記。
取得試行は prerace_snapshot_log.csv に記録する (成功・失敗ともに)。

設計上の重要事項 (Codex/Gemini 指摘):
- official_dt はソース側の表示時刻、snapshot_dt はこちらの観測時刻として分離。
- 5 分前は auto の実証値 (LEAD_MIN=5、10 分前は late money 前で EV 上振れ)。
- 同レースを 2 度取らないよう prerace_snapshot_log.csv の status=ok で dedup。

使い方:
  python ingest_odds_prerace.py           # 本日のレースを対象に走査
  python ingest_odds_prerace.py 2026-05-13  # 指定日 (テスト用)
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import DATA_DIR
from src.netkeirin_client import NetkeirinClient
from src.netkeirin_parser import parse_odds_lists
from src.storage import append_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# 発走 N 分前 snapshot 窓 (両端含む)
WINDOW_MIN = 4
WINDOW_MAX = 6
# 取得対象日のレース。default は本日
TODAY_OVERRIDE: str | None = None


def parse_st_time(race_date: str, st_time: str) -> datetime | None:
    """'HH:MM' or 'HH:MM:SS' を datetime に。空なら None。"""
    if not st_time or st_time == "":
        return None
    try:
        parts = st_time.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) >= 3 else 0
        d = date.fromisoformat(race_date)
        return datetime(d.year, d.month, d.day, h, m, s)
    except (ValueError, IndexError):
        return None


def load_today_entries(target_date: str) -> list[dict]:
    """race_entries.csv から target_date のユニーク (race, st_time) 一覧を返す。"""
    path = DATA_DIR / "race_entries.csv"
    if not path.exists():
        logger.warning("race_entries.csv not found: %s", path)
        return []

    seen: set[tuple[str, str, str]] = set()
    races: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("race_date") != target_date:
                continue
            key = (row["race_date"], row["place_code"], row["race_no"])
            if key in seen:
                continue
            seen.add(key)
            races.append({
                "race_date": row["race_date"],
                "place_code": row["place_code"],
                "race_no": row["race_no"],
                "st_time": row.get("st_time", ""),
            })
    return races


def load_captured(target_date: str) -> set[tuple[str, str, str]]:
    """既に status=ok で取得済みの (date, place, race_no) を返す。"""
    path = DATA_DIR / "prerace_snapshot_log.csv"
    if not path.exists():
        return set()

    captured: set[tuple[str, str, str]] = set()
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("race_date") != target_date:
                continue
            if row.get("status") != "ok":
                continue
            captured.add((
                row["race_date"], row["place_code"], row["race_no"],
            ))
    return captured


def capture_race(
    client: NetkeirinClient,
    race_date: str,
    place_code: int,
    race_no: int,
    st_time_str: str,
    snapshot_dt: datetime,
    minutes_before: int,
) -> dict:
    """1 レースのオッズ snapshot を取得し、CSV へ追記。

    戻り値: {"status": "ok"|"no_odds"|"fail", "n_rows": int, "note": str}
    """
    race_id = NetkeirinClient.make_race_id(race_date, place_code, race_no)
    snapshot_iso = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        odds_data = client.get_odds(race_id)
    except Exception as e:
        logger.error("  R%d (%s) FETCH ERROR: %s", race_no, race_id, e)
        return {"status": "fail", "n_rows": 0, "note": f"fetch_error: {e}"[:200]}

    if odds_data is None:
        logger.info("  R%d (%s) NG (no odds)", race_no, race_id)
        return {"status": "no_odds", "n_rows": 0, "note": "netkeirin_NG"}

    rows = parse_odds_lists(place_code, race_date, race_no, odds_data)
    if not rows:
        return {"status": "no_odds", "n_rows": 0, "note": "empty_rows"}

    # snapshot_dt と minutes_before_start を全行に付与
    for r in rows:
        r["snapshot_dt"] = snapshot_iso
        r["minutes_before_start"] = minutes_before

    n = append_rows("race_odds_prerace.csv", rows)
    logger.info(
        "  R%d: captured %d rows (mins_before=%d, official_dt=%s)",
        race_no, n, minutes_before, odds_data.get("official_dt", ""),
    )
    return {"status": "ok", "n_rows": n, "note": ""}


def main() -> None:
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    now = datetime.now()

    logger.info("=== prerace odds snapshot: target_date=%s, now=%s ===",
                target_date, now.strftime("%H:%M:%S"))

    races = load_today_entries(target_date)
    if not races:
        logger.info("No races for %s in race_entries.csv. Exit.", target_date)
        return

    captured = load_captured(target_date)
    if captured:
        logger.info("Already captured: %d races", len(captured))

    # 取得対象を絞り込み
    to_capture: list[dict] = []
    for race in races:
        key = (race["race_date"], race["place_code"], race["race_no"])
        if key in captured:
            continue
        st_dt = parse_st_time(race["race_date"], race["st_time"])
        if st_dt is None:
            continue
        mins_before = (st_dt - now).total_seconds() / 60.0
        if WINDOW_MIN <= mins_before <= WINDOW_MAX:
            to_capture.append({
                **race,
                "st_dt": st_dt,
                "minutes_before": int(round(mins_before)),
            })

    if not to_capture:
        logger.info("No races in window [%d, %d] min before start. Exit.",
                    WINDOW_MIN, WINDOW_MAX)
        return

    logger.info("To capture: %d races", len(to_capture))
    client = NetkeirinClient()

    log_rows: list[dict] = []
    for race in to_capture:
        result = capture_race(
            client,
            race["race_date"],
            int(race["place_code"]),
            int(race["race_no"]),
            race["st_time"],
            now,
            race["minutes_before"],
        )
        log_rows.append({
            "race_date": race["race_date"],
            "place_code": race["place_code"],
            "race_no": race["race_no"],
            "st_time": race["st_time"],
            "snapshot_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            "minutes_before_start": race["minutes_before"],
            "status": result["status"],
            "n_rows": result["n_rows"],
            "note": result["note"],
        })

    append_rows("prerace_snapshot_log.csv", log_rows)

    n_ok = sum(1 for r in log_rows if r["status"] == "ok")
    n_no_odds = sum(1 for r in log_rows if r["status"] == "no_odds")
    n_fail = sum(1 for r in log_rows if r["status"] == "fail")
    logger.info("=== Done: ok=%d, no_odds=%d, fail=%d ===",
                n_ok, n_no_odds, n_fail)


if __name__ == "__main__":
    main()
