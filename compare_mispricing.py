"""Mispricing 特徴量の効果検証 (Phase 6 N3)

with_odds (基準) と with_odds_mp (mispricing 追加) を比較し、
市場 vs 戦績の食い違い特徴量がモデルにどう効くか可視化。

⚠️ with_odds_mp も pre-start odds ではないので upper-bound 値。

出力: D:\\keirin-ai-data\\mispricing_compare_report.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR as DATA

CONFIGS = [
    {"label": "with_odds",     "suffix": "_with_odds",     "args": ["--use-odds"]},
    {"label": "with_odds_mp",  "suffix": "_with_odds_mp",  "args": ["--use-odds", "--use-mispricing"]},
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
    out = DATA / "mispricing_compare_report.md"
    lines: list[str] = []
    lines.append("# Mispricing 特徴量 効果検証レポート (Phase 6 N3)")
    lines.append("")
    lines.append("⚠️ どちらも race_odds.csv (post-start = リーケージあり) ベースの")
    lines.append("upper-bound 性能。本番では再現不能。")
    lines.append("")

    # 全レース ROI 比較
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
    lines.append("### 差分: with_odds_mp - with_odds (mispricing 追加効果)")
    lines.append("")
    base_bt, _ = load_one("_with_odds")
    mp_bt, _ = load_one("_with_odds_mp")
    diff = (mp_bt.set_index("bet_type")["roi"] -
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
    lines.append("## 4. 上位 X% 購入時 ROI 差分 (with_odds_mp - with_odds)")
    lines.append("")
    rows = []
    for thresh in [5, 10, 25]:
        _, th_base = load_one("_with_odds")
        _, th_mp = load_one("_with_odds_mp")
        base = th_base[th_base["threshold_pct"] == thresh].set_index("bet_type")["roi"]
        mp = th_mp[th_mp["threshold_pct"] == thresh].set_index("bet_type")["roi"]
        diff = (mp - base).round(4)
        d = diff.to_dict()
        rows.append({"threshold_pct": thresh, **d})
    df = pd.DataFrame(rows).set_index("threshold_pct")
    lines.append(df.to_markdown())
    lines.append("")

    lines.append("## 補足")
    lines.append("- delta_roi > 0: mispricing 追加で ROI 改善")
    lines.append("- 公営競技控除: ROI -25% 程度が損益分岐の目安")
    lines.append("- 期待される効果: 市場が外す穴目を検出して edge を増やす")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n比較レポート: {out}")


def main() -> None:
    for cfg in CONFIGS:
        run_one(cfg["args"], cfg["label"])
    generate_report()
    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
