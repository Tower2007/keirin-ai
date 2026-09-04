r"""Phase A (約定実現性) + Phase C (二重EV判定) 統合分析 (v0.1.0)
-----------------------------------------------------------------------
Phase A: 発走 N 分前 snapshot オッズが確定オッズ (= payout/100) とどれだけ
         乖離するかを offset (5/3/2/1/0.5分前) × 券種別に定量化し、
         「最遅約定可能 snapshot」を確定する。
Phase B: ev_prerace_merged.csv (p_combo: Harville 組合せ確率) は構築済み。
Phase C: p_combo × 各offsetオッズ で EV を再計算し、EV>=閾値 で買った場合の
         実現 ROI (確定 payout ベース) を計算。日次ブートストラップで CI95、
         BH-FDR 補正付きで「本番昇格に値するセルがあるか」を判定する。

使い方: python phase_ac_analysis.py
出力:   reports/phase_ac_YYYY-MM-DD.md
読み取り専用 (D:\keirin-ai-data の CSV を読むだけ)。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.odds_prerace import read_prerace  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", r"D:\keirin-ai-data"))
REPORT_DIR = ROOT / "reports"
KEY = ["race_date", "place_code", "race_no", "bet_type", "kumi_ban"]
BET = 100
N_BOOT = 2000
RNG = np.random.default_rng(42)


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    snap = read_prerace(dtype={"kumi_ban": str}, data_dir=DATA)
    pay = pd.read_csv(DATA / "payouts.csv", dtype={"kumi_ban": str}, low_memory=False)
    ev = pd.read_csv(DATA / "ev_prerace_merged.csv", dtype={"kumi_ban": str})
    # snapshot がある期間の payouts だけに絞る
    pay = pay[pay["race_date"] >= snap["race_date"].min()]
    pay = pay[["race_date", "place_code", "race_no", "bet_type", "kumi_ban", "payout"]]
    pay["final_odds"] = pay["payout"] / 100.0
    return snap, pay, ev


def phase_a(snap: pd.DataFrame, pay: pd.DataFrame) -> pd.DataFrame:
    """当選組の snapshot オッズ vs 確定オッズの乖離を offset×券種別に集計"""
    m = snap.merge(pay, on=KEY, how="inner")  # 当選組のみ残る
    m = m[(m["odds"] > 0) & (m["final_odds"] > 0)]
    m["drift_pct"] = (m["final_odds"] / m["odds"] - 1) * 100

    def _agg(g: pd.DataFrame) -> pd.Series:
        return pd.Series({
            "n": len(g),
            "drift_med": round(g["drift_pct"].median(), 1),
            "drift_p25": round(g["drift_pct"].quantile(0.25), 1),
            "drift_p75": round(g["drift_pct"].quantile(0.75), 1),
            "悪化率(下振れ>5%)": round((g["drift_pct"] < -5).mean() * 100, 1),
        })

    return (m.groupby(["target_offset_min", "bet_type"])
             .apply(_agg, include_groups=False)
             .reset_index()
             .sort_values(["bet_type", "target_offset_min"], ascending=[True, False]))


def phase_c(snap: pd.DataFrame, pay: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    """offset別オッズ×p_combo→EV選別→実現ROI+ブートストラップCI"""
    probs = ev[KEY + ["p_combo"]].drop_duplicates(KEY)
    odds_o = snap[KEY + ["target_offset_min", "odds"]]
    base = odds_o.merge(probs, on=KEY, how="inner")
    base = base[base["odds"] > 0]
    base["ev_off"] = base["p_combo"] * base["odds"]
    base = base.merge(pay[KEY + ["payout"]], on=KEY, how="left")
    base["payout"] = base["payout"].fillna(0)

    rows = []
    for (off, bt), g in base.groupby(["target_offset_min", "bet_type"]):
        for thr in (1.0, 1.2, 1.5, 2.0):
            picks = g[g["ev_off"] >= thr]
            n = len(picks)
            if n < 30:
                continue
            stake = n * BET
            ret = picks["payout"].sum()
            roi = ret / stake * 100
            # 日次ブートストラップ
            daily = picks.groupby("race_date").agg(
                stake=("payout", "size"), ret=("payout", "sum"))
            daily["stake"] *= BET
            days = daily.index.to_numpy()
            if len(days) < 5:
                continue
            boots = []
            for _ in range(N_BOOT):
                pick_days = RNG.choice(days, size=len(days), replace=True)
                d = daily.loc[pick_days]
                boots.append(d["ret"].sum() / d["stake"].sum() * 100)
            boots = np.array(boots)
            rows.append({
                "offset_min": off, "bet_type": bt, "ev_thr": thr,
                "n_bets": n, "days": len(days),
                "roi": round(roi, 1),
                "ci_low": round(np.percentile(boots, 2.5), 1),
                "ci_high": round(np.percentile(boots, 97.5), 1),
                # p値: ROI<=100 となるブートストラップ比率 (片側)
                "p_le100": round(float((boots <= 100).mean()), 4),
            })
    res = pd.DataFrame(rows)
    if res.empty:
        return res
    # BH-FDR 補正 (q=0.10)
    res = res.sort_values("p_le100").reset_index(drop=True)
    mtests = len(res)
    res["bh_crit"] = [(i + 1) / mtests * 0.10 for i in range(mtests)]
    res["fdr_pass"] = res["p_le100"] <= res["bh_crit"]
    return res.sort_values(["bet_type", "offset_min", "ev_thr"])


def main() -> int:
    snap, pay, ev = load()
    period = f"{snap['race_date'].min()}〜{snap['race_date'].max()}"
    print(f"snapshot rows={len(snap):,} ({period}) / ev rows={len(ev):,}", flush=True)

    pa = phase_a(snap, pay)
    pc = phase_c(snap, pay, ev)

    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"phase_ac_{dt.date.today().isoformat()}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Phase A/C 統合分析 ({period})\n\n")
        f.write("## Phase A: 約定実現性 (当選組の snapshot→確定オッズ乖離 %)\n\n")
        f.write("drift_med < 0 = snapshotオッズより確定が低い (当選側に票が集まる通常パターン)。\n\n")
        f.write(pa.to_markdown(index=False) + "\n\n")
        f.write("## Phase C: 二重EV判定 (offset別オッズでEV選別 → 実現ROI)\n\n")
        f.write("fdr_pass=True = BH-FDR(q=0.10) を通過 = 多重検定補正後も ROI>100 が有意。\n\n")
        if pc.empty:
            f.write("(該当セルなし)\n")
        else:
            f.write(pc.to_markdown(index=False) + "\n\n")
            n_pass = int(pc["fdr_pass"].sum())
            f.write(f"**FDR通過セル: {n_pass} / {len(pc)}**\n")
    print(f"レポート: {out}", flush=True)
    print("\n--- Phase A (乖離中央値) ---", flush=True)
    print(pa.to_string(index=False), flush=True)
    if not pc.empty:
        print("\n--- Phase C (FDR通過セルのみ) ---", flush=True)
        passed = pc[pc["fdr_pass"]]
        print(passed.to_string(index=False) if not passed.empty else "(なし)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
