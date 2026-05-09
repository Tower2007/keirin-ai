"""venue-day 単位のデータ品質サマリを生成

data/race_quality.csv を出力する。ML 学習時に欠損日や中止日をフィルタする用途。

使い方:
  python build_race_quality.py

出力カラム:
  race_date, place_code, place_name, grade,
  n_planned         : entries にあるレース数 (出走表が出た races)
  n_results         : tyaku が埋まった結果が記録されたレース数
  n_payouts         : payouts に存在するレース数
  n_missing_results : n_planned - n_results
  n_missing_payouts : n_planned - n_payouts
  completion_ratio  : n_payouts / n_planned
  status            : normal / partial_canceled / all_canceled / payout_missing
                      / canceled (中止/順延/打切で実レース無し、is_canceled=True)
                      / no_entries (entries 欠落の anomaly。通常 0 件想定)
"""

import sys
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR as DATA


def main() -> None:
    meta = pd.read_csv(DATA / "race_meta.csv")
    entries = pd.read_csv(
        DATA / "race_entries.csv",
        low_memory=False,
        usecols=["race_date", "place_code", "race_no"],
    )
    results = pd.read_csv(
        DATA / "race_results.csv",
        low_memory=False,
        usecols=["race_date", "place_code", "race_no", "tyaku"],
    )
    payouts = pd.read_csv(
        DATA / "payouts.csv",
        usecols=["race_date", "place_code", "race_no"],
    )

    # venue-day ごとの集計
    n_planned = (
        entries.groupby(["race_date", "place_code"])["race_no"]
        .nunique()
        .rename("n_planned")
    )
    n_results = (
        results[results["tyaku"].notna()]
        .groupby(["race_date", "place_code"])["race_no"]
        .nunique()
        .rename("n_results")
    )
    n_payouts = (
        payouts.groupby(["race_date", "place_code"])["race_no"]
        .nunique()
        .rename("n_payouts")
    )

    # is_canceled が無い旧スキーマでも動作するよう、欠落時は False で埋める
    if "is_canceled" not in meta.columns:
        meta["is_canceled"] = False
    else:
        meta["is_canceled"] = (
            meta["is_canceled"].astype(str).str.lower().isin(["true", "1"])
        )
    quality = meta[
        ["race_date", "place_code", "place_name", "grade", "is_canceled"]
    ].copy()
    quality = quality.merge(n_planned, on=["race_date", "place_code"], how="left")
    quality = quality.merge(n_results, on=["race_date", "place_code"], how="left")
    quality = quality.merge(n_payouts, on=["race_date", "place_code"], how="left")

    int_cols = ["n_planned", "n_results", "n_payouts"]
    quality[int_cols] = quality[int_cols].fillna(0).astype(int)
    quality["n_missing_results"] = quality["n_planned"] - quality["n_results"]
    quality["n_missing_payouts"] = quality["n_planned"] - quality["n_payouts"]

    ratio = quality["n_payouts"] / quality["n_planned"].replace(0, pd.NA)
    quality["completion_ratio"] = ratio.fillna(0).astype(float).round(4)

    def status_of(row: pd.Series) -> str:
        if row["is_canceled"]:
            return "canceled"
        if row["n_planned"] == 0:
            return "no_entries"
        if row["n_results"] == 0:
            return "all_canceled"
        if row["n_results"] < row["n_planned"]:
            return "partial_canceled"
        if row["n_payouts"] < row["n_results"]:
            return "payout_missing"
        return "normal"

    quality["status"] = quality.apply(status_of, axis=1)
    quality = quality.sort_values(["race_date", "place_code"]).reset_index(drop=True)

    out = DATA / "race_quality.csv"
    quality.to_csv(out, index=False)

    sys.stdout.reconfigure(encoding="utf-8")
    print(f"wrote {out} ({len(quality):,} venue-days)")
    print()
    print("=== status counts ===")
    print(quality["status"].value_counts().to_string())
    print()
    print("=== completion_ratio describe ===")
    print(quality["completion_ratio"].describe().to_string())
    print()
    print("=== sample: status != normal (head 15) ===")
    abnormal = quality[quality["status"] != "normal"]
    print(
        abnormal[
            [
                "race_date",
                "place_code",
                "place_name",
                "n_planned",
                "n_results",
                "n_payouts",
                "status",
            ]
        ]
        .head(15)
        .to_string(index=False)
    )


if __name__ == "__main__":
    main()
