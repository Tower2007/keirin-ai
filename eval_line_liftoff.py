r"""実ライン本投入の coef_model 離陸測定 (A/B、測定専用・非破壊)

market_blend_eval.py は ml_pred_probs.csv (production, 差分再生成) を読むが、
週次採用の差分再生成は blend 窓のごく一部しか新モデルで置換しない。よって
本スクリプトで **同一条件 (train_end=2026-05-11, 全 blend 窓 OOS)** の
per-car OOS 確率を 2 系統生成して A/B する:

  A (before): no-line モデル (.prev.lgb バックアップ = 直前の 41 特徴量モデル)
  B (after) : line モデル (ml_weekly_model_*.lgb = 54 特徴量、実ライン投入)

いずれも学習期間 (〜2026-05-11) の後 (2026-05-12〜) を予測するので in-sample
リークなし。production の ml_pred_probs.csv は一切書き換えない。

出力: scratchpad の ml_pred_probs_{before,after}.csv (market_blend_eval が
--probs で読める形式)。D: のデータは書かない。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import lightgbm as lgb

from src.config import DATA_DIR as DATA
import ml_baseline as mlb
import weekly_retrain as wr

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
PRED_START = "2026-05-12"   # train_end+1 (完全 OOS)
PRED_END = "2026-07-06"
KEYS = ["race_date", "place_code", "race_no"]

MODEL_DIR = DATA / "weekly_model"

FEAT_NO_LINE = list(mlb.FEAT_NUM) + list(mlb.FEAT_CAT)
FEAT_LINE = list(mlb.FEAT_NUM) + list(mlb.LINE_REAL_NUM_FEATURES) + list(mlb.FEAT_CAT)


def _predict(models: dict, sub: pd.DataFrame, feat_cols: list[str]) -> pd.DataFrame:
    out = sub[KEYS + ["car_no"]].copy()
    out["p_win"] = models["y_win"].predict(sub[feat_cols])
    out["p_top2"] = models["y_top2"].predict(sub[feat_cols])
    out["p_top3"] = models["y_top3"].predict(sub[feat_cols])
    out["pred_rank"] = models["y_rank"].predict(sub[feat_cols])
    return out


def _load_models(suffix: str) -> dict:
    """suffix='' → 現行(line)、'.prev' → 直前(no-line)。"""
    models = {}
    for t in ["y_win", "y_top2", "y_top3", "y_rank"]:
        p = MODEL_DIR / f"ml_weekly_model_{t}{suffix}.lgb"
        models[t] = lgb.Booster(model_file=str(p))
    return models


def main() -> None:
    print("[1] dataset 構築 (実ライン込み)")
    data, data_max = wr.load_data_extended()
    df = mlb.build_dataset(data)
    sub = df[(df["race_date"] >= PRED_START) & (df["race_date"] <= PRED_END)].copy()
    print(f"  予測対象: {len(sub):,} 行 ({PRED_START}..{PRED_END})")

    # B (after): line モデル (現行採用)
    print("[2] AFTER (line 54feat) 予測")
    mb = _load_models("")
    pb = _predict(mb, sub, FEAT_LINE)
    (OUT_DIR / "ml_pred_probs_after.csv").write_text(
        pb.to_csv(index=False), encoding="utf-8")
    print(f"  wrote ml_pred_probs_after.csv ({len(pb):,} 行)")

    # A (before): no-line モデル (.prev バックアップ)
    prev_exists = (MODEL_DIR / "ml_weekly_model_y_win.prev.lgb").exists()
    if prev_exists:
        print("[3] BEFORE (no-line 41feat, .prev) 予測")
        ma = _load_models(".prev")
        pa = _predict(ma, sub, FEAT_NO_LINE)
        (OUT_DIR / "ml_pred_probs_before.csv").write_text(
            pa.to_csv(index=False), encoding="utf-8")
        print(f"  wrote ml_pred_probs_before.csv ({len(pa):,} 行)")
    else:
        print("[3] .prev モデルなし → BEFORE はスキップ")


if __name__ == "__main__":
    main()
