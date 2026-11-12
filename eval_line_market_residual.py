r"""実ラインの再評価 — 市場残差を目的変数にした離陸測定 (測定専用・非破壊, 2026-11-12)
=======================================================================
eval_line_liftoff.py (2026-07-07) の後継の測定。当時は実ラインの被覆が学習 5 年の約 2% で、
採用モデルのライン特徴量 gain=0、結合係数 coef_model は 0 をまたいだ (NO-GO)。
race_lines.csv の被覆が 2026-04-26〜 の約 6.5 か月に増えたので、**市場がすでに織り込んだ分を
除いた残り (市場残差)** にラインが情報を持つかだけを測る。

手順 (本番の特徴量セット・モデル・D: のデータは一切変えない):
  1. 市場 1 着確率 q: 直前二車単 (dedup = 締切に最も近い snapshot) を de-vig し 1 着側で周辺化
     (market_anchor.market_win_from_st2)。
  2. 実ライン特徴量 13 種 (build_line_real_features.build_features をメモリ上で再計算)。
  3. LightGBM (小さい木) を init_score = logit(q) で学習 = 市場からの残差だけを学ぶ。
     学習 〜TRAIN_END、係数の当てはめ CALIB 月、評価 TEST 期間 (いずれも学習より後)。
  4. 残差マージン f (= 予測 logit - logit(q)) を条件付きロジット p ∝ q^a · exp(c·f) に入れ、
     c の 95%CI (レース単位ブートストラップ, CALIB+TEST) と、TEST でのレース単位対数損失
     (市場^α vs 市場+ライン) を出す。c の CI が 0 をまたげば「市場に対する上積みなし」。

使い方:
  python eval_line_market_residual.py [出力ディレクトリ]   # 既定はカレント。CSV/MD をそこへ書く
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KEYS = ["race_date", "place_code", "race_no"]
MARKET_START = "2026-05-16"   # 直前オッズの収集開始
TRAIN_END = "2026-08-31"
CALIB = ("2026-09-01", "2026-09-30")
TEST_START = "2026-10-01"
N_BOOT = 200


def _load(data_dir: Path) -> pd.DataFrame:
    import market_anchor as ma
    import build_line_real_features as blf
    from src.odds_prerace import load_prerace_dedup
    import ml_baseline as mlb

    lines = blf.build_features()
    for c in ("place_code", "race_no", "car_no"):
        lines[c] = pd.to_numeric(lines[c], errors="coerce").astype("Int64")
    lines["race_date"] = lines["race_date"].astype(str)
    odds, _ = load_prerace_dedup(start=MARKET_START, data_dir=data_dir)
    mk = ma.market_win_from_st2(odds)
    res = pd.read_csv(data_dir / "race_results.csv", dtype=str, low_memory=False,
                      usecols=KEYS + ["car_no", "tyaku"])
    res = res[res["race_date"] >= MARKET_START]
    for c in ("place_code", "race_no", "car_no"):
        res[c] = pd.to_numeric(res[c], errors="coerce").astype("Int64")
    res["win"] = (pd.to_numeric(res["tyaku"], errors="coerce") == 1).astype(int)
    d = res.drop_duplicates(KEYS + ["car_no"]).merge(mk, on=KEYS + ["car_no"], how="inner")
    d = d.merge(lines, on=KEYS + ["car_no"], how="inner")
    g = d.groupby(KEYS)
    n_res = res.groupby(KEYS).size().rename("n_res")
    d = d.merge(n_res.reset_index(), on=KEYS, how="left")
    d = d[(g["car_no"].transform("size") == d["n_res"]) & (g["win"].transform("sum") == 1)].copy()
    d["mkt_win"] = d["mkt_win"] / d.groupby(KEYS)["mkt_win"].transform("sum")
    d["race_id"] = (d["race_date"] + "_" + d["place_code"].astype(int).astype(str) + "_"
                    + d["race_no"].astype(int).astype(str))
    d.attrs["features"] = list(mlb.LINE_REAL_NUM_FEATURES)
    return d.reset_index(drop=True)


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def main() -> int:
    from src.config import DATA_DIR
    import lightgbm as lgb
    import market_anchor as ma

    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    d = _load(Path(DATA_DIR))
    feats = d.attrs["features"]
    print(f"[1] 突合 {len(d):,} 行 / {d['race_id'].nunique():,} R ({d['race_date'].min()}..{d['race_date'].max()})"
          f" {time.time() - t0:.1f}s")
    tr = d[d["race_date"] <= TRAIN_END]
    params = {"objective": "binary", "learning_rate": 0.03, "num_leaves": 7,
              "min_data_in_leaf": 300, "feature_fraction": 0.9, "lambda_l2": 10.0,
              "verbosity": -1, "seed": 0}
    vmask = tr["race_date"] > "2026-08-01"
    dtr = lgb.Dataset(tr.loc[~vmask, feats], tr.loc[~vmask, "win"], init_score=_logit(tr.loc[~vmask, "mkt_win"]))
    dva = lgb.Dataset(tr.loc[vmask, feats], tr.loc[vmask, "win"], init_score=_logit(tr.loc[vmask, "mkt_win"]))
    bst = lgb.train(params, dtr, num_boost_round=600, valid_sets=[dva],
                    callbacks=[lgb.early_stopping(50, verbose=False)])
    print(f"[2] 残差モデル best_iter={bst.best_iteration} (学習 〜{TRAIN_END}, {tr['race_id'].nunique():,} R)")
    oos = d[d["race_date"] >= CALIB[0]].copy()
    oos["f"] = bst.predict(oos[feats], num_iteration=bst.best_iteration, raw_score=True)
    cal = oos[oos["race_date"] <= CALIB[1]]
    te = oos[oos["race_date"] >= TEST_START]

    def frame(x, with_f):
        y = x.copy()
        # market_anchor の m に exp(f) を入れる。条件付きロジットはレース内の定数ずれに不変なので
        # 全体の最大で割って (0, 1] に収める (_Races は log(clip(m, 1e-9, 1)) を取るため)
        y["p_win"] = np.exp(y["f"] - y["f"].max()) if with_f else 1.0
        return y

    # α (市場のみ) と (a, c) を CALIB で当てはめ → TEST で評価
    alpha = ma.fit_alpha(frame(cal, False))
    a, c = ma.fit_blend(frame(cal, True))
    r_te = ma._Races(frame(te, True))
    ll_mkt = r_te.nll_grad(1.0, 0.0)[0]
    ll_alpha = r_te.nll_grad(alpha, 0.0)[0]
    ll_line = r_te.nll_grad(a, c)[0]
    ci = ma.bootstrap_b(frame(oos, True), n_boot=N_BOOT)
    c_all = ma.fit_blend(frame(oos, True))[1]
    imp = dict(zip(feats, bst.feature_importance("gain").round(1).tolist()))
    res = {
        "n_rows": int(len(d)), "n_races": int(d["race_id"].nunique()),
        "train_races": int(tr["race_id"].nunique()), "calib_races": int(cal["race_id"].nunique()),
        "test_races": int(te["race_id"].nunique()), "best_iter": int(bst.best_iteration),
        "alpha": alpha, "a": a, "c_calib": c, "c_oos": c_all,
        "c_ci95": list(ci) if ci else None,
        "test_race_ll": {"market": ll_mkt, "market_alpha": ll_alpha, "market_plus_line": ll_line},
        "gain": imp, "seconds": round(time.time() - t0, 1),
    }
    (out_dir / "line_market_residual.json").write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
    verdict = ("0 と区別できる (ラインに市場残差の情報あり)" if ci and (ci[0] > 0 or ci[1] < 0)
               else "0 をまたぐ (市場に対する上積みの証拠なし)")
    print(f"[3] c (残差マージンの係数) CALIB={c:+.3f} / OOS 全体={c_all:+.3f} 95%CI "
          f"[{ci[0]:+.3f}, {ci[1]:+.3f}] → {verdict}")
    print(f"    TEST {res['test_races']:,} R レース単位対数損失: 市場 {ll_mkt:.4f} / 市場^α {ll_alpha:.4f} / "
          f"市場+ライン {ll_line:.4f}")
    print(f"    gain: {imp}")
    print(f"[done] {res['seconds']}s → {out_dir / 'line_market_residual.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
