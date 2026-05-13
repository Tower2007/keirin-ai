"""race_odds.csv から per-(race, car) のオッズ特徴量 CSV を生成

Phase 6 6a-3: upper-bound 検証用。Codex 提案の 6 候補 + log scale を実装。

⚠️ 重要: race_odds.csv の official_dt は st_time + 0〜5 分に集中しており、
本番投票前に観測不可 (運用リーケージ)。ここで作る特徴量は upper-bound 性能の
参考値のみ。本番運用は race_odds_prerace.csv (pre-start snapshot) を別途使用。

実装方針 (Codex 提案):
- 第 1 段: ST2 と SH2 (実装済)
- 第 2 段: W (ワイド) を追加 (2026-05-13)
- 命名: imp_winprob_st2 / imp_top2prob_sh2 / imp_top3prob_w
- レース内正規化で sum=1 (元値は失う)
- log(odds_min) も raw scale として保持
- is_incomplete_st2 フラグでキャンセル車検出
- has_odds / has_w_odds フラグで欠損明示 (0 埋め禁止)

race_odds.csv の構造:
- ヘッダなし (Codex 発見)
- columns: race_date, place_code, race_no, bet_type, kumi_ban, odds, max_odds, popularity, official_dt
- ST2: "X-Y" (1着 X, 2着 Y)
- SH2: "X=Y" (X<Y、順序なし)
- W:   "X=Y" (X<Y、順序なし) + max_odds 列がワイドだけ非ゼロ

使い方:
  python build_odds_features.py
  python build_odds_features.py --max-rows 1000000  # サンプル動作確認
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

from src.config import DATA_DIR

sys.stdout.reconfigure(encoding="utf-8")

ODDS_HEADER = [
    "race_date", "place_code", "race_no",
    "bet_type", "kumi_ban", "odds", "max_odds",
    "popularity", "official_dt",
]

OUT_COLS = [
    "race_date", "place_code", "race_no", "car_no",
    # ─── ST2/SH2 系 (第 1 段、Codex 提案) ────
    "imp_winprob_st2",      # ST2 1着付けからの暗黙勝率 (sum=1 normalized)
    "imp_top2prob_sh2",     # SH2 contains-car の連対圏 marginal (正規化)
    "pop_rank_st2_1st",     # ST2 1着付けの min popularity
    "pop_rank_sh2_min",     # SH2 含む組合せの min popularity
    "odds_min_st2_1st",     # ST2 1着付けの min odds
    "log_odds_min_st2_1st", # log(odds_min)
    "n_st2_first",          # ST2 1着付け候補数 (期待値: n_cars - 1)
    "is_incomplete_st2",    # n_st2_first < expected = 1
    "has_odds",             # ST2 or SH2 にこの車の組合せが存在するか
    # ─── W 系 (第 2 段、2026-05-13 追加) ────
    "imp_top3prob_w",       # W contains-car の 3 着内圏 marginal (min odds ベース正規化)
    "pop_rank_w_min",       # W 含む組合せの min popularity
    "w_odds_min_avg",       # W min odds の平均 (車別下限の期待値)
    "w_odds_range_avg",     # W (max - min) odds の平均 (不確実性指標)
    "has_w_odds",           # W にこの車の組合せが存在するか
]


def safe_float(s: str) -> float | None:
    try:
        v = float(s)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def safe_int(s: str) -> int | None:
    try:
        return int(s) if s else None
    except (ValueError, TypeError):
        return None


def compute_race_features(race_key, rows) -> list[dict]:
    """1 レース分の rows から per-car 特徴量を計算。"""
    race_date, place_code, race_no = race_key

    # ST2 / SH2 / W を抽出
    st2_rows = []  # (1st_car, 2nd_car, odds, popularity)
    sh2_rows = []  # (car_a, car_b, odds, popularity)  ※昇順
    w_rows = []    # (car_a, car_b, odds_min, odds_max, popularity)  ※昇順
    cars_in_race: set[int] = set()

    for row in rows:
        bet_type = row[3]
        kumi_ban = row[4]
        odds = safe_float(row[5])
        max_odds = safe_float(row[6]) if len(row) > 6 else None
        pop = safe_int(row[7]) if len(row) > 7 else None
        if odds is None:
            continue

        if bet_type == "ST2":
            try:
                parts = [int(x) for x in kumi_ban.split("-")]
            except ValueError:
                continue
            if len(parts) != 2:
                continue
            st2_rows.append((parts[0], parts[1], odds, pop))
            cars_in_race.update(parts)
        elif bet_type == "SH2":
            try:
                parts = [int(x) for x in kumi_ban.split("=")]
            except ValueError:
                continue
            if len(parts) != 2:
                continue
            sh2_rows.append((parts[0], parts[1], odds, pop))
            cars_in_race.update(parts)
        elif bet_type == "W":
            try:
                parts = [int(x) for x in kumi_ban.split("=")]
            except ValueError:
                continue
            if len(parts) != 2:
                continue
            w_rows.append((parts[0], parts[1], odds, max_odds, pop))
            cars_in_race.update(parts)

    if not cars_in_race:
        return []

    n_cars = len(cars_in_race)
    expected_st2_first = n_cars - 1

    features: list[dict] = []
    for car in sorted(cars_in_race):
        # ST2 1着付け = car が 1st の rows
        st2_first = [(o, p) for (a, b, o, p) in st2_rows if a == car]
        # SH2 contains car
        sh2_contains = [(o, p) for (a, b, o, p) in sh2_rows
                        if a == car or b == car]
        # W contains car
        w_contains = [(o_min, o_max, p) for (a, b, o_min, o_max, p) in w_rows
                      if a == car or b == car]

        # ST2/SH2 系
        imp_winprob_st2 = sum(1.0 / o for o, _ in st2_first) if st2_first else None
        imp_top2prob_sh2 = sum(1.0 / o for o, _ in sh2_contains) if sh2_contains else None
        pops_st2 = [p for _, p in st2_first if p is not None]
        pops_sh2 = [p for _, p in sh2_contains if p is not None]
        pop_rank_st2_1st = min(pops_st2) if pops_st2 else None
        pop_rank_sh2_min = min(pops_sh2) if pops_sh2 else None
        odds_min_st2_1st = min(o for o, _ in st2_first) if st2_first else None
        log_odds_min = math.log(odds_min_st2_1st) if odds_min_st2_1st else None
        n_st2_first = len(st2_first)
        is_incomplete_st2 = 1 if n_st2_first < expected_st2_first else 0
        has_odds = 1 if (st2_first or sh2_contains) else 0

        # W 系
        imp_top3prob_w = (
            sum(1.0 / o_min for o_min, _, _ in w_contains if o_min)
            if w_contains else None
        )
        pops_w = [p for _, _, p in w_contains if p is not None]
        pop_rank_w_min = min(pops_w) if pops_w else None
        w_odds_mins = [o_min for o_min, _, _ in w_contains if o_min]
        w_odds_min_avg = (sum(w_odds_mins) / len(w_odds_mins)) if w_odds_mins else None
        w_odds_ranges = [
            (o_max - o_min) for o_min, o_max, _ in w_contains
            if o_min and o_max
        ]
        w_odds_range_avg = (
            sum(w_odds_ranges) / len(w_odds_ranges) if w_odds_ranges else None
        )
        has_w_odds = 1 if w_contains else 0

        features.append({
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "car_no": car,
            "imp_winprob_st2": imp_winprob_st2,
            "imp_top2prob_sh2": imp_top2prob_sh2,
            "pop_rank_st2_1st": pop_rank_st2_1st,
            "pop_rank_sh2_min": pop_rank_sh2_min,
            "odds_min_st2_1st": odds_min_st2_1st,
            "log_odds_min_st2_1st": log_odds_min,
            "n_st2_first": n_st2_first,
            "is_incomplete_st2": is_incomplete_st2,
            "has_odds": has_odds,
            "imp_top3prob_w": imp_top3prob_w,
            "pop_rank_w_min": pop_rank_w_min,
            "w_odds_min_avg": w_odds_min_avg,
            "w_odds_range_avg": w_odds_range_avg,
            "has_w_odds": has_w_odds,
        })

    # レース内正規化 (sum=1)
    total_imp_wp = sum(f["imp_winprob_st2"] for f in features
                       if f["imp_winprob_st2"] is not None)
    total_imp_tp = sum(f["imp_top2prob_sh2"] for f in features
                       if f["imp_top2prob_sh2"] is not None)
    total_imp_t3 = sum(f["imp_top3prob_w"] for f in features
                       if f["imp_top3prob_w"] is not None)
    for f in features:
        if f["imp_winprob_st2"] is not None and total_imp_wp > 0:
            f["imp_winprob_st2"] = f["imp_winprob_st2"] / total_imp_wp
        if f["imp_top2prob_sh2"] is not None and total_imp_tp > 0:
            f["imp_top2prob_sh2"] = f["imp_top2prob_sh2"] / total_imp_tp
        if f["imp_top3prob_w"] is not None and total_imp_t3 > 0:
            f["imp_top3prob_w"] = f["imp_top3prob_w"] / total_imp_t3

    return features


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rows", type=int, default=0,
                    help="0 = 全件, それ以外は先頭 N 行だけ処理 (デバッグ用)")
    ap.add_argument("--out", default=str(DATA_DIR / "odds_features.csv"))
    args = ap.parse_args()

    src = DATA_DIR / "race_odds.csv"
    out = Path(args.out)

    if not src.exists():
        sys.exit(f"race_odds.csv not found: {src}")

    print(f"Reading: {src}")
    print(f"Writing: {out}")
    print("-" * 60)

    t0 = time.time()
    current_race = None
    current_rows: list[list[str]] = []
    n_rows_in = 0
    n_races = 0
    n_features_out = 0

    with open(src, "r", encoding="utf-8", newline="") as fin, \
         open(out, "w", encoding="utf-8", newline="") as fout:
        reader = csv.reader(fin)
        writer = csv.DictWriter(fout, fieldnames=OUT_COLS)
        writer.writeheader()

        for row in reader:
            if len(row) < 9:
                continue
            n_rows_in += 1
            if args.max_rows and n_rows_in > args.max_rows:
                break

            race_key = (row[0], row[1], row[2])
            if current_race is None:
                current_race = race_key
                current_rows = [row]
            elif race_key == current_race:
                current_rows.append(row)
            else:
                # 前レース処理
                feats = compute_race_features(current_race, current_rows)
                for f in feats:
                    writer.writerow(f)
                n_features_out += len(feats)
                n_races += 1
                if n_races % 5000 == 0:
                    elapsed = time.time() - t0
                    rate = n_races / elapsed if elapsed else 0
                    print(f"  ... {n_races:,} races, {n_features_out:,} car-rows, "
                          f"{rate:.0f} races/sec")
                current_race = race_key
                current_rows = [row]

        # 最終レース flush
        if current_rows:
            feats = compute_race_features(current_race, current_rows)
            for f in feats:
                writer.writerow(f)
            n_features_out += len(feats)
            n_races += 1

    elapsed = time.time() - t0
    print("-" * 60)
    print(f"Input rows:    {n_rows_in:,}")
    print(f"Races:         {n_races:,}")
    print(f"Output rows:   {n_features_out:,}")
    print(f"Elapsed:       {elapsed:.1f} sec ({n_races / elapsed:.0f} races/sec)")
    print(f"Output:        {out}")


if __name__ == "__main__":
    main()
