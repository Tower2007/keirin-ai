"""prerace_snapshot_log.csv の v2 移行 (2026-07-11 監査 P1-5 / P2 対応)

やること (1 回だけ実行するワンショット):
  1. 既存内重複の除去 — rebuild_snapshot_log.py が既存 log 内の重複を残していた
     (5/31 に同一 ok 行 198 件など)。(race_date, place_code, race_no,
     target_offset_min, status) をキーに最初の 1 行を残す。
  2. 秒精度 3 列のバックフィル:
       scheduled_snapshot_dt = st_time - target_offset_min (予定時刻)
       seconds_before_start  = (st_time - snapshot_dt) 秒 (負 = 発走後取得)
       capture_lag_sec       = (snapshot_dt - scheduled) 秒
     st_time / snapshot_dt が欠ける行は空欄のまま。
  3. 新スキーマ (storage.CSV_SCHEMAS) でヘッダごと書き直し。

元ファイルは .bak_v1_YYYYMMDD にバックアップしてから上書きする。

使い方:
  python scripts/migrate_snapshot_log_v2.py          # dry-run (集計のみ)
  python scripts/migrate_snapshot_log_v2.py --apply  # 実行
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DATA_DIR
from src.storage import CSV_SCHEMAS

LOG_PATH = DATA_DIR / "prerace_snapshot_log.csv"
DEDUP_KEY = ("race_date", "place_code", "race_no", "target_offset_min", "status")


def _parse_dt(race_date: str, st_time: str) -> datetime | None:
    if not race_date or not st_time:
        return None
    try:
        parts = st_time.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) >= 3 else 0
        d = date.fromisoformat(race_date)
        return datetime(d.year, d.month, d.day, h, m, s)
    except (ValueError, IndexError):
        return None


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き換える")
    args = ap.parse_args()

    with open(LOG_PATH, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"読込: {len(rows):,} 行")

    # 1. dedup (最初の 1 行を残す)
    seen: set[tuple] = set()
    kept: list[dict] = []
    for r in rows:
        key = tuple(r.get(k, "") for k in DEDUP_KEY)
        if key in seen:
            continue
        seen.add(key)
        kept.append(r)
    n_dup = len(rows) - len(kept)
    print(f"重複除去: {n_dup:,} 行削除 → {len(kept):,} 行")

    # 2. 秒精度 3 列バックフィル
    n_filled = 0
    for r in kept:
        if r.get("scheduled_snapshot_dt"):
            continue  # 既に新形式 (daemon 更新後の行)
        st_dt = _parse_dt(r.get("race_date", ""), r.get("st_time", ""))
        snap_dt = None
        raw_snap = r.get("snapshot_dt", "")
        if raw_snap:
            try:
                snap_dt = datetime.strptime(raw_snap, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        off = None
        try:
            off = float(r.get("target_offset_min", ""))
        except (ValueError, TypeError):
            pass
        if st_dt is not None and off is not None:
            sched = st_dt - timedelta(minutes=off)
            r["scheduled_snapshot_dt"] = sched.strftime("%Y-%m-%d %H:%M:%S")
            if snap_dt is not None:
                r["seconds_before_start"] = int((st_dt - snap_dt).total_seconds())
                r["capture_lag_sec"] = round((snap_dt - sched).total_seconds(), 1)
            n_filled += 1
    print(f"バックフィル: {n_filled:,} 行に scheduled/seconds/lag を付与")

    # 集計サマリ (発走後取得の検出)
    n_post = sum(1 for r in kept
                 if str(r.get("seconds_before_start", "")).lstrip("-").isdigit()
                 and int(r["seconds_before_start"]) < 0)
    print(f"発走後取得 (seconds_before_start < 0): {n_post} 行")

    if not args.apply:
        print("\n[dry-run] --apply で書き換え実行")
        return

    # 3. バックアップ → 新スキーマで書き直し
    bak = LOG_PATH.with_suffix(f".csv.bak_v1_{date.today().strftime('%Y%m%d')}")
    shutil.copy2(LOG_PATH, bak)
    print(f"バックアップ: {bak}")

    columns = CSV_SCHEMAS["prerace_snapshot_log.csv"]
    with open(LOG_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)
    print(f"書換完了: {len(kept):,} 行 × {len(columns)} 列")


if __name__ == "__main__":
    main()
