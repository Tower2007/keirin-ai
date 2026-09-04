"""本番 EV 配管 (Phase B) — per-car 確率 → 組合せ確率 → prerace odds 結合

目的 (Phase B = 配管検証):
  production_no_odds モデルの per-car 予測 (ml_pred_probs.csv) を
  per-race 正規化 → Harville モデルで組合せ確率に変換し、発走前 live odds
  (race_odds_prerace.csv) と結合して EV を計算する「配管」を通す。

  ⚠️ このフェーズでは券種優劣・黒字赤字は一切結論づけない。
  目的は配管が破綻なく通ること + 健全性 2 指標の確認のみ:
    (1) dedup 後 overround ≈ 1.34 (= 1/(1-控除率)) を再現するか
    (2) EV ゲート無し全買い (uniform) ROI が ≈ -控除率 に収束するか
        (効率市場では E[当選払戻] = 100 * n_combos / overround となり
         uniform 全買い ROI = 1/overround - 1 ≈ -控除率 に一致するはず)

組合せ確率 (Harville モデル, p_win を per-race 正規化した q_i ベース):
  二車単 ST2 (順序 i→j): P = q_i * q_j / (1 - q_i)
  二車複 SH2 (無順序{i,j}): P = q_i*q_j/(1-q_i) + q_j*q_i/(1-q_j)

健全性チェック (overround / all-buy ROI) は **モデル非依存** (odds + payouts のみ)
なので ml_pred_probs.csv が無くても実行できる。

使い方:
  python ev_prerace_pipeline.py
  python ev_prerace_pipeline.py --start 2026-05-16 --end 2026-06-02
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from src.config import DATA_DIR as DATA
from src.odds_prerace import load_prerace_dedup as _load_prerace_dedup_partitioned

KEYS = ["race_date", "place_code", "race_no"]
JOIN_KEYS = KEYS + ["bet_type", "kumi_ban"]
# 控除率の目安 (公営競技)
TAKEOUT_REF = 0.25


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ─── オッズ / 払戻ロード (モデル非依存) ──────────────────────────────────

def load_prerace_dedup(start: str | None, end: str | None,
                       use_cache: bool = True) -> pd.DataFrame:
    """race_odds_prerace を (race,bet,kumi) 単位で最新 snapshot に dedup。

    同一 (race,bet,kumi) に 5/3/2/1/0.5 分前の複数 snapshot があるので、
    dedup を怠ると overround が offset 個数倍に膨らむ (報告の "3 倍バグ")。
    最遅 offset (= 最も締切に近い = 約定価格に近い) を残すため snapshot_dt 最新を採用。

    2026-09-04: 読み込みは月次パーティション (race_odds_prerace_YYYY-MM.csv +
    残っていれば旧単一ファイル) に変更。確定月は dedup 結果を
    DATA_DIR/cache/odds_prerace_dedup_YYYY-MM.parquet にキャッシュし、当月だけ
    毎回 dedup する。意味論は不変 (src/odds_prerace.dedup_latest、tests で固定)。
    """
    df, n_raw = _load_prerace_dedup_partitioned(
        start, end, data_dir=DATA, use_cache=use_cache, verbose=True)
    print(f"  prerace odds: {n_raw:,} → {len(df):,} 行 (dedup後)")
    return df


def load_payouts(start: str | None, end: str | None) -> pd.DataFrame:
    df = pd.read_csv(
        DATA / "payouts.csv",
        usecols=["race_date", "place_code", "race_no", "bet_type",
                 "kumi_ban", "payout"],
        dtype={"race_date": str, "place_code": str, "race_no": str,
               "bet_type": str, "kumi_ban": str},
        low_memory=False,
    )
    if start:
        df = df[df["race_date"] >= start]
    if end:
        df = df[df["race_date"] <= end]
    df["payout"] = pd.to_numeric(df["payout"], errors="coerce")
    df = df[df["payout"] > 0]
    df = df.drop_duplicates(subset=JOIN_KEYS, keep="last")
    df["realized_odds"] = df["payout"] / 100.0
    return df


# ─── 健全性チェック (モデル非依存) ─────────────────────────────────────

def sanity_overround(odds: pd.DataFrame) -> pd.DataFrame:
    """(race,bet) 単位の overround = Σ(1/odds) を券種別に集計。"""
    odds = odds.copy()
    odds["imp"] = 1.0 / odds["odds"]
    grp = odds.groupby(KEYS + ["bet_type"])["imp"].sum().reset_index(name="overround")
    rows = []
    for bt, g in grp.groupby("bet_type"):
        rows.append({
            "bet_type": bt,
            "n_races": int(len(g)),
            "overround_median": round(float(g["overround"].median()), 4),
            "overround_mean": round(float(g["overround"].mean()), 4),
            "implied_takeout_median%": round(
                (1 - 1 / float(g["overround"].median())) * 100, 1),
        })
    return pd.DataFrame(rows)


def sanity_allbuy_roi(odds: pd.DataFrame, pay: pd.DataFrame) -> pd.DataFrame:
    """EV ゲート無し全買い ROI を券種別に集計 (モデル非依存)。

    2 通りの賭け方を出す:
    - uniform : 各組に 100 円均等。cost=100*n_combos, return=当選 payout。
        ⚠️ 単勝系 exotics では本命が勝ちやすく当選払戻が組数平均を下回るため
        -控除率 より低く (-40% 前後) 出るのが正常。清算検証には pool 加重を使う。
    - pool    : 各組に market implied prob (1/odds を正規化) 比例で賭ける。
        効率市場なら当選者に関わらず毎レース 1/overround を返すので、清算ロジックが
        正しければ全券種で ROI ≈ 1/overround - 1 ≈ -控除率 に収束する (clearing 健全性)。
        実測が -25% より僅かに低い分は snapshot→確定の執行ウェッジ (Phase A)。
    """
    n_combos = odds.groupby(KEYS + ["bet_type"]).size().reset_index(name="n_combos")
    win = pay.groupby(KEYS + ["bet_type"])["payout"].sum().reset_index(name="win_payout")

    # pool 加重用: per-(race,bet) overround と各組 stake
    o = odds.copy()
    o["imp"] = 1.0 / o["odds"]
    ov = o.groupby(KEYS + ["bet_type"])["imp"].sum().reset_index(name="ov")
    o = o.merge(ov, on=KEYS + ["bet_type"])
    o["stake"] = o["imp"] / o["ov"]  # per-(race,bet) で stake 合計=1
    win_combo = pay[KEYS + ["bet_type", "kumi_ban", "realized_odds"]]
    o_win = o.merge(win_combo, on=JOIN_KEYS, how="inner")  # 当選組に賭けた stake
    o_win["ret"] = o_win["stake"] * o_win["realized_odds"]
    pool_ret = o_win.groupby("bet_type")["ret"].sum()
    pool_cost = o.groupby("bet_type")["stake"].sum()  # = n_races (各 race stake=1)

    m = n_combos.merge(win, on=KEYS + ["bet_type"], how="left")
    m["win_payout"] = m["win_payout"].fillna(0)
    rows = []
    for bt, g in m.groupby("bet_type"):
        cost = float((g["n_combos"] * 100).sum())
        ret = float(g["win_payout"].sum())
        pc = float(pool_cost.get(bt, 0))
        pr = float(pool_ret.get(bt, 0))
        rows.append({
            "bet_type": bt,
            "n_races": int(len(g)),
            "avg_n_combos": round(float(g["n_combos"].mean()), 1),
            "uniform_roi%": round((ret / cost - 1) * 100, 2) if cost else None,
            "pool_roi%": round((pr / pc - 1) * 100, 2) if pc else None,
        })
    return pd.DataFrame(rows)


# ─── 組合せ確率変換 (Harville, モデル依存) ──────────────────────────────

def build_combo_probs(probs: pd.DataFrame) -> pd.DataFrame:
    """per-car p_win を per-race 正規化 → ST2/SH2 の組合せ確率を生成。

    返り値: race_date, place_code, race_no, bet_type, kumi_ban, p_combo
    """
    out_rows: list[dict] = []
    n_races = 0
    for (rd, pc, rn), g in probs.groupby(KEYS, sort=False):
        cars = g["car_no"].astype(int).tolist()
        pw = g["p_win"].astype(float).to_numpy()
        s = pw.sum()
        if s <= 0 or len(cars) < 2:
            continue
        q = pw / s  # per-race 正規化 (Σq=1)
        qmap = dict(zip(cars, q))
        n_races += 1
        # ST2 (順序付き i→j): q_i * q_j/(1-q_i)
        # SH2 (無順序{i,j}): P(i-j)+P(j-i)
        for i in cars:
            qi = qmap[i]
            denom_i = 1.0 - qi
            if denom_i <= 0:
                continue
            for j in cars:
                if i == j:
                    continue
                qj = qmap[j]
                p_ij = qi * qj / denom_i  # P(1st=i, 2nd=j)
                out_rows.append({
                    "race_date": rd, "place_code": pc, "race_no": rn,
                    "bet_type": "ST2", "kumi_ban": f"{i}-{j}", "p_combo": p_ij,
                })
        # SH2 無順序ペア
        for a_idx in range(len(cars)):
            for b_idx in range(a_idx + 1, len(cars)):
                i, j = cars[a_idx], cars[b_idx]
                qi, qj = qmap[i], qmap[j]
                p = 0.0
                if 1 - qi > 0:
                    p += qi * qj / (1 - qi)
                if 1 - qj > 0:
                    p += qj * qi / (1 - qj)
                lo, hi = sorted([i, j])
                out_rows.append({
                    "race_date": rd, "place_code": pc, "race_no": rn,
                    "bet_type": "SH2", "kumi_ban": f"{lo}={hi}", "p_combo": p,
                })
    print(f"  組合せ確率: {n_races:,} races → {len(out_rows):,} combos (ST2+SH2)")
    return pd.DataFrame(out_rows)


def check_combo_prob_normalization(combo: pd.DataFrame) -> None:
    """組合せ確率が per-race per-bet で Σ≈1 になるか (正規化健全性)。"""
    s = combo.groupby(KEYS + ["bet_type"])["p_combo"].sum()
    for bt in ["ST2", "SH2"]:
        sub = s[s.index.get_level_values("bet_type") == bt]
        if len(sub) == 0:
            continue
        print(f"    {bt}: Σp_combo per-race  median={sub.median():.4f} "
              f"mean={sub.mean():.4f} (理想=1.0)")


def main() -> None:
    _stdout_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--probs", default="ml_pred_probs.csv",
                    help="per-car 確率ファイル (default ml_pred_probs.csv)")
    ap.add_argument("--no-cache", action="store_true",
                    help="確定月の dedup キャッシュ (cache/*.parquet) を使わず全て CSV から再計算")
    ap.add_argument("--dry-run", action="store_true",
                    help="ev_prerace_merged.csv を書かない (読み込み・健全性チェックのみ)")
    args = ap.parse_args()

    print("=" * 70)
    print("本番 EV 配管 (Phase B) — 配管検証 + 健全性 2 指標")
    print(f"  期間: {args.start or '(全)'} 〜 {args.end or '(全)'}")
    print("=" * 70)

    print("\n[1] オッズ / 払戻ロード (dedup)")
    odds = load_prerace_dedup(args.start, args.end, use_cache=not args.no_cache)
    pay = load_payouts(args.start, args.end)

    # ── 健全性チェック 1: overround ──
    print("\n" + "=" * 70)
    print("健全性 (1): dedup 後 overround = Σ(1/odds)  [理想 ≈ 1.34, 控除率≈25%]")
    print("=" * 70)
    ov = sanity_overround(odds)
    print(ov.to_string(index=False))

    # ── 健全性チェック 2: uniform 全買い ROI ──
    print("\n" + "=" * 70)
    print("健全性 (2): EV ゲート無し全買い ROI")
    print("  pool_roi%(清算検証) が全券種 ≈ -25% なら清算ロジック健全。")
    print("  uniform は exotics で -40% 前後が正常 (本命勝ち bias)。W は range odds 注意。")
    print("=" * 70)
    ab = sanity_allbuy_roi(odds, pay)
    print(ab.to_string(index=False))

    # ── 組合せ確率変換 + EV 結合 (モデル依存) ──
    probs_path = DATA / args.probs
    if not probs_path.exists():
        print(f"\n[skip] {probs_path} が無いため EV 結合はスキップ。")
        print("       ml_baseline.py 実行後に再度このスクリプトを回すこと。")
        return

    print(f"\n[2] per-car 確率ロード: {args.probs}")
    probs = pd.read_csv(
        probs_path,
        dtype={"race_date": str, "place_code": str, "race_no": str},
    )
    if args.start:
        probs = probs[probs["race_date"] >= args.start]
    if args.end:
        probs = probs[probs["race_date"] <= args.end]
    print(f"  per-car probs: {len(probs):,} 行, "
          f"日付 {probs['race_date'].min()} 〜 {probs['race_date'].max()}")

    print("\n[3] 組合せ確率変換 (Harville, ST2/SH2)")
    combo = build_combo_probs(probs)
    if len(combo) == 0:
        print("  [警告] 組合せ確率 0 件。probs の car_no / p_win を確認。")
        return
    print("  正規化健全性:")
    check_combo_prob_normalization(combo)

    print("\n[4] EV 結合 (combo prob × prerace odds)")
    odds_st = odds[odds["bet_type"].isin(["ST2", "SH2"])]
    merged = combo.merge(
        odds_st[JOIN_KEYS + ["odds"]], on=JOIN_KEYS, how="inner"
    )
    merged["ev"] = merged["p_combo"] * merged["odds"]
    # join カバレッジ
    combo_races = combo[KEYS].drop_duplicates()
    odds_races = odds_st[KEYS].drop_duplicates()
    overlap = combo_races.merge(odds_races, on=KEYS, how="inner")
    print(f"  picks のレース: {len(combo_races):,}, "
          f"odds のレース(ST2/SH2): {len(odds_races):,}, "
          f"重複: {len(overlap):,}")
    print(f"  EV 結合行数: {len(merged):,}")
    if len(merged):
        for bt in ["ST2", "SH2"]:
            sub = merged[merged["bet_type"] == bt]
            if len(sub) == 0:
                continue
            print(f"    {bt}: n={len(sub):,}, EV median={sub['ev'].median():.3f}, "
                  f"EV mean={sub['ev'].mean():.3f}, "
                  f"EV>1 の割合={float((sub['ev']>1).mean())*100:.1f}%")
        out = DATA / "ev_prerace_merged.csv"
        if args.dry_run:
            print(f"\n  [dry-run] 保存スキップ: {out}")
        else:
            merged.to_csv(out, index=False)
            print(f"\n  保存: {out}")

    print("\n" + "=" * 70)
    print("※ Phase B は配管検証のみ。EV>1 割合や券種優劣から黒字/採否は判断しない。")
    print("  本番判定は母数蓄積 + 約定実現性 (Phase A) + FDR 補正後 (Phase C)。")
    print("=" * 70)


if __name__ == "__main__":
    main()
