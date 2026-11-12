"""2026-11-12 監査対応の回帰テスト (欠測補完 / shadow 評価 / ゲートB / 通知 / 台帳検査 ほか)。"""

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

import shadow_eval as se


# ─── shadow_eval ─────────────────────────────────────────────────────────

def _shadow(rows):
    cols = ["race_date", "place_code", "race_no", "car_no", "p_win", "p_top2", "p_top3",
            "predicted_at", "run_type"]
    return pd.DataFrame(rows, columns=cols).astype(str)


def test_select_official_prefers_official_and_latest():
    sh = _shadow([
        ("2026-11-13", "11", "1", "1", "0.5", "0.6", "0.7", "2026-11-13 07:30:00", "official"),
        ("2026-11-13", "11", "1", "1", "0.4", "0.5", "0.6", "2026-11-13 07:40:00", "official"),
        ("2026-11-13", "11", "1", "1", "0.9", "0.9", "0.9", "2026-11-13 12:00:00", "rerun"),
        ("2026-11-13", "11", "1", "2", "0.3", "0.3", "0.3", "2026-11-13 07:30:00", "legacy_unverifiable"),
    ])
    out = se.select_official(sh)
    assert len(out) == 1
    assert out.iloc[0]["p_win"] == "0.4"  # official のうち predicted_at 最新


def test_bh_fdr_monotone_and_bounded():
    q = se.bh_fdr([0.001, 0.04, 0.5, 1.0])
    assert all(0 <= x <= 1 for x in q)
    assert q[0] <= q[1] <= q[2] <= q[3]
    assert q[0] == pytest.approx(0.004)


def _race(date_, pc, rn, probs):
    rows = []
    for i, p in enumerate(probs, start=1):
        rows.append((date_, pc, rn, str(i), str(p), str(p * 1.5), str(p * 2),
                     "2026-11-13 07:30:00", "official"))
    return rows


def test_forward_metrics_hit_and_roi():
    sh = _shadow(_race("2026-11-16", "11", "1", [0.4, 0.3, 0.1, 0.1, 0.05, 0.05]))
    res = pd.DataFrame({"race_date": "2026-11-16", "place_code": "11", "race_no": "1",
                        "car_no": [str(i) for i in range(1, 7)], "tyaku": ["1", "2", "3", "4", "5", "6"]})
    pay = pd.DataFrame([("2026-11-16", "11", "1", "SH2", "1=2", "500")],
                       columns=["race_date", "place_code", "race_no", "bet_type", "kumi_ban", "payout"])
    fm = se.forward_metrics(sh, res, {("2026-11-16", "11", "1")}, pay)
    t = fm["total"]
    assert t["n_races"] == 1
    assert t["SH2_hit"] == 1.0 and t["SH2_roi"] == pytest.approx(5.0)
    assert t["ST2_hit"] == 0.0


def test_evaluate_policies_marks_a_not_passed_and_counts_races():
    sh = _shadow(_race("2026-11-16", "11", "1", [0.5, 0.3, 0.1, 0.05, 0.03, 0.02]))
    odds = pd.DataFrame([("2026-11-16", "11", "1", "SH2", "1=2", "9.0", "2026-11-16 10:00:00", "1")],
                        columns=["race_date", "place_code", "race_no", "bet_type", "kumi_ban",
                                 "odds", "snapshot_dt", "target_offset_min"])
    pay = pd.DataFrame([("2026-11-16", "11", "1", "SH2", "1=2", "900")],
                       columns=["race_date", "place_code", "race_no", "bet_type", "kumi_ban", "payout"])
    pol = se.evaluate_policies(sh, odds, {("2026-11-16", "11", "1")}, pay, gate_a=None)
    assert len(pol) == len(se.POLICY_BETS) * len(se.POLICY_EV_THRESHOLDS) * len(se.POLICY_OFFSETS)
    hit = [p for p in pol if p["policy_id"] == "SH2_ev1.00" and p["offset"] == 1][0]
    assert hit["n_races"] == 1 and hit["roi"] == pytest.approx(8.0)
    assert hit["verdict"].startswith("A未通過")
    # ロック日より前のレースは数えない
    pol2 = se.evaluate_policies(sh.assign(race_date="2026-11-01"), odds,
                                {("2026-11-01", "11", "1")}, pay, None)
    assert all(p["n_races"] == 0 for p in pol2)


def test_sunset_gate_b_messages():
    pol = [{"policy_id": "x", "n_races": 300, "weeks": 5, "roi": 0.1, "q": 0.01, "a_pass": True,
            "verdict": "合格候補"}]
    s = se.sunset_status(pol, 1, date(2026, 12, 1))
    assert any("合格候補" in m for m in s["messages"])


def test_sunset_deadlines():
    pol = [{"n_races": 10, "a_pass": False, "verdict": "A未通過 (参考値)"}]
    s = se.sunset_status(pol, 0, date(2027, 2, 6))
    assert any("ゲートA" in m for m in s["messages"])
    s = se.sunset_status(pol, 0, date(2027, 5, 15))
    assert any("打ち切り推奨" in m for m in s["messages"])
    s = se.sunset_status(pol, 0, date(2026, 12, 1))
    assert s["messages"] == []


def test_md_and_html_render_empty_policies():
    sh = _shadow(_race("2026-11-16", "11", "1", [0.5, 0.3, 0.1, 0.05, 0.03, 0.02]))
    res = {"forward": {"weeks": [], "total": {"n_races": 0}},
           "policies": se.evaluate_policies(sh, pd.DataFrame(columns=["race_date"]), set(),
                                            pd.DataFrame(columns=["race_date", "place_code", "race_no",
                                                                  "bet_type", "kumi_ban", "payout"]), None),
           "sunset": se.sunset_status([], 0, date(2026, 11, 13)),
           "n_official_rows": 0, "n_official_days": 0}
    assert any("ゲートB" in x for x in se.md_lines(res))
    import weekly_drift_report as w
    assert se.html_lines(res, "", w.HTML_TBL, w.HTML_TH, w.HTML_TH_R, w.td)
    assert se.md_lines(None, "boom")[2].startswith("⚠️")


# ─── 日次被覆率 / 範囲 / 完了遅延 ─────────────────────────────────────────

def test_coverage_status_thresholds():
    import weekly_drift_report as w
    assert w._coverage_status(100, 100, 500, 100) == "✅ 正常"
    assert w._coverage_status(100, 77, 500, 100).startswith("✅ 正常 (ライン 77%")
    assert w._coverage_status(100, 40, 500, 100).startswith("⚠️ ライン一部欠落")
    assert w._coverage_status(100, 100, 300, 100).startswith("⚠️ オッズ一部欠落")


def test_get_date_range_includes_previous_monday():
    import argparse
    import weekly_drift_report as w
    s, e = w.get_date_range(argparse.Namespace(since=None, days=7))
    assert (e - s).days == 7


def test_summarize_actual_offset_uses_seconds():
    import weekly_drift_report as w
    rows = [{"status": "ok", "target_offset_min": "5", "minutes_before_start": "5",
             "seconds_before_start": "299"},
            {"status": "ok", "target_offset_min": "5", "minutes_before_start": "5",
             "seconds_before_start": "301"}]
    assert w.summarize_actual_offset(rows)[5]["std"] > 0


def test_gate_a_pass_count():
    import weekly_drift_report as w
    g = {"offsets": {5: {"bet_verdicts": {"SH2": True, "ST2": False, "RH3": False, "RT3": False}}}}
    assert w.gate_a_pass_count(g) == (1, 4)


# ─── 遡り取得 / race_quality / 欠測台帳 ────────────────────────────────────

def test_plan_backfill():
    import ingest_yesterday as iy
    comp = pd.DataFrame({"race_date": ["2026-11-05", "2026-11-05", "2026-11-06"],
                         "place_code": ["11", "12", "13"],
                         "status": ["missing_results", "complete", "missing_payout"]})
    meta_dates = {"2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"}  # 11-03 だけ丸ごと無い
    plan = iy.plan_backfill(meta_dates, comp, date(2026, 11, 10), "2026-11-09", lookback=7)
    assert plan["2026-11-03"] is None
    assert plan["2026-11-05"] == [11]
    assert plan["2026-11-06"] == [13]
    assert "2026-11-04" not in plan
    assert "2026-11-10" not in plan and "2026-11-09" not in plan


def test_mark_results_missing_all_venues_same_day():
    import build_race_quality as bq
    q = pd.DataFrame({"race_date": ["2026-10-14"] * 3 + ["2026-10-16"] * 2,
                      "place_code": [1, 2, 3, 1, 2],
                      "n_planned": [7, 9, 12, 7, 9], "n_results": [0, 0, 0, 0, 9],
                      "is_canceled": [False] * 5,
                      "status": ["all_canceled"] * 3 + ["all_canceled", "normal"]})
    out = bq.mark_results_missing(q)
    assert list(out["status"][:3]) == ["results_missing"] * 3
    assert out["status"][3] == "all_canceled"  # 同日に他場の結果があれば従来どおり中止扱い


def test_record_gap_idempotent(tmp_path, monkeypatch):
    import src.gaps as g
    monkeypatch.setattr(g, "DATA_DIR", tmp_path)
    assert g.record_gap("lines", "2026-10-30", "", "x") is True
    assert g.record_gap("lines", "2026-10-30", "", "x") is False
    assert len(g.load_gaps()) == 1


# ─── 台帳検査 ────────────────────────────────────────────────────────────

def test_dedupe_payouts_removes_only_exact_duplicates(tmp_path):
    import ledger_check as lc
    cols = "race_date,place_code,race_no,bet_type,bet_name,kumi_ban,payout,popularity\r\n"
    body = ("2026-01-01,11,1,SH2,x,1=2,500,1\r\n2026-01-01,11,1,SH2,x,1=2,500,1\r\n"
            "2026-01-01,11,1,SH2,x,1=2,600,1\r\n")
    (tmp_path / "payouts.csv").write_bytes((cols + body).encode("utf-8"))
    assert lc.payouts_dup_keys(tmp_path) == 2
    r = lc.dedupe_payouts(tmp_path, backup_dir=tmp_path)
    assert r["removed"] == 1 and r["after"] == 2
    assert lc.payouts_dup_keys(tmp_path) == 1  # 列が違う行は残す (キー重複は検査で見える)


# ─── daemon: 完了基準タイミング / 発走時刻更新 ──────────────────────────────

def test_timing_row_and_program_update():
    import ingest_odds_prerace_daemon as d
    race = {"race_date": "2026-11-13", "place_code": "11", "race_no": "1", "st_time": "10:00"}
    st = datetime(2026, 11, 13, 10, 0)
    sa = datetime(2026, 11, 13, 9, 59)
    res = {"status": "ok", "fetch_start": datetime(2026, 11, 13, 9, 59, 0, 100000),
           "fetch_end": datetime(2026, 11, 13, 9, 59, 3), "server_date": "x"}
    row = d.timing_row(race, 1, sa, res, st)
    assert row["done_lag_sec"] == 3.0 and row["seconds_before_done"] == 57.0
    assert d.timing_row(race, 1, sa, {"status": "fail"}, st) is None

    pend = [{"race": dict(race), "st_dt": st, "snapshot_at": sa, "offset": 1.0},
            {"race": dict(race, race_no="2", st_time="10:30"), "st_dt": datetime(2026, 11, 13, 10, 30),
             "snapshot_at": datetime(2026, 11, 13, 10, 29), "offset": 1.0},
            {"race": dict(race, race_no="3"), "st_dt": st, "snapshot_at": sa, "offset": 1.0}]
    rinfo = [{"raceNo": 1, "stTime": "10:10"}, {"raceNo": 2, "stTime": "10:30", "resultFlg": 1}]
    r = d.apply_program_update(pend, "11", "2026-11-13", rinfo)
    assert r == {"moved": 1, "dropped": 2}  # R1 繰下げ / R2 終了 / R3 は番組から消えた
    assert pend[0]["st_dt"] == datetime(2026, 11, 13, 10, 10)
    assert pend[0]["snapshot_at"] == datetime(2026, 11, 13, 10, 9)


# ─── weekly_retrain: 欠落補完 / 通知 ───────────────────────────────────────

class _Stub:
    best_iteration = 1

    def predict(self, X, num_iteration=None):
        return np.full(len(X), 0.2)


def test_regenerate_missing_races_oos_only(tmp_path, monkeypatch):
    import weekly_retrain as wr
    monkeypatch.setattr(wr, "PROBS_PATH", tmp_path / "p.csv")
    monkeypatch.setattr(wr, "PICKS_PATH", tmp_path / "k.csv")
    probs = pd.DataFrame({"race_date": ["2026-10-01"], "place_code": [11], "race_no": [1], "car_no": [1],
                          "p_win": [0.1], "p_top2": [0.1], "p_top3": [0.1], "pred_rank": [3.0]})
    probs.to_csv(wr.PROBS_PATH, index=False)
    pd.DataFrame({"race_date": ["2026-10-01"], "place_code": [11], "race_no": [1], "SH2": ["1=2"]}) \
        .to_csv(wr.PICKS_PATH, index=False)
    rows = []
    for rd, rn in [("2026-09-01", 1), ("2026-09-20", 2), ("2026-10-01", 1), ("2026-10-02", 3)]:
        for c in range(1, 4):
            rows.append({"race_date": rd, "place_code": 11, "race_no": rn, "car_no": c})
    df = pd.DataFrame(rows)
    models = {t: _Stub() for t in wr.CLF_TARGETS + [wr.REG_TARGET]}
    info = wr.regenerate_missing_races(models, df, train_end="2026-09-13",
                                       regen_start="2026-10-02", feat_cols=["car_no"])
    assert info["n_fill_races"] == 1  # 09-20 の R2 のみ (09-01 は学習期間内 = in-sample のため補わない)
    out = pd.read_csv(wr.PROBS_PATH)
    assert set(out["race_date"]) == {"2026-10-01", "2026-09-20"}


def test_attention_notice_levels():
    import weekly_retrain as wr
    assert wr.build_attention_notice("OK", "ok", {"verdict": "OK"}, True) is None
    assert wr.build_attention_notice("OK", "ok", {"verdict": "SKIP"}, True) is None
    n = wr.build_attention_notice("NG", "AUC 低下", {"verdict": "SKIP"}, False)
    assert "🔴NG" in n[0] and "[keirin]" in n[0]
    n = wr.build_attention_notice("OK", "ok", {"verdict": "ERROR", "reason": "boom"}, True)
    assert "🟡WARN" in n[0]
    n = wr.build_attention_notice("WARN", "微悪化", {"verdict": "OK"}, True)
    assert "🟡WARN" in n[0]
    n = wr.build_attention_notice("NG", "x", None, False, crash="trace")
    assert "実行失敗" in n[0] and "trace" in n[1]


def test_notify_attention_uses_mock(monkeypatch):
    import weekly_retrain as wr
    import gmail_notify
    sent = []
    monkeypatch.setattr(gmail_notify, "send_email", lambda **kw: sent.append(kw))
    wr._notify_attention(("subj", "body"))
    assert sent and sent[0]["subject"] == "subj"


def test_real_mail_is_blocked_by_conftest(capsys):
    import weekly_retrain as wr
    wr._notify_attention(("subj", "body"))  # 実送信はされず、失敗は握られて再学習を止めない
    assert "送信失敗" in capsys.readouterr().out
