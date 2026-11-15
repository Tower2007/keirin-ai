"""正当な欠損 (全返還の不成立レース / 日全体の中止) の台帳 void_races.csv (2026-11-15)

race_completion は「結果・払戻が無い」を一律に取得漏れ候補 (missing_*) としていたが、
全返還 (不成立) のレースは着順表・払戻が元々サイトに無く、日全体の中止は race_meta に
is_canceled=False が先に入って (追記専用のため) 直らない。サイトで確認できた正当な欠損を
ここに根拠つきで残し、build_race_completion が欠測から除外する。

列: recorded_at, race_date, place_code, race_no, reason, note
  race_no が空 = その場・日の全レース (日全体の中止)。
  reason: full_refund (全返還=不成立) / day_canceled (開催中止)
キー = (race_date, place_code, race_no)。同じキーは二重に記録しない (冪等)。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime

from .config import DATA_DIR

VOIDS_NAME = "void_races.csv"
COLS = ["recorded_at", "race_date", "place_code", "race_no", "reason", "note"]
REASONS = ("full_refund", "day_canceled")


def load_voids(data_dir=None) -> list[dict]:
    path = (data_dir or DATA_DIR) / VOIDS_NAME
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def record_void(race_date: str, place_code, race_no, reason: str, note: str = "") -> bool:
    """正当な欠損を 1 件記録。既に同じキーがあれば False。race_no=None/"" は日全体。"""
    if reason not in REASONS:
        raise ValueError(f"unknown reason: {reason}")
    rn = "" if race_no in (None, "") else str(race_no)
    key = (race_date, str(place_code), rn)
    if any((v["race_date"], v["place_code"], v["race_no"]) == key for v in load_voids()):
        return False
    path = DATA_DIR / VOIDS_NAME
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new_file:
            w.writeheader()
        w.writerow({"recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "race_date": race_date, "place_code": str(place_code),
                    "race_no": rn, "reason": reason, "note": note})
    return True


def is_full_refund(jsj012: dict) -> bool:
    """JSJ012 (結果ページ) が全返還 (不成立) か。取得済みレスポンスだけで判定する。"""
    if jsj012.get("resultCd") != 0:
        return False
    return "全返還" in json.dumps(jsj012.get("haraiGakuSubData") or {}, ensure_ascii=False)
