"""prerace_snapshot_log.csv を race_odds_prerace.csv から再構築

2026-05-27 / 5/29 で daemon の一括 append 設計が PC sleep 等で log を取り損ねた。
race_odds_prerace.csv (実 odds データ) は逐次 append されていたので、
こちらから (race_date, place_code, race_no, target_offset_min, snapshot_dt) を
逆引きして snapshot_log を補完する。

実行後、log の status=ok 件数は race_odds_prerace.csv の "実際の取得済 venue-day"
と一致するはず。
"""

from __future__ import annotations

import csv
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# scripts/ 配下から起動した時の sys.path 調整
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DATA_DIR
from src.odds_prerace import iter_partition_rows, list_partition_files


def main() -> None:
    odds_files = list_partition_files(DATA_DIR)
    log_path = DATA_DIR / "prerace_snapshot_log.csv"

    if not odds_files:
        print(f"❌ {DATA_DIR} に race_odds_prerace_*.csv / race_odds_prerace.csv 不在")
        return

    # 既存 log を読んで dedup 用に保持
    existing_keys: set[tuple[str, str, str, str]] = set()
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                key = (r["race_date"], r["place_code"], r["race_no"],
                       r.get("target_offset_min", ""))
                existing_keys.add(key)
    print(f"既存 log entries: {len(existing_keys):,}")

    # race_odds_prerace.csv から (race, target_offset) 単位の集計
    # 同じ (race, offset) でも snapshot_dt が複数ある可能性は通常無いが、
    # ある場合は最初の snapshot_dt + 行数合計で 1 entry にまとめる
    Key = tuple[str, str, str, str]  # date, pc, race_no, target_offset_min
    info: dict[Key, dict] = {}

    # 月次パーティション + 旧単一ファイルを逐次走査 (2026-09-04)
    for r in iter_partition_rows(DATA_DIR):
        key = (r["race_date"], r["place_code"], r["race_no"],
               r.get("target_offset_min", ""))
        if not key[3]:
            continue  # 旧 schema (target_offset_min 無し) は除外
        if key in info:
            info[key]["n_rows"] += 1
        else:
            info[key] = {
                "race_date": r["race_date"],
                "place_code": r["place_code"],
                "race_no": r["race_no"],
                "st_time": "",  # race_odds_prerace には st_time 列無し
                "snapshot_dt": r.get("snapshot_dt", ""),
                "minutes_before_start": r.get("minutes_before_start", ""),
                "target_offset_min": key[3],
                "status": "ok",  # データが存在 = ok 扱い
                "n_rows": 1,
                "note": "rebuilt_from_odds_csv",
            }

    print(f"race_odds_prerace.csv 由来 entries: {len(info):,}")

    # 既存 log に無い entry だけ追加
    new_rows = [v for k, v in info.items() if k not in existing_keys]
    print(f"新規追加対象: {len(new_rows):,} (既存 dedup 後)")

    if not new_rows:
        print("追加なし、終了")
        return

    # backup
    if log_path.exists():
        bak = log_path.with_suffix(".csv.bak")
        shutil.copy2(log_path, bak)
        print(f"backup: {bak}")

    # 既存と新規をマージして書き出し (snapshot_dt 順)
    all_rows: list[dict] = []
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)
    all_rows.extend(new_rows)

    # 既存内の重複も除去 (2026-07-11 監査 P2: 5/31 に同一 ok 198 件が残り
    # 観測数を水増ししていた。マージ後の全行を同一キーで dedup する)
    dedup_key = ("race_date", "place_code", "race_no", "target_offset_min", "status")
    seen: set[tuple] = set()
    unique_rows: list[dict] = []
    for r in all_rows:
        key = tuple(r.get(k, "") for k in dedup_key)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(r)
    if len(unique_rows) < len(all_rows):
        print(f"既存内重複を除去: {len(all_rows) - len(unique_rows):,} 行")
    all_rows = unique_rows

    # snapshot_dt 順 (空文字 = 末尾)
    def sort_key(r):
        dt = r.get("snapshot_dt", "")
        return dt or "9999-12-31"

    all_rows.sort(key=sort_key)

    # st_time が空の rebuild 行は race_entries から補完したいが、
    # ここでは簡略化のため空のままにする (重要なのは status=ok の集計)
    # 列は storage.CSV_SCHEMAS を単一の真実とする (旧ハードコードは
    # スキーマ拡張時に新列を落とす事故になるため廃止)
    from src.storage import CSV_SCHEMAS
    cols = CSV_SCHEMAS["prerace_snapshot_log.csv"]
    with open(log_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"完了: {log_path} に {len(all_rows):,} entries")


if __name__ == "__main__":
    main()
