"""台帳 (CSV) のキー重複検査と、全列一致の重複行の除去 (2026-11-12)

週次メールの「キー重複 0」検査と、payouts.csv の重複除去 (scripts 相当) を提供する。
背景: 7/11 の遡り取得で 2025-01-21 場74 R1-R9 と 2026-04-26 場87 R8 の払戻が二重に追記され
(70 組 140 行)、検査も掃除も無いまま残っていた。

使い方:
  python ledger_check.py              # 検査のみ (読み取り)
  python ledger_check.py --dedupe     # payouts.csv の全列一致の重複行を除去 (.bak を作ってから)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PAYOUT_KEY = ["race_date", "place_code", "race_no", "bet_type", "kumi_ban"]


def payouts_dup_keys(data_dir: Path) -> int:
    df = pd.read_csv(Path(data_dir) / "payouts.csv", usecols=PAYOUT_KEY, dtype=str, low_memory=False)
    return int(df.duplicated(PAYOUT_KEY).sum())


def shadow_official_dup_keys(data_dir: Path) -> int:
    """official 行の (race,car) 重複数。0 が正常 (rerun / legacy との重複は規則 shadow_eval で解決)。"""
    p = Path(data_dir) / "shadow_predictions.csv"
    if not p.exists():
        return 0
    df = pd.read_csv(p, usecols=["race_date", "place_code", "race_no", "car_no", "run_type"], dtype=str)
    df = df[df["run_type"] == "official"]
    return int(df.duplicated(["race_date", "place_code", "race_no", "car_no"]).sum())


def check_all(data_dir) -> dict:
    return {"payouts_dup_keys": payouts_dup_keys(data_dir),
            "shadow_official_dup_keys": shadow_official_dup_keys(data_dir)}


def dedupe_payouts(data_dir: Path, backup_dir: Path | None = None) -> dict:
    """payouts.csv の全列一致の重複行を除去する (先頭を残す)。キー重複でも列が異なる行は残す。

    実行前に .bak (または backup_dir) へコピーする。戻り値は前後の行数。
    """
    path = Path(data_dir) / "payouts.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    before = len(df)
    out = df.drop_duplicates(keep="first")
    removed = before - len(out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = (Path(backup_dir) / f"payouts.csv.bak-{stamp}") if backup_dir else path.with_name(
        f"payouts.csv.bak-{stamp}")
    if removed:
        shutil.copy2(path, bak)
        out.to_csv(path, index=False, lineterminator="\r\n")
    return {"before": before, "after": len(out), "removed": removed,
            "backup": str(bak) if removed else ""}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from src.config import DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--dedupe", action="store_true")
    args = ap.parse_args()
    print(check_all(DATA_DIR))
    if args.dedupe:
        print(dedupe_payouts(DATA_DIR))
        print(check_all(DATA_DIR))


if __name__ == "__main__":
    main()
