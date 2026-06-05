"""約定実現性 (settlement realizability) 検証 — Phase A 中核分析

問い: 発走 N 分前 snapshot odds は「約定できる価格」か?
競輪はパリミュチュエルで、払戻は発走時の最終プールで確定する。5 分前 snapshot
odds で計算した EV は、実際には約定できない価格上の計算である可能性がある。

本スクリプトは当選組について:
  - race_odds_prerace.csv の offset 別 snapshot odds
  - payouts.csv の確定実現オッズ (= payout / 100)
を (race_date, place_code, race_no, bet_type, kumi_ban) で join し、
  執行ウェッジ wedge = realized_odds / snapshot_odds - 1
を券種別・offset 別に集計する。

注意:
- 実現オッズは「当選組」にしか存在しない (払戻があるのは的中組のみ)。
  したがって本分析は「当たった切符の執行ウェッジ」を測る。これは回収が発生する
  母集団そのものであり、逆選択 (favorite ほど realized < snapshot か) の検出に適する。
- ワイド (W) は払戻がレンジ。snapshot 側も odds(=min) と max_odds を持つため、
  realized が [odds, max_odds] に収まるか + 中点との wedge を別途報告する。

合格基準 (Phase A):
  5 分前 → 確定の中央値乖離が必要エッジ幅 (数 %) より小さく、系統的逆選択が無いこと。
  満たさなければ「5 分前 odds 基準の本番 EV 設計は机上計算」と確定する。

使い方:
  python analyze_settlement_wedge.py
  python analyze_settlement_wedge.py --start 2026-05-16 --end 2026-06-02
  python analyze_settlement_wedge.py --bet-types ST2,SH2,W
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from src.config import DATA_DIR

# 順序型 / 無順序型 (kumi_ban のセパレータ差。join 整合の確認用)
ORDERED = {"ST2", "WT2", "RT3"}

# join キー
KEYS = ["race_date", "place_code", "race_no", "bet_type", "kumi_ban"]


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_prerace(start: str | None, end: str | None) -> pd.DataFrame:
    """race_odds_prerace.csv を読み、(key, offset) 単位で最新 snapshot に dedup。"""
    path = DATA_DIR / "race_odds_prerace.csv"
    df = pd.read_csv(
        path,
        usecols=[
            "race_date", "place_code", "race_no", "bet_type", "kumi_ban",
            "odds", "max_odds", "target_offset_min", "snapshot_dt",
        ],
        dtype={
            "race_date": str, "place_code": str, "race_no": str,
            "bet_type": str, "kumi_ban": str,
        },
        low_memory=False,
    )
    if start:
        df = df[df["race_date"] >= start]
    if end:
        df = df[df["race_date"] <= end]
    df = df[pd.to_numeric(df["odds"], errors="coerce") > 0]
    # (key, offset) 単位で snapshot_dt 最新だけ残す (再実行による重複対策)
    df = df.sort_values("snapshot_dt")
    df = df.drop_duplicates(
        subset=KEYS + ["target_offset_min"], keep="last"
    )
    df["odds"] = pd.to_numeric(df["odds"], errors="coerce")
    df["max_odds"] = pd.to_numeric(df["max_odds"], errors="coerce")
    df["target_offset_min"] = pd.to_numeric(
        df["target_offset_min"], errors="coerce"
    )
    return df


def load_payouts(start: str | None, end: str | None) -> pd.DataFrame:
    """payouts.csv → 当選組の実現オッズ realized_odds = payout / 100。"""
    path = DATA_DIR / "payouts.csv"
    df = pd.read_csv(
        path,
        usecols=[
            "race_date", "place_code", "race_no", "bet_type",
            "kumi_ban", "payout",
        ],
        dtype={
            "race_date": str, "place_code": str, "race_no": str,
            "bet_type": str, "kumi_ban": str,
        },
        low_memory=False,
    )
    if start:
        df = df[df["race_date"] >= start]
    if end:
        df = df[df["race_date"] <= end]
    df["payout"] = pd.to_numeric(df["payout"], errors="coerce")
    df = df[df["payout"] > 0]
    df["realized_odds"] = df["payout"] / 100.0
    # 同一 (key) に複数払戻 (W は 3 組) → kumi_ban が異なるので key で一意のはず
    df = df.drop_duplicates(subset=KEYS, keep="last")
    return df


def summarize(merged: pd.DataFrame, offset: float) -> dict | None:
    """1 つの (bet_type, offset) スライスの統計を返す。"""
    sub = merged[merged["target_offset_min"] == offset]
    if len(sub) == 0:
        return None
    w = sub["wedge"].dropna()
    if len(w) == 0:
        return None
    return {
        "offset": offset,
        "n": int(len(w)),
        "median_wedge_pct": round(float(w.median()) * 100, 2),
        "mean_wedge_pct": round(float(w.mean()) * 100, 2),
        "p25_pct": round(float(w.quantile(0.25)) * 100, 2),
        "p75_pct": round(float(w.quantile(0.75)) * 100, 2),
        "frac_abs_gt10_pct": round(float((w.abs() > 0.10).mean()) * 100, 1),
        "frac_abs_gt20_pct": round(float((w.abs() > 0.20).mean()) * 100, 1),
        "frac_adverse_pct": round(float((w < 0).mean()) * 100, 1),  # realized < snapshot
    }


def main() -> None:
    _stdout_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="開始日 YYYY-MM-DD (含む)")
    ap.add_argument("--end", default=None, help="終了日 YYYY-MM-DD (含む)")
    ap.add_argument("--bet-types", default="SH2,ST2,RH3,RT3,W",
                    help="対象券種 CSV (default 'SH2,ST2,RH3,RT3,W')")
    args = ap.parse_args()

    bet_types = [b.strip() for b in args.bet_types.split(",") if b.strip()]

    print("=" * 70)
    print("約定実現性 (settlement wedge) 分析")
    print(f"  期間: {args.start or '(全)'} 〜 {args.end or '(全)'}")
    print(f"  券種: {bet_types}")
    print("=" * 70)

    pre = load_prerace(args.start, args.end)
    pay = load_payouts(args.start, args.end)

    pre = pre[pre["bet_type"].isin(bet_types)]
    pay = pay[pay["bet_type"].isin(bet_types)]

    print(f"\nprerace snapshot 行数 (dedup後): {len(pre):,}")
    print(f"payouts 当選組 行数: {len(pay):,}")
    print(f"prerace 日付範囲: {pre['race_date'].min()} 〜 {pre['race_date'].max()}")
    print(f"payouts  日付範囲: {pay['race_date'].min()} 〜 {pay['race_date'].max()}")
    offsets = sorted(pre["target_offset_min"].dropna().unique())
    print(f"取得済 offset (分前): {offsets}")

    # join: 当選組の snapshot odds を offset 別に取得
    merged = pre.merge(
        pay[KEYS + ["realized_odds"]], on=KEYS, how="inner"
    )
    if len(merged) == 0:
        print("\n[警告] join 結果 0 件。kumi_ban 形式 / 日付重複を確認せよ。")
        return

    # ワイドは odds がレンジ下限。wedge 計算は W とそれ以外で分ける。
    # 非 W: wedge = realized / snapshot_odds - 1
    # W   : snapshot 中点 (odds+max_odds)/2 を基準に wedge、レンジ内判定も別途
    is_w = merged["bet_type"] == "W"
    snap_basis = merged["odds"].astype(float).copy()
    mid = (merged["odds"] + merged["max_odds"]) / 2.0
    snap_basis[is_w] = mid[is_w].where(mid[is_w].notna(), merged["odds"][is_w])
    merged["snap_basis"] = snap_basis
    merged["wedge"] = merged["realized_odds"] / merged["snap_basis"] - 1.0

    print("\n" + "=" * 70)
    print("券種 × offset 別 執行ウェッジ (wedge = realized/snapshot - 1)")
    print("  median<0 = realized が snapshot を下回る = 当選側で不利 (逆選択)")
    print("=" * 70)
    header = (f"{'bet':<5}{'off':>5}{'n':>7}{'median%':>9}{'mean%':>8}"
              f"{'p25%':>8}{'p75%':>8}{'|w|>10%':>9}{'|w|>20%':>9}{'adv%':>7}")
    for bt in bet_types:
        bt_merged = merged[merged["bet_type"] == bt]
        if len(bt_merged) == 0:
            continue
        print(f"\n--- {bt} ---")
        print(header)
        for off in offsets:
            s = summarize(bt_merged, off)
            if s is None:
                continue
            print(f"{bt:<5}{s['offset']:>5}{s['n']:>7}{s['median_wedge_pct']:>9}"
                  f"{s['mean_wedge_pct']:>8}{s['p25_pct']:>8}{s['p75_pct']:>8}"
                  f"{s['frac_abs_gt10_pct']:>9}{s['frac_abs_gt20_pct']:>9}"
                  f"{s['frac_adverse_pct']:>7}")

    # 人気帯別の逆選択チェック (snapshot odds 帯ごとの median wedge)
    print("\n" + "=" * 70)
    print("snapshot odds 帯別 median wedge (本命=低オッズ で逆選択が強いか)")
    print("=" * 70)
    bands = [(1, 3), (3, 6), (6, 10), (10, 20), (20, 50), (50, 1e9)]
    # offset が最大 (最も早い = 既存 5 分前相当) のスライスで評価
    base_off = max(offsets) if offsets else None
    base = merged[merged["target_offset_min"] == base_off]
    print(f"(offset={base_off} 分前 snapshot ベース, n={len(base):,})")
    print(f"{'odds帯':>14}{'n':>7}{'median_wedge%':>15}{'adverse%':>10}")
    for lo, hi in bands:
        b = base[(base["snap_basis"] >= lo) & (base["snap_basis"] < hi)]
        w = b["wedge"].dropna()
        if len(w) == 0:
            continue
        label = f"{lo}-{hi if hi < 1e9 else '+'}"
        print(f"{label:>14}{len(w):>7}{round(float(w.median())*100,2):>15}"
              f"{round(float((w<0).mean())*100,1):>10}")

    # ワイドのレンジ内判定
    w_rows = merged[(merged["bet_type"] == "W") & merged["max_odds"].notna()]
    if len(w_rows) > 0:
        in_range = ((w_rows["realized_odds"] >= w_rows["odds"]) &
                    (w_rows["realized_odds"] <= w_rows["max_odds"]))
        print("\n" + "=" * 70)
        print("ワイド: 確定配当が snapshot レンジ [odds, max_odds] に収まる割合")
        print(f"  n={len(w_rows):,}, レンジ内={round(float(in_range.mean())*100,1)}%, "
              f"レンジ下抜け(不利)={round(float((w_rows['realized_odds']<w_rows['odds']).mean())*100,1)}%")
        print("=" * 70)

    # 総括判定
    print("\n" + "=" * 70)
    print("判定サマリ")
    print("=" * 70)
    if base_off is not None:
        overall = merged[merged["target_offset_min"] == base_off]["wedge"].dropna()
        med = float(overall.median()) * 100
        adv = float((overall < 0).mean()) * 100
        print(f"offset={base_off}分前 全券種: n={len(overall):,}, "
              f"median wedge={med:.2f}%, 不利(realized<snapshot)={adv:.1f}%")
        verdict = ("合格寄り (乖離小)" if abs(med) < 3 and abs(adv - 50) < 10
                   else "要警戒 (系統的乖離/逆選択の兆候)")
        print(f"暫定判定: {verdict}")
        print("※ サンプル小のため確定判定不可。母数蓄積 + 直前 snapshot で再評価せよ。")


if __name__ == "__main__":
    main()
