"""5 分前 odds snapshot daemon (1 日 1 回起動、コンソール非表示)

毎分 cron の代替: 1 日に 1 回だけ pythonw で起動して、当日のレース全部を
順次処理する。CMD ウィンドウは pythonw のため出ない (UX 改善)。

設計:
- 起動時に race_entries.csv から今日のレース一覧と st_time を読む
- 取得済 (prerace_snapshot_log.csv の status=ok) は除外
- 各レースについて snapshot 時刻 = st_time - 5 分 を計算
- snapshot_at が 2 分以上前のレースは「乗り遅れ」扱いで skip
- 残りを時刻順に処理:
    sleep until snapshot_at → capture → 次のレースへ
- 最終レース処理後に exit

ログは logs/cron_prerace_daemon_YYYY-MM-DD.log に書く (pythonw は stdout なし)。

7:00 朝の cron が race_entries.csv に当日データを書き込むため、本デーモンは
8:00 に起動するのが安全。entries 未準備の場合は最大 50 分まで retry してから
諦める。

使い方:
  pythonw ingest_odds_prerace_daemon.py            # cron 経由 (隠れる)
  python  ingest_odds_prerace_daemon.py            # 手動デバッグ (stdout 出る)
  python  ingest_odds_prerace_daemon.py 2026-05-13 # 日付指定 (テスト)
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# pythonw で起動された場合に cwd と sys.path を補正 (相対 import が壊れないように)
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DATA_DIR
from src.netkeirin_client import NetkeirinClient
from src.netkeirin_parser import parse_odds_lists
from src.storage import append_rows

# logging: pythonw だと stdout が無いのでファイル必須
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"cron_prerace_daemon_{date.today().isoformat()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),  # python (not pythonw) 時のみ stdout に出る
    ],
)
logger = logging.getLogger(__name__)

# 設定
SNAPSHOT_OFFSET_MIN = 5      # 発走 N 分前を狙う
ENTRIES_RETRY_MAX = 10       # entries 未準備時の retry 回数
ENTRIES_RETRY_INTERVAL = 300 # entries retry 待機 (秒) = 5 分
MISS_TOLERANCE_MIN = 2       # snapshot_at がこの分数以上前なら乗り遅れ扱い


def parse_st_time(race_date: str, st_time: str) -> datetime | None:
    """'HH:MM' or 'HH:MM:SS' を datetime に。空なら None。"""
    if not st_time:
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
            captured.add((row["race_date"], row["place_code"], row["race_no"]))
    return captured


def capture_race(
    client: NetkeirinClient,
    race_date: str,
    place_code: int,
    race_no: int,
    snapshot_dt: datetime,
    minutes_before: int,
) -> dict:
    """1 レースのオッズ snapshot を取得し、CSV へ追記。"""
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
    logger.info("=" * 60)
    logger.info("=== Daemon start: target_date=%s ===", target_date)
    logger.info("=" * 60)

    # entries を最大 50 分 retry (朝 7:00 lines cron 失敗対策)
    races: list[dict] = []
    for attempt in range(ENTRIES_RETRY_MAX):
        races = load_today_entries(target_date)
        if races:
            logger.info("Loaded %d unique races for %s", len(races), target_date)
            break
        logger.info("No race_entries for %s, retry %d/%d in %ds",
                    target_date, attempt + 1, ENTRIES_RETRY_MAX,
                    ENTRIES_RETRY_INTERVAL)
        time.sleep(ENTRIES_RETRY_INTERVAL)
    if not races:
        logger.info("No races after %d retries. Exit.", ENTRIES_RETRY_MAX)
        return

    captured = load_captured(target_date)
    logger.info("Already captured: %d", len(captured))

    # snapshot スケジュール組み立て
    schedule: list[tuple[dict, datetime, datetime]] = []
    for race in races:
        key = (race["race_date"], race["place_code"], race["race_no"])
        if key in captured:
            continue
        st_dt = parse_st_time(race["race_date"], race["st_time"])
        if st_dt is None:
            continue
        snapshot_at = st_dt - timedelta(minutes=SNAPSHOT_OFFSET_MIN)
        schedule.append((race, st_dt, snapshot_at))

    schedule.sort(key=lambda x: x[2])

    # 既に乗り遅れたレース除外
    now = datetime.now()
    cutoff = now - timedelta(minutes=MISS_TOLERANCE_MIN)
    fresh_schedule = [(r, st, sa) for r, st, sa in schedule if sa > cutoff]
    missed = len(schedule) - len(fresh_schedule)
    if missed:
        logger.info("Missed %d races (snapshot window already past)", missed)

    if not fresh_schedule:
        logger.info("Nothing to capture today. Exit.")
        return

    logger.info("Schedule: %d captures planned", len(fresh_schedule))
    for r, st, sa in fresh_schedule[:5]:
        logger.info("  %s pc=%s R%s: st=%s snapshot_at=%s",
                    r["race_date"], r["place_code"], r["race_no"],
                    st.strftime("%H:%M"), sa.strftime("%H:%M"))
    if len(fresh_schedule) > 5:
        logger.info("  ... (+%d more)", len(fresh_schedule) - 5)

    client = NetkeirinClient()
    log_rows: list[dict] = []

    for race, st_dt, snapshot_at in fresh_schedule:
        now = datetime.now()
        wait_sec = (snapshot_at - now).total_seconds()
        if wait_sec > 0:
            logger.info("Sleep %.0fs (%s) → %s pc=%s R%s @ %s",
                        wait_sec,
                        str(timedelta(seconds=int(wait_sec))),
                        race["race_date"], race["place_code"],
                        race["race_no"], snapshot_at.strftime("%H:%M:%S"))
            time.sleep(wait_sec)

        now = datetime.now()
        mins_before = int(round((st_dt - now).total_seconds() / 60.0))

        result = capture_race(
            client,
            race["race_date"], int(race["place_code"]), int(race["race_no"]),
            now, mins_before,
        )
        log_rows.append({
            "race_date": race["race_date"],
            "place_code": race["place_code"],
            "race_no": race["race_no"],
            "st_time": race["st_time"],
            "snapshot_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            "minutes_before_start": mins_before,
            "status": result["status"],
            "n_rows": result["n_rows"],
            "note": result["note"],
        })

    if log_rows:
        append_rows("prerace_snapshot_log.csv", log_rows)

    n_ok = sum(1 for r in log_rows if r["status"] == "ok")
    n_no = sum(1 for r in log_rows if r["status"] == "no_odds")
    n_fail = sum(1 for r in log_rows if r["status"] == "fail")
    logger.info("=" * 60)
    logger.info("=== Daemon done: ok=%d, no_odds=%d, fail=%d ===",
                n_ok, n_no, n_fail)
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Daemon crashed")
        raise
