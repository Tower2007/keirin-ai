"""ml_baseline を odds なし/あり 両方走らせて比較レポートを生成

Phase 6 6a-3: 3-way 比較 (うち 2-way) の自動化。
  - run 1: --use-odds 無し (no_odds, 基準線)
  - run 2: --use-odds 有り (with_final_odds, upper-bound)

結果は DATA_DIR/odds_comparison_report.md に出力。
両 run の AUC / MAE / ROI / threshold ROI を並べて差分を表示。

⚠️ with_final_odds の AUC/ROI は運用リーケージ前提の上限性能。
本番期待値ではなく、市場込み上限性能の参考値として扱うこと。

使い方:
  python compare_odds_baseline.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR as DATA


def run_baseline(use_odds: bool) -> None:
    """ml_baseline.py を走らせる。"""
    cmd = [sys.executable, "ml_baseline.py"]
    if use_odds:
        cmd.append("--use-odds")
    label = "with_odds" if use_odds else "no_odds"
    print(f"\n{'=' * 70}")
    print(f">>> Running ml_baseline.py  ({label})")
    print("=" * 70)
    subprocess.check_call(cmd)


def load_results(suffix: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ml_baseline_v2_result*.csv と ml_threshold_result*.csv を読む。"""
    bt = pd.read_csv(DATA / f"ml_baseline_v2_result{suffix}.csv")
    th = pd.read_csv(DATA / f"ml_threshold_result{suffix}.csv")
    return bt, th


def generate_report() -> None:
    no_bt, no_th = load_results("")
    yes_bt, yes_th = load_results("_with_odds")

    out = DATA / "odds_comparison_report.md"
    lines: list[str] = []
    lines.append("# ML Baseline オッズあり/なし 比較レポート")
    lines.append("")
    lines.append("⚠️ **重要**: race_odds.csv の `official_dt` は発走後 0〜5 分のため、")
    lines.append("with_odds の AUC/ROI は本番では再現不能な上限性能。pre-start odds")
    lines.append("(`race_odds_prerace.csv`) 蓄積後に honest backtest を別途実施する。")
    lines.append("")

    lines.append("## 1. 全レース購入 (1ticket/race) ROI 比較")
    lines.append("")
    merged = no_bt[["bet_type", "roi", "hit_rate"]].rename(
        columns={"roi": "roi_no_odds", "hit_rate": "hit_no_odds"}
    ).merge(
        yes_bt[["bet_type", "roi", "hit_rate"]].rename(
            columns={"roi": "roi_with_odds", "hit_rate": "hit_with_odds"}
        ),
        on="bet_type",
    )
    merged["roi_delta"] = (merged["roi_with_odds"] - merged["roi_no_odds"]).round(4)
    merged["hit_delta"] = (merged["hit_with_odds"] - merged["hit_no_odds"]).round(4)
    lines.append(merged.to_markdown(index=False))
    lines.append("")

    lines.append("## 2. confidence 上位 X% 購入 ROI 比較")
    lines.append("")
    no_pv = no_th.pivot(index="bet_type", columns="threshold_pct", values="roi").round(4)
    yes_pv = yes_th.pivot(index="bet_type", columns="threshold_pct",
                          values="roi").round(4)
    no_pv.columns = [f"top{c}%_no_odds" for c in no_pv.columns]
    yes_pv.columns = [f"top{c}%_with_odds" for c in yes_pv.columns]

    # 並べて表示
    combined = pd.concat([no_pv, yes_pv], axis=1).sort_index(axis=1)
    lines.append(combined.to_markdown())
    lines.append("")

    # ROI 差分
    lines.append("### 上位 X% ROI 差分 (with_odds - no_odds)")
    lines.append("")
    no_pv2 = no_th.pivot(index="bet_type", columns="threshold_pct", values="roi")
    yes_pv2 = yes_th.pivot(index="bet_type", columns="threshold_pct", values="roi")
    diff = (yes_pv2 - no_pv2).round(4)
    diff.columns = [f"top{c}%_delta" for c in diff.columns]
    lines.append(diff.to_markdown())
    lines.append("")

    lines.append("## 補足")
    lines.append("- ROI: -1.0 = 全損, 0 = 損益±0, +0.5 = +50%")
    lines.append("- 公営競技控除: ROI -25% 程度が損益分岐の目安")
    lines.append("- with_odds が劇的に良い場合、リーケージの強さを示唆する")
    lines.append("  (本番では再現不能だが、market wisdom の上限値として参考になる)")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n比較レポート: {out}")


def main() -> None:
    odds_path = DATA / "odds_features.csv"
    if not odds_path.exists():
        sys.exit(f"先に build_odds_features.py を実行してください: {odds_path} がありません")

    run_baseline(use_odds=False)
    run_baseline(use_odds=True)
    generate_report()
    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
