"""W (ワイド) odds 特徴量の効果検証 (Phase 6 N2)

with_odds (ST2/SH2 のみ) と with_odds_w (W 追加) を比較。
y_top3 で効く可能性を中心に確認。

⚠️ どちらも post-start odds ベースの upper-bound 性能。

出力: D:\\keirin-ai-data\\w_odds_compare_report.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR as DATA

CONFIGS = [
    {"label": "with_odds",    "suffix": "_with_odds",   "args": ["--use-odds"]},
    {"label": "with_odds_w",  "suffix": "_with_odds_w", "args": ["--use-odds", "--use-w-odds"]},
]


def run_one(args: list[str], label: str) -> None:
    cmd = [sys.executable, "ml_baseline.py"] + args
    print(f"\n{'=' * 70}")
    print(f">>> Running ml_baseline.py  ({label})")
    print("=" * 70)
    subprocess.check_call(cmd)


def load_one(suffix: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    bt = pd.read_csv(DATA / f"ml_baseline_v2_result{suffix}.csv")
    th = pd.read_csv(DATA / f"ml_threshold_result{suffix}.csv")
    return bt, th


def generate_report() -> None:
    out = DATA / "w_odds_compare_report.md"
    lines: list[str] = []
    lines.append("# W (ワイド) odds 特徴量 効果検証 (Phase 6 N2)")
    lines.append("")
    lines.append("⚠️ どちらも race_odds.csv (post-start = リーケージあり) ベースの")
    lines.append("upper-bound 性能。本番では再現不能。")
    lines.append("")

    # 全レース ROI
    lines.append("## 1. 全レース購入 ROI 比較")
    lines.append("")
    rows = []
    for cfg in CONFIGS:
        bt, _ = load_one(cfg["suffix"])
        d = bt.set_index("bet_type")["roi"].to_dict()
        rows.append({"config": cfg["label"], **d})
    df = pd.DataFrame(rows).set_index("config")
    lines.append(df.round(4).to_markdown())
    lines.append("")

    # 差分
    lines.append("### 差分: with_odds_w - with_odds (W 追加効果)")
    lines.append("")
    base_bt, _ = load_one("_with_odds")
    w_bt, _ = load_one("_with_odds_w")
    diff = (w_bt.set_index("bet_type")["roi"] -
            base_bt.set_index("bet_type")["roi"]).round(4)
    lines.append(diff.to_frame("delta_roi").to_markdown())
    lines.append("")

    # 的中率
    lines.append("## 2. 的中率比較")
    lines.append("")
    rows = []
    for cfg in CONFIGS:
        bt, _ = load_one(cfg["suffix"])
        d = bt.set_index("bet_type")["hit_rate"].to_dict()
        rows.append({"config": cfg["label"], **d})
    df = pd.DataFrame(rows).set_index("config")
    lines.append(df.round(4).to_markdown())
    lines.append("")

    # threshold ROI
    for thresh in [5, 10, 25]:
        lines.append(f"## 3.{thresh}: confidence 上位 {thresh}% 購入時 ROI")
        lines.append("")
        rows = []
        for cfg in CONFIGS:
            _, th = load_one(cfg["suffix"])
            sub = th[th["threshold_pct"] == thresh]
            d = sub.set_index("bet_type")["roi"].to_dict()
            rows.append({"config": cfg["label"], **d})
        df = pd.DataFrame(rows).set_index("config")
        lines.append(df.round(4).to_markdown())
        lines.append("")

    # threshold 差分
    lines.append("## 4. 上位 X% 購入時 ROI 差分 (with_odds_w - with_odds)")
    lines.append("")
    rows = []
    for thresh in [5, 10, 25]:
        _, th_base = load_one("_with_odds")
        _, th_w = load_one("_with_odds_w")
        base = th_base[th_base["threshold_pct"] == thresh].set_index("bet_type")["roi"]
        w = th_w[th_w["threshold_pct"] == thresh].set_index("bet_type")["roi"]
        diff = (w - base).round(4)
        d = diff.to_dict()
        rows.append({"threshold_pct": thresh, **d})
    df = pd.DataFrame(rows).set_index("threshold_pct")
    lines.append(df.to_markdown())
    lines.append("")

    lines.append("## 補足")
    lines.append("- delta_roi > 0: W 追加で ROI 改善")
    lines.append("- 期待効果: W は 3 着内予測 (y_top3) に直接効くはず")
    lines.append("- ただし市場の蒸留度がさらに上がる懸念もある")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n比較レポート: {out}")


def main() -> None:
    for cfg in CONFIGS:
        run_one(cfg["args"], cfg["label"])
    generate_report()
    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
