"""CSV ファイル読み書きモジュール

data/ 配下に CSV を蓄積。ヘッダ自動付与、追記モード。
"""

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")

# CSV ファイル名 → カラム定義
CSV_SCHEMAS: dict[str, list[str]] = {
    "race_meta.csv": [
        "race_date", "place_code", "place_name", "race_name", "grade",
        "sel_kaisai", "enc_para_k", "enc_para_s",
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
    """指定 race-day が既に CSV に存在するか（重複投入防止）。"""
    path = DATA_DIR / csv_name
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("place_code") == str(place_code) and row.get("race_date") == race_date:
                return True
    return False
