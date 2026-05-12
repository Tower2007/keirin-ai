"""race_odds.csv の軽量サンプル調査。

目的:
- 先頭 N 行だけを読み、レースごとの official_dt が単一か確認する
- race_entries.csv の st_time と比較し、official_dt が発走前かを粗く見る

依存は標準ライブラリのみ。大容量 CSV を全走査しない。
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


ODDS_HEADER = [
    "race_date",
    "place_code",
    "race_no",
    "bet_type",
    "kumi_ban",
    "odds",
    "max_odds",
    "popularity",
    "official_dt",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=r"D:\keirin-ai-data")
    parser.add_argument("--max-odds-rows", type=int, default=200_000)
    parser.add_argument(
        "--odds-stdin",
        action="store_true",
        help="標準入力からヘッダなし race_odds 行を読む。Get-Content -Tail 等との併用用。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    odds_path = data_dir / "race_odds.csv"
    entries_path = data_dir / "race_entries.csv"

    race_dts: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    race_rows: Counter[tuple[str, str, str]] = Counter()
    bet_counts: Counter[str] = Counter()

    if args.odds_stdin:
        import sys

        odds_file = sys.stdin
        close_odds = False
    else:
        odds_file = odds_path.open("r", encoding="utf-8", newline="")
        close_odds = True

    try:
        f = odds_file
        reader = csv.DictReader(f, fieldnames=ODDS_HEADER)
        for i, row in enumerate(reader, start=1):
            if i > args.max_odds_rows:
                break
            key = (row["race_date"], row["place_code"], row["race_no"])
            race_dts[key].add(row["official_dt"])
            race_rows[key] += 1
            bet_counts[row["bet_type"]] += 1
    finally:
        if close_odds:
            odds_file.close()

    target_races = set(race_dts)
    st_times: dict[tuple[str, str, str], str] = {}
    den_times: dict[tuple[str, str, str], str] = {}

    with entries_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["race_date"], row["place_code"], row["race_no"])
            if key in target_races and key not in st_times:
                st_times[key] = row.get("st_time", "")
                den_times[key] = row.get("den_time", "")
            if len(st_times) == len(target_races):
                break

    deltas: list[tuple[int, tuple[str, str, str], str, str, str]] = []
    missing_st = 0
    multi_dt = 0
    for key, dts in race_dts.items():
        if len(dts) != 1:
            multi_dt += 1
            continue
        st = st_times.get(key, "")
        official_dt = next(iter(dts))
        if not st or not official_dt:
            missing_st += 1
            continue
        official = datetime.strptime(official_dt, "%Y-%m-%d %H:%M:%S")
        start = datetime.strptime(f"{key[0]} {st}:00", "%Y-%m-%d %H:%M:%S")
        delta_min = int((official - start).total_seconds() // 60)
        deltas.append((delta_min, key, den_times.get(key, ""), st, official_dt))

    bins = Counter()
    for delta_min, *_ in deltas:
        if delta_min < -10:
            bins["<-10"] += 1
        elif delta_min < 0:
            bins["-10..-1"] += 1
        elif delta_min == 0:
            bins["0"] += 1
        elif delta_min <= 5:
            bins["1..5"] += 1
        else:
            bins[">5"] += 1

    print(f"odds_rows_sampled={sum(race_rows.values())}")
    print(f"races_sampled={len(race_rows)}")
    print(f"races_with_multiple_official_dt={multi_dt}")
    print(f"races_missing_st_time={missing_st}")
    print("bet_type_counts=" + ", ".join(f"{k}:{v}" for k, v in sorted(bet_counts.items())))
    print("official_minus_st_time_minutes_bins=" + ", ".join(f"{k}:{v}" for k, v in sorted(bins.items())))
    print()
    print("first_12_races:")
    for delta_min, key, den, st, official_dt in sorted(deltas, key=lambda x: x[1])[:12]:
        print(
            f"{key[0]} pc={key[1]} R{key[2]} "
            f"den={den} st={st} official_dt={official_dt} delta_min={delta_min}"
        )


if __name__ == "__main__":
    main()
