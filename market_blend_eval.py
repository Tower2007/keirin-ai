"""市場ブレンド (Benter 型ロジスティック結合) のオフライン評価 - 測定専用
=======================================================================
banei src/recalibrator.py / boat ml_market_blend.py と同型の
  blend_logit = b0 + b1*logit(model_p) + b2*logit(market_p)
を競輪に適用する価値があるかを、実装前に定量するスクリプト。
読み込みのみ (D: のデータは一切変更しない)。出力は標準出力のみ。

競輪固有の設計 (単勝オッズが存在しない):
  race_odds_prerace.csv の券種は SH2/ST2/RH3/RT3/W/WH2/WT2 で単勝が無い。
  そこで ST2 (二車単) の 1/odds をレース内合計=1 に正規化 (de-vig) し、
  1着側 marginal Σ_j P(i→j) を車 i の「市場勝率」とする (Σ_i = 1)。
  これが banei の「単勝 de-vig 合計=1」の競輪等価物。
  y_top2 の参考評価には SH2 (二車複) marginal (合計=2、boat の複勝方式) を使う。

ターゲット選定の根拠:
  本番配管 (ev_prerace_pipeline.py) は p_win を per-race 正規化した q を
  Harville で組合せ確率に変換して EV を計算する。よって主評価は y_win
  (model = 正規化 q_win)。y_top2 は参考。

評価プロトコル:
  walk-forward k=5 (レースを日付・場・R 数値ソートでブロック化、過去で fit
  して次ブロックで OOS 評価) で model / market / blend の logloss・brier。
  Δlogloss はレース単位クラスタ bootstrap (B=1000) で 95% CI。
  さらに OOF 確率で Harville を引き直し EV 選別の実払戻 ROI を比較する。

判定 (2026-07-03 測定、結論は reports/market_blend_eval_2026-07-03.md):
  coef_model ≈ 0 (T-1: +0.014 / T-2: -0.003 / T-5: +0.063)、
  blend vs market の Δlogloss は全 offset・全ターゲットで CI がゼロを跨ぐ
  → production_no_odds は市場に対し直交情報を持たず、ブレンド実装は中止。
  モデル改良 (ライン特徴量等) 後に本スクリプトを再実行して再判定すること。

使い方:
  python market_blend_eval.py            # T-1 分前 (本番推奨 offset)
  python market_blend_eval.py --offset 2 # T-2 分前
  python market_blend_eval.py --self-test
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from src.config import DATA_DIR as DATA

KEYS = ["race_date", "place_code", "race_no"]
EPS = 1e-4
# ml_pred_probs (OOS 予測) と prerace odds の重複範囲。データが伸びたら --end で拡張
START_DEFAULT = "2026-05-16"


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def load_odds(bet_type: str, offset: float, start: str, end: str) -> pd.DataFrame:
    """prerace odds を券種・offset で絞り、(race,kumi) 最新 snapshot に dedup。"""
    df = pd.read_csv(
        DATA / "race_odds_prerace.csv",
        usecols=KEYS + ["bet_type", "kumi_ban", "odds", "snapshot_dt",
                        "target_offset_min"],
        dtype={k: str for k in KEYS} | {"bet_type": str, "kumi_ban": str},
        low_memory=False,
    )
    df = df[(df.bet_type == bet_type)
            & (df.race_date >= start) & (df.race_date <= end)]
    df = df[pd.to_numeric(df.target_offset_min, errors="coerce") == offset]
    df["odds"] = pd.to_numeric(df.odds, errors="coerce")
    df = df[df.odds > 0]
    df = df.sort_values("snapshot_dt").drop_duplicates(
        subset=KEYS + ["kumi_ban"], keep="last")
    return df.drop(columns=["snapshot_dt", "target_offset_min"])


def market_win_from_st2(st2: pd.DataFrame) -> pd.DataFrame:
    """ST2 de-vig の 1着 marginal = 市場勝率 (レース内合計=1)。

    完全行列 (n 車なら n*(n-1) 組) のレースのみ採用。欠けがあれば
    marginal が歪むためレースごとスキップ (フェイルセーフ)。
    """
    rows: list[tuple] = []
    for key, g in st2.groupby(KEYS, sort=False):
        parts = g.kumi_ban.str.split("-", expand=True)
        try:
            i_car = parts[0].astype(int)
            j_car = parts[1].astype(int)
        except (ValueError, KeyError):
            continue
        cars = set(i_car) | set(j_car)
        n = len(cars)
        if n < 5 or len(g) != n * (n - 1):
            continue
        inv = 1.0 / g.odds.to_numpy()
        p = inv / inv.sum()
        marg = pd.Series(p).groupby(i_car.to_numpy()).sum()
        for car, mp in marg.items():
            rows.append(key + (int(car), float(mp)))
    return pd.DataFrame(rows, columns=KEYS + ["car_no", "mkt_win"])


def market_top2_from_sh2(sh2: pd.DataFrame) -> pd.DataFrame:
    """SH2 de-vig の contains marginal = 市場連対率 (レース内合計=2)。"""
    rows: list[tuple] = []
    for key, g in sh2.groupby(KEYS, sort=False):
        parts = g.kumi_ban.str.split("=", expand=True)
        try:
            a = parts[0].astype(int)
            b = parts[1].astype(int)
        except (ValueError, KeyError):
            continue
        cars = set(a) | set(b)
        n = len(cars)
        if n < 5 or len(g) != n * (n - 1) // 2:
            continue
        inv = 1.0 / g.odds.to_numpy()
        p = inv / inv.sum()  # 組合せ和=1。各車は複数組に現れ marginal 和=2
        s = pd.Series(np.concatenate([p, p]),
                      index=np.concatenate([a.to_numpy(), b.to_numpy()]))
        marg = s.groupby(level=0).sum()
        for car, mp in marg.items():
            rows.append(key + (int(car), float(mp)))
    return pd.DataFrame(rows, columns=KEYS + ["car_no", "mkt_top2"])


def load_probs(start: str, end: str) -> pd.DataFrame:
    """production_no_odds の OOS 予測 (weekly_retrain が差分生成、in-sample 無し)。"""
    p = pd.read_csv(DATA / "ml_pred_probs.csv", dtype={k: str for k in KEYS})
    p = p[(p.race_date >= start) & (p.race_date <= end)].copy()
    p["car_no"] = p.car_no.astype(int)
    # 本番配管 (ev_prerace_pipeline) と同じ per-race 正規化 q
    p["q_win"] = p.groupby(KEYS).p_win.transform(lambda s: s / s.sum())
    return p


def load_results(start: str, end: str) -> pd.DataFrame:
    r = pd.read_csv(DATA / "race_results.csv",
                    usecols=KEYS + ["car_no", "tyaku"],
                    dtype={k: str for k in KEYS}, low_memory=False)
    r = r[(r.race_date >= start) & (r.race_date <= end)].copy()
    r["tyaku"] = pd.to_numeric(r.tyaku, errors="coerce")
    r = r.dropna(subset=["tyaku"])
    r["car_no"] = pd.to_numeric(r.car_no, errors="coerce")
    r = r.dropna(subset=["car_no"])
    r["car_no"] = r.car_no.astype(int)
    r["y_win"] = (r.tyaku == 1).astype(int)
    r["y_top2"] = (r.tyaku <= 2).astype(int)
    ok = r.groupby(KEYS).y_win.transform("sum") == 1  # 1着1人のクリーンなレースのみ
    return r[ok].drop(columns=["tyaku"])


def wf_blocks(df: pd.DataFrame, k: int) -> pd.DataFrame:
    """レースを (日付, 場, R) 数値ソートで k ブロックに分割 (文字列ソート禁止)。"""
    races = df[KEYS].drop_duplicates().copy()
    races["_p"] = pd.to_numeric(races.place_code, errors="coerce")
    races["_r"] = pd.to_numeric(races.race_no, errors="coerce")
    races = (races.sort_values(["race_date", "_p", "_r"])
             .drop(columns=["_p", "_r"]).reset_index(drop=True))
    races["blk"] = np.arange(len(races)) * k // len(races)
    return df.merge(races, on=KEYS, how="left")


def fit_blend(mdl, mkt, y) -> dict:
    from sklearn.linear_model import LogisticRegression
    x = np.column_stack([logit(mdl), logit(mkt)])
    lr = LogisticRegression(C=1.0, max_iter=1000)
    lr.fit(x, np.asarray(y, dtype=int))
    return {"intercept": float(lr.intercept_[0]),
            "coef_model": float(lr.coef_[0][0]),
            "coef_market": float(lr.coef_[0][1])}


def apply_blend(mdl, mkt, c: dict) -> np.ndarray:
    z = (c["intercept"] + c["coef_model"] * logit(mdl)
         + c["coef_market"] * logit(mkt))
    return 1.0 / (1.0 + np.exp(-z))


def cluster_boot_dll(df, col_a, col_b, ycol, rng, B=1000):
    """Δlogloss = LL(a) - LL(b) のレースクラスタ bootstrap 95% CI (正なら b 優位)。"""
    per = []
    for _, g in df.groupby(KEYS, sort=False):
        y = g[ycol].to_numpy(float)
        pa = np.clip(g[col_a].to_numpy(float), EPS, 1 - EPS)
        pb = np.clip(g[col_b].to_numpy(float), EPS, 1 - EPS)
        lla = -(y * np.log(pa) + (1 - y) * np.log(1 - pa)).sum()
        llb = -(y * np.log(pb) + (1 - y) * np.log(1 - pb)).sum()
        per.append((lla, llb, len(g)))
    arr = np.array(per)
    point = (arr[:, 0].sum() - arr[:, 1].sum()) / arr[:, 2].sum()
    idx = rng.integers(0, len(arr), size=(B, len(arr)))
    boots = ((arr[idx, 0].sum(axis=1) - arr[idx, 1].sum(axis=1))
             / arr[idx, 2].sum(axis=1))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return point, lo, hi


def evaluate(df, mdl_col, mkt_col, ycol, label, rng, k=5):
    from sklearn.metrics import brier_score_loss, log_loss
    print(f"\n--- {label}: n={len(df)} races={df.groupby(KEYS).ngroups} "
          f"pos={int(df[ycol].sum())} ---")
    dd = wf_blocks(df, k)
    oos = []
    for i in range(1, k):
        tr, te = dd[dd.blk < i], dd[dd.blk == i]
        if te.empty or tr[ycol].nunique() < 2:
            continue
        c = fit_blend(tr[mdl_col], tr[mkt_col], tr[ycol])
        t = te[KEYS + ["car_no", ycol, mdl_col, mkt_col]].copy()
        t["blend"] = apply_blend(te[mdl_col], te[mkt_col], c)
        oos.append(t)
    oos = pd.concat(oos, ignore_index=True)
    for name, col in [("model", mdl_col), ("market", mkt_col),
                      ("blend", "blend")]:
        pp = np.clip(oos[col].to_numpy(float), EPS, 1 - EPS)
        print(f"  {name:7s} logloss={log_loss(oos[ycol], pp, labels=[0, 1]):.4f} "
              f"brier={brier_score_loss(oos[ycol], pp):.4f}")
    cfull = fit_blend(df[mdl_col], df[mkt_col], df[ycol])
    print(f"  全期間係数: intercept={cfull['intercept']:.4f} "
          f"coef_model={cfull['coef_model']:.4f} "
          f"coef_market={cfull['coef_market']:.4f}")
    for a, b, tag in [(mdl_col, "blend", "model->blend"),
                      (mkt_col, "blend", "market->blend")]:
        pt, lo, hi = cluster_boot_dll(oos, a, b, ycol, rng)
        verdict = "有意" if lo > 0 else ("blend劣後" if hi < 0 else "CI跨ぎ")
        print(f"  dlogloss({tag}) = {pt:+.4f}  CI95=[{lo:+.4f},{hi:+.4f}] {verdict}")
    return oos, cfull


def harville_combo(q_by_car: dict) -> dict:
    """{car: q} -> {('ST2','i-j'): p, ('SH2','i=j'): p} (ev_prerace_pipeline と同式)。"""
    out: dict = {}
    cars = list(q_by_car)
    for i in cars:
        qi = q_by_car[i]
        if 1 - qi <= 0:
            continue
        for j in cars:
            if i == j:
                continue
            out[("ST2", f"{i}-{j}")] = qi * q_by_car[j] / (1 - qi)
    for a in range(len(cars)):
        for b in range(a + 1, len(cars)):
            i, j = cars[a], cars[b]
            p = 0.0
            if 1 - q_by_car[i] > 0:
                p += q_by_car[i] * q_by_car[j] / (1 - q_by_car[i])
            if 1 - q_by_car[j] > 0:
                p += q_by_car[j] * q_by_car[i] / (1 - q_by_car[j])
            out[("SH2", f"{min(i, j)}={max(i, j)}")] = p
    return out


def roi_sim(oos, odds_all, payouts, prob_col, label):
    """OOF 確率を per-race 正規化 -> Harville -> EV 選別 -> 実払戻 ROI。"""
    recs = []
    for key, g in oos.groupby(KEYS, sort=False):
        q = g.set_index("car_no")[prob_col]
        s = q.sum()
        if s <= 0:
            continue
        q = (q / s).to_dict()
        for (bt, kumi), p in harville_combo(q).items():
            recs.append(key + (bt, kumi, p))
    combo = pd.DataFrame(recs, columns=KEYS + ["bet_type", "kumi_ban", "p"])
    m = combo.merge(odds_all[KEYS + ["bet_type", "kumi_ban", "odds"]],
                    on=KEYS + ["bet_type", "kumi_ban"], how="inner")
    m["ev"] = m.p * m.odds
    m = m.merge(payouts, on=KEYS + ["bet_type", "kumi_ban"], how="left")
    m["ret"] = m.payout.fillna(0.0)
    print(f"\n  [ROI sim: {label}] (100円均等、実払戻清算)")
    for bt in ["ST2", "SH2"]:
        for thr in [1.0, 1.2, 1.5]:
            s = m[(m.bet_type == bt) & (m.ev >= thr)]
            if len(s) == 0:
                print(f"    {bt} EV>={thr}: n=0")
                continue
            roi = s.ret.sum() / (len(s) * 100) * 100
            print(f"    {bt} EV>={thr}: n={len(s):6d} "
                  f"hit={int((s.ret > 0).sum()):4d} ROI={roi:6.1f}%")


def self_test() -> int:
    """pytest 非依存の自己検証 (合成データ、D: 読み込み不要)。"""
    # 1) ST2 marginal: 一様オッズ 5 車 (20 組) なら市場勝率は各 1/5
    cars = [1, 2, 3, 4, 5]
    st2_kumi = [f"{i}-{j}" for i in cars for j in cars if i != j]
    st2 = pd.DataFrame({
        "race_date": ["2026-01-01"] * len(st2_kumi),
        "place_code": ["11"] * len(st2_kumi),
        "race_no": ["1"] * len(st2_kumi),
        "kumi_ban": st2_kumi,
        "odds": [20.0] * len(st2_kumi),
    })
    mw = market_win_from_st2(st2)
    assert len(mw) == 5 and np.allclose(mw.mkt_win, 1 / 5), mw
    assert abs(mw.mkt_win.sum() - 1.0) < 1e-9
    # 2) 不完全行列 (1組欠け) はレースごとスキップ
    mw2 = market_win_from_st2(st2.iloc[:-1])
    assert len(mw2) == 0
    # 3) SH2 marginal: 一様 10 組なら各車 2/5、合計=2
    sh2_kumi = [f"{i}={j}" for a, i in enumerate(cars)
                for j in cars[a + 1:]]
    sh2 = pd.DataFrame({
        "race_date": ["2026-01-01"] * len(sh2_kumi),
        "place_code": ["11"] * len(sh2_kumi),
        "race_no": ["1"] * len(sh2_kumi),
        "kumi_ban": sh2_kumi,
        "odds": [10.0] * len(sh2_kumi),
    })
    mt = market_top2_from_sh2(sh2)
    assert len(mt) == 5 and np.allclose(mt.mkt_top2, 2 / 5)
    assert abs(mt.mkt_top2.sum() - 2.0) < 1e-9
    # 4) Harville: Sum p(ST2) = Sum p(SH2) = 1
    combos = harville_combo({1: 0.5, 2: 0.3, 3: 0.2})
    s_st2 = sum(v for (bt, _), v in combos.items() if bt == "ST2")
    s_sh2 = sum(v for (bt, _), v in combos.items() if bt == "SH2")
    assert abs(s_st2 - 1.0) < 1e-9 and abs(s_sh2 - 1.0) < 1e-9
    # 5) fit/apply の往復: market だけが真実なら coef_market が支配的
    rng = np.random.default_rng(7)
    mkt = rng.uniform(0.05, 0.6, 4000)
    mdl = np.clip(mkt + rng.normal(0, 0.15, 4000), 0.01, 0.95)  # ノイズ入り模倣
    y = (rng.uniform(size=4000) < mkt).astype(int)
    c = fit_blend(mdl, mkt, y)
    assert c["coef_market"] > c["coef_model"], c
    out = apply_blend(mdl, mkt, c)
    assert np.isfinite(out).all() and (out > 0).all() and (out < 1).all()
    print("self-test OK (5/5)")
    return 0


def main() -> int:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description="市場ブレンドのオフライン評価 (読み込みのみ)")
    ap.add_argument("--offset", type=float, default=1,
                    help="snapshot offset 分前 (既定 1 = Phase A の最遅約定可能点)")
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default="2099-12-31")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260703)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    rng = np.random.default_rng(args.seed)
    print(f"=== keirin 市場ブレンド オフライン評価 (T-{args.offset}分) ===")
    probs = load_probs(args.start, args.end)
    if probs.empty:
        print("WARN: ml_pred_probs が期間内 0 行")
        return 1
    print(f"probs: {len(probs)} 行 "
          f"({probs.race_date.min()}..{probs.race_date.max()})")
    res = load_results(args.start, args.end)
    st2 = load_odds("ST2", args.offset, args.start, args.end)
    mkt_w = market_win_from_st2(st2)
    print(f"市場勝率 (ST2 marginal): races={mkt_w.groupby(KEYS).ngroups if len(mkt_w) else 0}")

    d = (probs.merge(mkt_w, on=KEYS + ["car_no"], how="inner")
              .merge(res, on=KEYS + ["car_no"], how="inner"))
    if d.empty:
        print("WARN: 突合 0 行 (odds と probs の期間重複を確認)")
        return 1
    cnt = d.groupby(KEYS).car_no.transform("count")
    full = d.groupby(KEYS).mkt_win.transform("sum")
    d = d[(cnt >= 5) & (full > 0.995) & (full < 1.005)].copy()
    print(f"y_win 突合: n={len(d)} races={d.groupby(KEYS).ngroups} "
          f"期間={d.race_date.min()}..{d.race_date.max()}")
    oos_w, _ = evaluate(d, "q_win", "mkt_win", "y_win",
                        f"y_win (model=q_win, market=ST2 marginal, T-{args.offset})",
                        rng, args.k)

    sh2 = load_odds("SH2", args.offset, args.start, args.end)
    mkt_t2 = market_top2_from_sh2(sh2)
    d2 = (probs.merge(mkt_t2, on=KEYS + ["car_no"], how="inner")
               .merge(res, on=KEYS + ["car_no"], how="inner"))
    if len(d2):
        cnt = d2.groupby(KEYS).car_no.transform("count")
        full = d2.groupby(KEYS).mkt_top2.transform("sum")
        d2 = d2[(cnt >= 5) & (full > 1.99) & (full < 2.01)].copy()
        d2["p_top2c"] = d2.p_top2.clip(EPS, 1 - EPS)
        evaluate(d2, "p_top2c", "mkt_top2", "y_top2",
                 f"y_top2 (model=p_top2, market=SH2 marginal, T-{args.offset}) [参考]",
                 rng, args.k)

    pay = pd.read_csv(DATA / "payouts.csv",
                      usecols=KEYS + ["bet_type", "kumi_ban", "payout"],
                      dtype={k: str for k in KEYS}
                      | {"bet_type": str, "kumi_ban": str}, low_memory=False)
    pay = pay[(pay.race_date >= args.start) & (pay.race_date <= args.end)]
    pay["payout"] = pd.to_numeric(pay.payout, errors="coerce")
    pay = (pay[pay.payout > 0]
           .drop_duplicates(subset=KEYS + ["bet_type", "kumi_ban"], keep="last"))
    odds_all = pd.concat([st2, sh2], ignore_index=True)
    roi_sim(oos_w, odds_all, pay, "q_win", "model q_win (OOF window)")
    roi_sim(oos_w, odds_all, pay, "blend", "blend (OOF)")
    roi_sim(oos_w, odds_all, pay, "mkt_win",
            "market marginal only [参考: Harville 歪み検出用]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
