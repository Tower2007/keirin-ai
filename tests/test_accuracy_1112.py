"""精度検証 2026-11-12 (Keirin C/E) の改善のテスト: 市場基準の計器 / 採用ゲート v3 / early stopping。

合成データのみ (D: のデータ・モデル・ネットワーク不要)。メール送信は conftest が禁止している。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_anchor as ma  # noqa: E402
import shadow_eval as se  # noqa: E402
import weekly_retrain as wr  # noqa: E402


# ---------------------------------------------------------------- market_anchor
def _synthetic(n_races=900, alpha_true=1.15, model_info=0.0, seed=0, start="2026-07-01", noise_sd=1.0):
    rng = np.random.default_rng(seed)
    rows, odds = [], []
    d0 = pd.Timestamp(start)
    for r in range(n_races):
        k = 7
        s = rng.gamma(2.0, 1.0, k)
        q = s / s.sum()                                   # 市場 (de-vig) の 1 着確率
        z = rng.normal(0, 0.7, k)                         # 市場が知らない情報
        true = q ** alpha_true * np.exp(model_info * z)  # 真の確率 = 市場^α × (モデルだけが知る情報)
        true /= true.sum()
        w = rng.choice(k, p=true)
        m = np.exp(z + rng.normal(0, noise_sd, k)); m /= m.sum()
        day = (d0 + pd.Timedelta(days=r // 30)).strftime("%Y-%m-%d")
        for i in range(k):
            rows.append({"race_date": day, "place_code": "11", "race_no": str(r % 30 + 1),
                         "car_no": str(i + 1), "p_win": m[i], "tyaku": "1" if i == w else "5"})
            for j in range(k):
                if i != j:  # 二車単: 1 着 i の周辺が q になるよう均等割り
                    odds.append({"race_date": day, "place_code": "11", "race_no": str(r % 30 + 1),
                                 "bet_type": "ST2", "kumi_ban": f"{i + 1}-{j + 1}",
                                 "odds": 0.75 / (q[i] / (k - 1))})
    sh = pd.DataFrame(rows)
    return sh, sh[["race_date", "place_code", "race_no", "car_no", "tyaku"]], pd.DataFrame(odds)


def test_market_win_from_st2_sums_to_one():
    _, _, odds = _synthetic(n_races=3)
    m = ma.market_win_from_st2(odds)
    s = m.groupby(["race_date", "place_code", "race_no"])["mkt_win"].sum()
    assert np.allclose(s, 1.0)


def test_alpha_recovers_and_model_without_info_gets_zero_b():
    sh, res, odds = _synthetic(n_races=1500, alpha_true=1.15, model_info=0.0)
    d = ma.build_frame(sh, res, odds)
    assert d["race_id"].nunique() == 1500
    assert abs(ma.fit_alpha(d) - 1.15) < 0.08
    a, b = ma.fit_blend(d)
    lo, hi = ma.bootstrap_b(d, n_boot=30)
    assert lo < 0 < hi and abs(b) < 0.05


def test_refit_forward_and_lines(tmp_path):
    sh, res, odds = _synthetic(n_races=2400, model_info=1.0, noise_sd=0.2)   # 7/1〜 80 日 → 8・9 月に適用
    d = ma.build_frame(sh, res, odds)
    hist, added = ma.refit_missing_months(d, [], date(2026, 9, 20), n_boot=20)
    assert added == ["2026-08", "2026-09"]
    assert all(e["train_date_max"] < f"{e['apply_month']}-01" for e in hist)
    hist2, added2 = ma.refit_missing_months(d, hist, date(2026, 9, 25), n_boot=20)
    assert added2 == []                                        # 既存月は書き換えない
    ma.save_history(tmp_path, hist)
    assert [e["apply_month"] for e in ma.load_history(tmp_path)] == ["2026-08", "2026-09"]
    ev = ma.evaluate_forward(d, hist)
    ev["latest"] = hist[-1]
    assert ev["rl_blend"] < ev["rl_mkt"]                       # 情報のあるモデルは結合で効く
    L = ma.lines(ev)
    assert len(L) == 2 and "0 と区別できる" in L[1]
    assert "まだありません" in ma.lines({"n_races": 0})[0]


def test_shadow_eval_anchor_lines_error_and_none():
    assert se._anchor_lines({"market_anchor": {"error": "X: y"}})[0].startswith("市場基準の確率 (計器): 取得失敗")
    assert se._anchor_lines({}) == []


# ---------------------------------------------------------------- 採用ゲート v3
CH = {"auc": 0.8070, "logloss": 0.3250, "ece": 0.008}


def _new(auc, ll, ece=0.008):
    return {"y_win": {"auc": auc, "logloss": ll, "ece": ece}}


def test_same_window_rules():
    meta_fresh = {"trained_at": "2026-11-02T06:00:00"}
    meta_stale = {"trained_at": "2026-09-01T06:00:00"}
    today = date(2026, 11, 9)
    v, r, ok = wr._should_adopt_same_window(_new(0.8071, 0.3249), CH, meta_fresh, today)
    assert (v, ok) == ("OK", True)
    v, r, ok = wr._should_adopt_same_window(_new(0.8055, 0.3252), CH, meta_fresh, today)
    assert (v, ok) == ("WARN", False) and "採用せず" in r          # WARN は採用しない
    v, r, ok = wr._should_adopt_same_window(_new(0.8055, 0.3252), CH, meta_stale, today)
    assert (v, ok) == ("WARN", True) and "鮮度" in r              # 現役が古ければ採用
    v, r, ok = wr._should_adopt_same_window(_new(0.8030, 0.3250), CH, meta_fresh, today)
    assert (v, ok) == ("NG", False)
    v, r, ok = wr._should_adopt_same_window(_new(0.8070, 0.3270), CH, meta_fresh, today)
    assert (v, ok) == ("NG", False)                                # logloss +0.6%
    v, r, ok = wr._should_adopt_same_window(_new(0.60, 0.33), CH, meta_fresh, today)
    assert v == "NG" and "絶対床" in r
    v, r, ok = wr._should_adopt_same_window(_new(0.60, 0.33), CH, meta_fresh, today, force=True)
    assert ok is True


def test_score_champion_fallbacks(tmp_path, monkeypatch):
    df = pd.DataFrame({"race_date": ["2026-10-01"], "y_win": [1], "x": [0.1]})
    assert wr._score_champion_same_window(df, "2026-09-14", "2026-11-08", None)[0] is None
    monkeypatch.setattr(wr, "_model_path", lambda t: tmp_path / "exists.lgb")
    (tmp_path / "exists.lgb").write_text("x", encoding="utf-8")
    meta = {"train_info": {"train_date_max": "2026-09-20"}, "feature_columns": ["x"]}
    r, note = wr._score_champion_same_window(df, "2026-09-14", "2026-11-08", meta)
    assert r is None and "食い込む" in note
    meta = {"train_info": {"train_date_max": "2026-09-01"}, "feature_columns": ["nope"]}
    r, note = wr._score_champion_same_window(df, "2026-09-14", "2026-11-08", meta)
    assert r is None and "揃わない" in note


def test_history_header_has_gate_columns():
    assert wr.HISTORY_HEADER[-4:] == ["gate_mode", "champ_auc_same", "champ_logloss_same",
                                      "champ_ece_same"]


# ---------------------------------------------------------------- early stopping
def test_train_one_early_stops_on_train_tail_not_test(monkeypatch):
    import ml_baseline as mlb
    seen = {}

    class _M:
        best_iteration = 3

        def predict(self, X, num_iteration=None):
            seen["pred_rows"] = len(X)
            return np.full(len(X), 0.5)

    def fake_train(params, train_set, num_boost_round, valid_sets, valid_names, callbacks):
        seen["valid_rows"] = valid_sets[0].data.shape[0]
        seen["train_rows"] = train_set.data.shape[0]
        seen["names"] = valid_names
        return _M()

    monkeypatch.setattr(mlb.lgb, "train", fake_train)
    dates = pd.date_range("2026-01-01", periods=100).strftime("%Y-%m-%d")
    train = pd.DataFrame({"race_date": dates, "f": np.arange(100.0), "y": [0, 1] * 50})
    test = pd.DataFrame({"race_date": ["2026-06-01"] * 37, "f": np.arange(37.0), "y": [0, 1] * 18 + [0]})
    mlb.train_one(train, test, "y", {"objective": "binary"}, ["f"])
    assert seen["names"] == ["val"]
    assert seen["valid_rows"] == 10 and seen["train_rows"] == 90   # 学習期間の末尾 10%
    assert seen["pred_rows"] == 37                                  # 予測は test のまま
