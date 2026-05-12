"""CSV ファイル読み書きモジュール

data/ 配下に CSV を蓄積。ヘッダ自動付与、追記モード。
backfill と ingest_today_lines (cron) の並列実行に備え、書き込み時は
.lock ファイル経由で排他ロックを取る。
"""

import csv
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DATA_DIR  # noqa: F401  (再 export 用、外部スクリプトの後方互換)

logger = logging.getLogger(__name__)


if sys.platform == "win32":
    import msvcrt

    def _acquire(fd: int) -> None:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.1)

    def _release(fd: int) -> None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _acquire(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _file_lock(path: Path):
    """CSV ごとの排他ロック。隣接する .lock ファイルを介して取得する。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        _acquire(lf.fileno())
        try:
            yield
        finally:
            _release(lf.fileno())

# CSV ファイル名 → カラム定義
CSV_SCHEMAS: dict[str, list[str]] = {
    "race_meta.csv": [
        "race_date", "place_code", "place_name", "race_name", "grade",
        "sel_kaisai", "enc_para_k", "enc_para_s",
        "is_canceled",
    ],
    "race_entries.csv": [
        "race_date", "place_code", "race_no", "car_no",
        "player_code", "player_name", "prefecture",
        "kyaku", "yosoin", "itioshi_flag", "aisyo_flag", "assen",
        "syumoku", "den_time", "st_time",
        "line_type", "narabi_flg", "ozz_flg", "result_flg",
    ],
    "race_lines.csv": [
        "race_date", "place_code", "race_no", "car_no",
        "narabi_x", "narabi_y",
    ],
    "race_stats.csv": [
        "race_date", "place_code", "race_no", "car_no",
        "player_code", "player_name", "prefecture",
        "kyuhan", "prev_kyuhan", "kyakusitu",
        "sotugyouki", "age", "heikin_tokuten",
        "nige_cnt", "makuri_cnt", "sasi_cnt", "mark_cnt", "back_cnt",
        "home_tori", "st_tori",
        "syouritu", "rentairitu2", "rentairitu3",
        "ketujyou_tuika_hojyu",
        "shumoku_name", "kyori", "shukai", "kyoso_shurui", "yudo_sensyu",
    ],
    "race_results.csv": [
        "race_date", "place_code", "race_no", "tyaku", "car_no",
        "player_code", "player_name", "age", "prefecture",
        "sotugyouki", "kyuhan",
        "tyakusa", "agari", "kimarite", "bh", "in_line_jyuni",
        "kojin_state", "tyaku_note",
        "tenki", "husoku",
    ],
    "payouts.csv": [
        "race_date", "place_code", "race_no",
        "bet_type", "bet_name", "kumi_ban",
        "payout", "popularity",
    ],
    # netkeirin から取得した事前/確定オッズ。official_dt 時点の最新オッズ。
    # max_odds は ワイド (W) の上限、それ以外は NULL。
    "race_odds.csv": [
        "race_date", "place_code", "race_no",
        "bet_type", "kumi_ban", "odds", "max_odds",
        "popularity", "official_dt",
    ],
    # pre-start odds snapshot (発走 N 分前)。cron で本番運用向けに蓄積。
    # snapshot_dt は ローカル観測時刻、official_dt はソース側の表示時刻 (Codex/Gemini 指摘で分離)。
    # minutes_before_start = (st_time - snapshot_dt) を分単位で記録 (整数)。
    "race_odds_prerace.csv": [
        "race_date", "place_code", "race_no",
        "bet_type", "kumi_ban", "odds", "max_odds",
        "popularity", "official_dt",
        "snapshot_dt", "minutes_before_start",
    ],
    # prerace snapshot 取得試行ログ (成功/失敗、リトライ管理用)。
    # status: ok / no_odds / fail
    "prerace_snapshot_log.csv": [
        "race_date", "place_code", "race_no",
        "st_time", "snapshot_dt", "minutes_before_start",
        "status", "n_rows", "note",
    ],
}


def _ensure_header(path: Path, columns: list[str]) -> None:
    """ファイルが無い or 空ならヘッダ行を書き込む。"""
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()


def append_rows(csv_name: str, rows: list[dict]) -> int:
    """CSV に行を追記。戻り値は追記した行数。"""
    if not rows:
        return 0

    if csv_name not in CSV_SCHEMAS:
        raise ValueError(f"Unknown CSV: {csv_name}")

    columns = CSV_SCHEMAS[csv_name]
    path = DATA_DIR / csv_name

    with _file_lock(path):
        _ensure_header(path, columns)
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writerows(rows)

    logger.debug("Appended %d rows to %s", len(rows), path)
    return len(rows)


def append_row(csv_name: str, row: dict) -> int:
    """1行追記のヘルパー。"""
    return append_rows(csv_name, [row])


def read_csv(csv_name: str) -> list[dict]:
    """CSV を全行読み込み。ファイル無しは空リスト。"""
    path = DATA_DIR / csv_name
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def row_count(csv_name: str) -> int:
    """CSV の行数（ヘッダ除く）。"""
    path = DATA_DIR / csv_name
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


def has_race_day(csv_name: str, place_code: int, race_date: str) -> bool:
    """指定 race-day が既に CSV に存在するか（重複投入防止）。

    バックフィルなど多数の venue-day を処理する場合は、毎回 CSV をフルスキャン
    すると O(N) × 件数で激重になる。`RaceDayIndex` を使うことで O(1) になる。
    """
    path = DATA_DIR / csv_name
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("place_code") == str(place_code) and row.get("race_date") == race_date:
                return True
    return False


class RaceDayIndex:
    """5 つの CSV それぞれの (race_date, place_code) を起動時に set へ展開し、
    存在チェックを O(1) にするためのインメモリインデックス。

    バックフィル等の長時間ジョブで使う。書き込み後は `mark()` で同期する。
    """

    CSV_NAMES = (
        "race_meta.csv",
        "race_entries.csv",
        "race_lines.csv",
        "race_stats.csv",
        "race_results.csv",
    )

    def __init__(self) -> None:
        self._sets: dict[str, set[tuple[str, int]]] = {
            name: self._load(name) for name in self.CSV_NAMES
        }

    @staticmethod
    def _load(csv_name: str) -> set[tuple[str, int]]:
        path = DATA_DIR / csv_name
        if not path.exists():
            return set()
        result: set[tuple[str, int]] = set()
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pc = row.get("place_code")
                rd = row.get("race_date")
                if not pc or not rd:
                    continue
                try:
                    result.add((rd, int(pc)))
                except ValueError:
                    continue
        return result

    def has(self, csv_name: str, place_code: int, race_date: str) -> bool:
        return (race_date, place_code) in self._sets.get(csv_name, set())

    def mark(self, csv_name: str, place_code: int, race_date: str) -> None:
        self._sets.setdefault(csv_name, set()).add((race_date, place_code))

    def sizes(self) -> dict[str, int]:
        return {name: len(s) for name, s in self._sets.items()}
