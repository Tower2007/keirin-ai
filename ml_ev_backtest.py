"""疑似 EV ベース選別バックテスト

考え方:
  1. confidence (モデルの自信スコア) でレースを N分位ビンに分ける
  2. 校正期間 (2025-01〜2025-08) でビン毎の平均払戻 (≒EV) を測定
  3. EV > 100 のビンだけ評価期間 (2025-09〜2026-04) で買う
  4. ROI を比較

これにより「過去の同じ自信レベルで儲かっていたゾーンだけ買う」戦略を
look-ahead 無しで検証できる。

事前オッズ無しでも疑似 EV ベース選別が可能。Phase 3 で本物のオッズが
取れた時、同じフレームワークで本物の EV 計算に置き換える。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

DATA = Path(__file__).parent / "data"
CALIBRATION_END = "2025-08-31"
N_BINS = 20  # 校正用ビン数 (5% 刻み)

# 券種ごとの: (label, ticket_col, conf_col, n_per_race)
BET_SPECS = [
    ("二車複", "SH2", "conf_SH2", 1),
    ("二車単", "ST2", "conf_ST2", 1),
    ("三連複", "RH3", "conf_RH3", 1),
    ("三連単", "RT3", "conf_RT3", 1),
]


def load_picks_and_payouts() -> tuple[pd.DataFrame, dict]:
    picks = pd.read_csv(DATA / "ml_picks.csv")
    payouts = pd.read_csv(DATA / "payouts.csv")
    payouts["key"] = (
        payouts["race_date"] + "|" + payouts["place_code"].astype(str) + "|"
        + payouts["race_no"].astype(str) + "|" + payouts["bet_type"] + "|"
        + payouts["kumi_ban"].astype(str)
    )
    pmap = payouts.groupby("key")["payout"].max().to_dict()
    return picks, pmap


def lookup_payout(pmap, rd, pc, rn, bt, kb) -> int:
    return pmap.get(f"{rd}|{pc}|{rn}|{bt}|{kb}", 0)


def annotate_payouts(picks: pd.DataFrame, pmap: dict) -> pd.DataFrame:
    """各券種について、その pick が当たったら払戻何円かを列追加。"""
    for label, bt_code, _, _ in BET_SPECS:
        col = f"payout_{bt_code}"
        picks[col] = picks.apply(
            lambda r: lookup_payout(pmap, r["race_date"], r["place_code"],
                                    r["race_no"], bt_code, r[bt_code]),
            axis=1,
        )
    # ワイドは 3組合せの合計払戻 (1 race = 300円コスト)
    picks["payout_W"] = picks.apply(
        lambda r: sum(
            lookup_payout(pmap, r["race_date"], r["place_code"], r["race_no"],
                          "W", r[w_col]) for w_col in ["W1", "W2", "W3"]
        ),
        axis=1,
    )
    return picks


def calibrate_and_evaluate(
    picks: pd.DataFrame, label: str, conf_col: str,
    payout_col: str, cost_per_race: int,
) -> dict:
    """1券種について: 校正→評価→ベットフィルタ後の ROI を返す。"""
    cal = picks[picks["race_date"] <= CALIBRATION_END].copy()
    ev = picks[picks["race_date"] > CALIBRATION_END].copy()

    # 校正期間で confidence を N分位ビンに
    bins = pd.qcut(cal[conf_col], q=N_BINS, labels=False, duplicates="drop")
    cal["bin"] = bins
    bin_edges = pd.qcut(cal[conf_col], q=N_BINS, retbins=True,
                        duplicates="drop")[1]

    # 各ビンの平均払戻 (= EV per ticket / cost-per-race)
    bin_stats = (
        cal.groupby("bin", observed=True)
        .agg(n=(payout_col, "size"),
             sum_payout=(payout_col, "sum"),
             hits=(payout_col, lambda s: (s > 0).sum()))
    )
    bin_stats["ev_per_ticket"] = bin_stats["sum_payout"] / bin_stats["n"]
    bin_stats["hit_rate"] = bin_stats["hits"] / bin_stats["n"]
    bin_stats["roi"] = bin_stats["ev_per_ticket"] / cost_per_race - 1

    # EV > cost のビン (= 期待値プラス)
    profitable_bins = bin_stats[bin_stats["ev_per_ticket"] > cost_per_race].index.tolist()

    # 評価期間で同じビン境界を使ってビンを割当
    ev_bin = pd.cut(ev[conf_col], bins=bin_edges,
                    labels=False, include_lowest=True)
    ev["bin"] = ev_bin
    # 校正で profitable と判定されたビンだけ買う
    selected = ev[ev["bin"].isin(profitable_bins)].copy()

    # ベースライン (校正で profitable でも全レース買う)
    n_eval = len(ev)
    n_select = len(selected)
    eval_payout = selected[payout_col].sum()
    eval_cost = n_select * cost_per_race
    eval_hits = (selected[payout_col] > 0).sum()
    eval_roi = (eval_payout / eval_cost - 1) if eval_cost else None

    # 比較用: 評価期間で全レース買った場合
    all_payout = ev[payout_col].sum()
    all_cost = n_eval * cost_per_race
    all_roi = all_payout / all_cost - 1 if all_cost else None

    return {
        "bet_type": label,
        "n_cal": len(cal),
        "n_eval": n_eval,
        "n_profitable_bins": len(profitable_bins),
        "n_selected": n_select,
        "select_rate": round(n_select / n_eval, 4) if n_eval else None,
        "all_roi": round(all_roi, 4) if all_roi is not None else None,
        "selected_roi": round(eval_roi, 4) if eval_roi is not None else None,
        "selected_hits": int(eval_hits),
        "selected_payout": int(eval_payout),
        "selected_cost": eval_cost,
        "_bin_stats": bin_stats,
        "_profitable_bins": profitable_bins,
    }


def main():
    print("=" * 70)
    print("EV-based Selection Backtest")
    print(f"  Calibration: 2025-01-01 〜 {CALIBRATION_END}")
    print(f"  Evaluation:  2025-09-01 〜 (test 残期間)")
    print(f"  N bins: {N_BINS}")
    print("=" * 70)

    picks, pmap = load_picks_and_payouts()
    print(f"\nPicks loaded: {len(picks):,} races")
    print("Annotating actual payouts per pick...")
    picks = annotate_payouts(picks, pmap)
    print("Done.")

    print()
    summary_rows = []
    for label, bt_code, conf_col, n_per in BET_SPECS:
        payout_col = f"payout_{bt_code}"
        cost = 100 * n_per
        result = calibrate_and_evaluate(picks, label, conf_col, payout_col, cost)
        summary_rows.append({k: v for k, v in result.items() if not k.startswith("_")})

        print(f"--- {label} ---")
        bs = result["_bin_stats"]
        print(f"  Calibration bins (head/tail by ROI):")
        sorted_bins = bs.sort_values("roi", ascending=False)
        print("  TOP 3 ROI bins:")
        print(sorted_bins.head(3).round(2).to_string())
        print("  BOTTOM 3 ROI bins:")
        print(sorted_bins.tail(3).round(2).to_string())
        print(f"  Profitable bins (EV > 100): {result['n_profitable_bins']}/{N_BINS}")
        print()

    # ワイド (cost 300/race)
    label = "ワイド"
    payout_col = "payout_W"
    result = calibrate_and_evaluate(picks, label, "conf_W", payout_col, 300)
    summary_rows.append({k: v for k, v in result.items() if not k.startswith("_")})

    bs = result["_bin_stats"]
    print(f"--- {label} ---")
    sorted_bins = bs.sort_values("roi", ascending=False)
    print("  TOP 3 ROI bins:")
    print(sorted_bins.head(3).round(2).to_string())
    print("  BOTTOM 3 ROI bins:")
    print(sorted_bins.tail(3).round(2).to_string())
    print(f"  Profitable bins (EV > 300): {result['n_profitable_bins']}/{N_BINS}")
    print()

    # サマリ表
    summary = pd.DataFrame(summary_rows)
    print("=" * 70)
    print("=== Summary ===")
    print("=" * 70)
    print(summary[["bet_type", "n_eval", "n_selected", "select_rate",
                   "all_roi", "selected_roi", "selected_hits"]].to_string(index=False))

    out = DATA / "ml_ev_backtest_result.csv"
    summary.to_csv(out, index=False)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
