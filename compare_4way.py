"""4-way 比較: production_no_odds / +line / final_odds_upper_bound / +odds+line

⚠️ 用途分類 (2026-05-15 Phase 6 pivot 後):
  PRODUCTION 候補: base (= production_no_odds), with_line (= base_line_shadow)
  UPPER BOUND 研究: with_odds (= final_odds_upper_bound),
                   with_odds_line (= final_odds_line_upper_bound)

  upper bound 系は本番 EV 判定に使わない。市場の蒸留度合いを監査する目的。
  本番 EV は race_odds_prerace.csv (live snapshot) を別途 lookup する。

Phase 6 6a-2 + 6a-3 統合検証。4 つのモデルを走らせ、AUC/ROI を比較。

出力: D:\\keirin-ai-data\\compare_4way_report.md
走行時間: 各 run 約 3〜4 分、計 12-20 分
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR as DATA


CONFIGS = [
    {"label": "base",           "suffix": "",                 "args": []},
    {"label": "with_line",      "suffix": "_with_line",       "args": ["--use-line"]},
    {"label": "with_odds",      "suffix": "_with_odds",       "args": ["--use-odds"]},
    {"label": "with_odds_line", "suffix": "_with_odds_line",  "args": ["--use-odds", "--use-line"]},
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
    out = DATA / "compare_4way_report.md"
    lines: list[str] = []
    lines.append("# 4-way 比較レポート: base / +line / +odds / +odds+line")
    lines.append("")
    lines.append("⚠️ **重要**: +odds 入りは race_odds.csv (post-start = リーケージあり)")
    lines.append("を使う upper-bound 性能。本番では再現不能。pre-start snapshot 蓄積後に")
    lines.append("honest backtest を別途実施する。")
    lines.append("")

    # ROI 全レース購入比較
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

    # Hit rate 全レース
    lines.append("## 2. 全レース 的中率比較")
    lines.append("")
    rows = []
    for cfg in CONFIGS:
        bt, _ = load_one(cfg["suffix"])
        d = bt.set_index("bet_type")["hit_rate"].to_dict()
        rows.append({"config": cfg["label"], **d})
    df = pd.DataFrame(rows).set_index("config")
    lines.append(df.round(4).to_markdown())
    lines.append("")

    # Threshold ROI (top5%, top10%) per config
    for thresh in [5, 10, 25]:
        lines.append(f"## 3.{thresh}: confidence 上位 {thresh}% のみ購入時 ROI")
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

    # 差分: with_odds_line - base
    lines.append("## 4. 差分: with_odds_line - base (全レース ROI)")
    lines.append("")
    base_bt, _ = load_one("")
    full_bt, _ = load_one("_with_odds_line")
    diff = (full_bt.set_index("bet_type")["roi"] -
            base_bt.set_index("bet_type")["roi"]).round(4)
    lines.append(diff.to_frame("delta_roi").to_markdown())
    lines.append("")

    # 差分: with_line - base (line だけの効果)
    lines.append("## 5. 差分: with_line - base (line 特徴量の純効果)")
    lines.append("")
    line_bt, _ = load_one("_with_line")
    diff = (line_bt.set_index("bet_type")["roi"] -
            base_bt.set_index("bet_type")["roi"]).round(4)
    lines.append(diff.to_frame("delta_roi").to_markdown())
    lines.append("")

    # 差分: with_odds_line - with_odds (line を追加で加えた効果)
    lines.append("## 6. 差分: with_odds_line - with_odds (line の追加効果)")
    lines.append("")
    odds_bt, _ = load_one("_with_odds")
    diff = (full_bt.set_index("bet_type")["roi"] -
            odds_bt.set_index("bet_type")["roi"]).round(4)
    lines.append(diff.to_frame("delta_roi").to_markdown())
    lines.append("")

    lines.append("## 補足")
    lines.append("- ROI: -1.0 = 全損, 0 = 損益±0, +0.5 = +50%")
    lines.append("- 公営競技控除: ROI -25% 程度が損益分岐の目安")
    lines.append("- line 特徴量は probabilistic / weak feature。完全ライン復元ではない")
    lines.append("- odds は upper-bound (リーケージ込み) なので本番期待値と異なる")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n比較レポート: {out}")


def main() -> None:
    # 必要なファイル存在チェック
    for f in ["odds_features.csv", "line_features.csv"]:
        p = DATA / f
        if not p.exists():
            sys.exit(f"{f} がありません。先に build スクリプトを実行してください。")

    # 既存結果を使うか判定 (既にあれば skip オプション、ここでは全部走らせる)
    for cfg in CONFIGS:
        run_one(cfg["args"], cfg["label"])

    generate_report()
    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
