"""取得できなかった (事後にも取れない) データの欠測台帳 data_gaps.csv (2026-11-12)

PC 停止などで「その日にしか取れないもの」(朝の lines / 発走前オッズ / shadow 予測) を
取り逃したとき、黙って消えないよう理由つきで残す。週次レポートが直近分を表示する。
キー = (kind, race_date, place_code)。同じキーは二重に記録しない (冪等)。

kind 例: lines / prerace_odds / shadow / results_unrecoverable
"""

from __future__ import annotations

import csv
from datetime import datetime

from .config import DATA_DIR

GAPS_NAME = "data_gaps.csv"
COLS = ["recorded_at", "kind", "race_date", "place_code", "note"]


def load_gaps() -> list[dict]:
    path = DATA_DIR / GAPS_NAME
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def record_gap(kind: str, race_date: str, place_code: str = "", note: str = "") -> bool:
    """欠測を 1 件記録する。既に同じキーがあれば False (何もしない)。"""
    key = (kind, race_date, str(place_code))
    if any((g["kind"], g["race_date"], g["place_code"]) == key for g in load_gaps()):
        return False
    path = DATA_DIR / GAPS_NAME
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new_file:
            w.writeheader()
        w.writerow({"recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "kind": kind, "race_date": race_date,
                    "place_code": str(place_code), "note": note})
    return True
