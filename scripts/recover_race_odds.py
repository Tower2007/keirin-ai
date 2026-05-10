"""破損した race_odds.csv の救出スクリプト

D:\\backup_keirin\\race_odds.csv は先頭に約 2.6 GB の NULL バイトを持ち、
その後に正常な CSV 行が約 86 万行続く形 (Drive Stream の "Free up space" で
キャッシュ末尾だけ残った状態)。

このスクリプトは:
  1. NULL 行を skip して正常行だけ抽出
  2. 抽出後の race_odds.csv を D:\\keirin-ai-data\\race_odds.csv に出力
  3. 含まれる venue-day を集計し backfill_odds_done.txt を再構築
     → 既に取れた範囲を再 backfill しないようにする

使い方:
  python scripts/recover_race_odds.py
"""

from __future__ import annotations

from pathlib import Path

SRC_ODDS = Path(r"D:\backup_keirin\race_odds.csv")
DST_DIR = Path(r"D:\keirin-ai-data")
DST_ODDS = DST_DIR / "race_odds.csv"
DST_DONE = DST_DIR / "backfill_odds_done.txt"


def is_valid_data_row(line: str) -> bool:
    """先頭が YYYY-MM-DD, で始まる行のみを有効と判定。"""
    if len(line) < 11:
        return False
    return (
        line[4] == "-"
        and line[7] == "-"
        and line[10] == ","
        and line[0:4].isdigit()
        and line[5:7].isdigit()
        and line[8:10].isdigit()
    )


def main() -> None:
    DST_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Reading: {SRC_ODDS}")
    print(f"Writing: {DST_ODDS}")
    print("-" * 60)

    venue_days: set[tuple[str, str]] = set()
    valid = 0
    skipped = 0

    with open(SRC_ODDS, "r", encoding="utf-8", errors="replace") as fin, \
         open(DST_ODDS, "w", encoding="utf-8", newline="") as fout:
        # 先頭の NULL 行を skip (readline が約 2.6 GB の NULL+\n を返す)
        first = fin.readline()
        print(f"Skipped leading line: {len(first):,} bytes "
              f"({'all NULL' if first[:1] == chr(0) else 'OTHER'})")

        for line in fin:
            if not is_valid_data_row(line):
                skipped += 1
                continue
            fout.write(line)
            valid += 1
            # venue-day を集計
            parts = line.split(",", 3)
            if len(parts) >= 2 and parts[1].isdigit():
                venue_days.add((parts[0], parts[1]))

            if valid % 100000 == 0:
                print(f"  ... {valid:,} rows, {len(venue_days):,} venue-days so far")

    print("-" * 60)
    print(f"Valid rows extracted:  {valid:,}")
    print(f"Skipped invalid rows:  {skipped:,}")
    print(f"Unique venue-days:     {len(venue_days):,}")

    # done.txt を再構築 (実際にデータが残っている venue-day のみ)
    print(f"\nWriting: {DST_DONE}")
    with open(DST_DONE, "w", encoding="utf-8") as f:
        for date, pc in sorted(venue_days):
            f.write(f"{date}|{pc}\n")
    print(f"Done entries: {len(venue_days):,}")

    # 救出データの日付範囲表示
    if venue_days:
        dates = sorted({d for d, _ in venue_days})
        print(f"\nDate range covered: {dates[0]} ~ {dates[-1]}")


if __name__ == "__main__":
    main()
