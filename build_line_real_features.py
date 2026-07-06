r"""実ライン特徴量生成 (公式並び race_lines.csv を本投入)

Phase 6 6a-2 の後継。build_line_weak_features.py は地区/県の代理ヒューリスティック
だったが、本スクリプトは **公式並び (race_lines.csv) の実データ** から
ライン構成を復元して per-car 特徴量を作る。

── race_lines.csv のエンコード (調査確定) ──────────────────────────────
  列: race_date, place_code, race_no, car_no, narabi_x, narabi_y
  narabi_x = トラック上のグローバル位置 (1..14、**欠番で区切り**)
  narabi_y = ライン束番号 (ほぼ 1。稀に 2/3 = 別枠並び)
  ライン復元則: (narabi_y, narabi_x) 昇順に並べ、narabi_x の差が
    >=2 で **ライン境界**、差 1 (or 0) は **同一ライン隣接**。
  例) 11R1: x=[1,2,3,5,7] car=[1,5,2,3,4] → [[1,5,2],[3],[4]]
  検証: 復元後のライン数/ライン人数分布は実競輪 (3〜4 ライン、単/2/3 車主体)
  と整合。全単騎レース (31R1 等) も正しく 7 単騎として復元される。
  データ範囲: 2026-04-26〜 (収集開始)。それ以前は NaN → LightGBM が native
  に欠損処理 (代理特徴ではなく「実データがある行だけ」効かせる正攻法)。

── 生成する per-car 実ライン特徴量 (12 種) ─────────────────────────────
  位置系:
    line_size            : 所属ラインの人数 (単騎=1)
    line_pos             : ライン内の並び位置 (先頭=1, 番手=2, 3番手=3...)
    is_line_head         : ライン先頭か (line_pos==1 かつ line_size>=2)
    is_line_second       : ライン番手 (2 番手) か
    is_line_third_plus   : ライン 3 番手以降か
    is_tanki             : 単騎か (line_size==1) ← 実データ確定 (代理の推定と別物)
    pos_ratio            : (line_pos-1)/(line_size-1) 単騎は 0。0=先頭,1=最後尾
  構成系:
    n_lines_in_race      : レース内ライン数 (少ないほど大ライン主導)
    max_line_size        : レース内最大ライン人数
    is_in_largest_line   : 最大ラインに属するか
    line_size_rank       : 所属ライン人数のレース内順位 (1=最大ライン, dense)
    n_tanki_in_race      : レース内の単騎数
  脚質×位置 (race_stats の脚質と交差):
    head_nige_propensity : ライン先頭なら nige_propensity、非先頭は 0
    (nige_propensity は build_line_weak と同式で再利用)

出力: D:\keirin-ai-data\line_real_features.csv (race_lines.csv を読むだけ、
       race_lines.csv 自体は書き換えない)

使い方:
  python build_line_real_features.py
"""

from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd

from src.config import DATA_DIR as DATA

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KEYS = ["race_date", "place_code", "race_no", "car_no"]
RKEYS = ["race_date", "place_code", "race_no"]

# ライン境界とみなす narabi_x の差 (>=2 で別ライン)。0/1 は同一ライン。
LINE_BREAK_GAP = 2


def reconstruct_lines(g: pd.DataFrame) -> pd.DataFrame:
    """1 レース分の並び (car_no, narabi_x, narabi_y) からライン構造を復元。

    返り値: car_no, line_id, line_pos, line_size を持つ DataFrame。
      line_id  : 0 始まりのライン番号 (先頭ラインから)
      line_pos : ライン内位置 (1=先頭)
    """
    g = g.sort_values(["narabi_y", "narabi_x"]).reset_index(drop=True)
    line_ids: list[int] = []
    line_pos: list[int] = []
    cur_id = 0
    pos = 0
    prev_x = None
    prev_y = None
    for _, row in g.iterrows():
        x = row["narabi_x"]
        y = row["narabi_y"]
        if prev_x is None:
            cur_id, pos = 0, 1
        elif (y != prev_y) or (x - prev_x >= LINE_BREAK_GAP):
            cur_id += 1
            pos = 1
        else:
            pos += 1
        line_ids.append(cur_id)
        line_pos.append(pos)
        prev_x, prev_y = x, y
    out = g[["car_no"]].copy()
    out["line_id"] = line_ids
    out["line_pos"] = line_pos
    out["line_size"] = out.groupby("line_id")["car_no"].transform("count")
    return out


def build_features() -> pd.DataFrame:
    lines = pd.read_csv(
        DATA / "race_lines.csv",
        usecols=RKEYS + ["car_no", "narabi_x", "narabi_y"],
        dtype={k: str for k in RKEYS},
    )
    for c in ["car_no", "narabi_x", "narabi_y"]:
        lines[c] = pd.to_numeric(lines[c], errors="coerce")
    # narabi_x/y 欠損行はライン復元不能なのでレース単位で扱えない → drop
    n0 = len(lines)
    lines = lines.dropna(subset=["car_no", "narabi_x", "narabi_y"])
    lines[["car_no", "narabi_x", "narabi_y"]] = lines[
        ["car_no", "narabi_x", "narabi_y"]
    ].astype(int)
    print(f"  race_lines: {n0:,} 行 → 有効 {len(lines):,} 行 "
          f"({lines['race_date'].min()}..{lines['race_date'].max()})")

    recon_parts = []
    for key, g in lines.groupby(RKEYS, sort=False):
        rec = reconstruct_lines(g)
        for i, k in enumerate(RKEYS):
            rec[k] = key[i]
        recon_parts.append(rec)
    rec = pd.concat(recon_parts, ignore_index=True)
    print(f"  復元済 per-car: {len(rec):,} 行 / "
          f"{rec.groupby(RKEYS).ngroups:,} レース")

    grp = rec.groupby(RKEYS, sort=False)

    # ── 位置系 ─────────────────────────────────────────
    rec["is_tanki"] = (rec["line_size"] == 1).astype(int)
    rec["is_line_head"] = ((rec["line_pos"] == 1) & (rec["line_size"] >= 2)).astype(int)
    rec["is_line_second"] = (rec["line_pos"] == 2).astype(int)
    rec["is_line_third_plus"] = (rec["line_pos"] >= 3).astype(int)
    denom = (rec["line_size"] - 1).replace(0, np.nan)
    rec["pos_ratio"] = ((rec["line_pos"] - 1) / denom).fillna(0.0)

    # ── 構成系 (race-level) ─────────────────────────────
    rec["n_lines_in_race"] = grp["line_id"].transform("nunique")
    rec["max_line_size"] = grp["line_size"].transform("max")
    rec["is_in_largest_line"] = (rec["line_size"] == rec["max_line_size"]).astype(int)
    rec["line_size_rank"] = grp["line_size"].rank(ascending=False, method="dense")
    rec["n_tanki_in_race"] = grp["is_tanki"].transform("sum")

    # ── 脚質 × 位置 (race_stats の脚質戦績を交差) ───────
    stats = pd.read_csv(
        DATA / "race_stats.csv", low_memory=False,
        usecols=RKEYS + ["car_no", "nige_cnt", "makuri_cnt", "sasi_cnt"],
        dtype={k: str for k in RKEYS},
    )
    stats["car_no"] = pd.to_numeric(stats["car_no"], errors="coerce")
    stats = stats.dropna(subset=["car_no"])
    stats["car_no"] = stats["car_no"].astype(int)
    for c in ["nige_cnt", "makuri_cnt", "sasi_cnt"]:
        stats[c] = pd.to_numeric(stats[c], errors="coerce").fillna(0)
    total_attacks = stats["nige_cnt"] + stats["makuri_cnt"] + stats["sasi_cnt"]
    stats["nige_propensity"] = stats["nige_cnt"] / (total_attacks + 1)

    rec = rec.merge(stats[RKEYS + ["car_no", "nige_propensity"]],
                    on=KEYS, how="left")
    rec["nige_propensity"] = rec["nige_propensity"].fillna(0.0)
    # 先頭車の逃げ志向 (ラインが先行力を持つかの実データ×戦績の交差項)
    rec["head_nige_propensity"] = np.where(
        rec["is_line_head"] == 1, rec["nige_propensity"], 0.0
    )

    out_cols = KEYS + [
        "line_size", "line_pos", "is_line_head", "is_line_second",
        "is_line_third_plus", "is_tanki", "pos_ratio",
        "n_lines_in_race", "max_line_size", "is_in_largest_line",
        "line_size_rank", "n_tanki_in_race", "head_nige_propensity",
    ]
    return rec[out_cols].copy()


def main() -> None:
    t0 = time.time()
    print("[1/2] 実ライン特徴量を構築 (race_lines.csv 復元)...")
    out = build_features()

    print("[2/2] 書き出し...")
    out_path = DATA / "line_real_features.csv"
    out.to_csv(out_path, index=False)
    print(f"  wrote {len(out):,} rows → {out_path} ({time.time() - t0:.1f}s)")

    print("\n=== 特徴量サマリ ===")
    print(f"is_tanki rate         : {out['is_tanki'].mean():.3f}")
    print(f"is_line_head rate     : {out['is_line_head'].mean():.3f}")
    print(f"is_line_second rate   : {out['is_line_second'].mean():.3f}")
    print(f"mean line_size        : {out['line_size'].mean():.2f}")
    print(f"mean n_lines_in_race  : {out['n_lines_in_race'].mean():.2f}")
    print(f"mean n_tanki_in_race  : {out['n_tanki_in_race'].mean():.2f}")
    print("line_size 分布:")
    print(out["line_size"].value_counts().sort_index().to_string())


if __name__ == "__main__":
    main()
