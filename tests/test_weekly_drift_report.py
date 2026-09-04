"""weekly_drift_report の回帰テスト (2026-07-11 監査 P1 対応)

1. 未定義定数 HTML_TD (末尾 L/R なし) の残存ゼロをソース検査で保証
   (local-keiba tests/test_exotic_ev.py の板値復活検知と同じ発想。
    7/6 に HTML_TD_L/R へ改名した際の置換漏れが週次メール HTML 生成を
    NameError で殺し得たため、bare 参照の復活をテストで封じる)
2. HTML 生成経路 (_html_drift_health / _html_gate_a) を合成データで
   レンダリングし NameError が出ないことを保証 (メール送信はしない)
3. daily_shadow_predict.earliest_start_dt の発走時刻ガード用パーサ検証

実行: python -m pytest tests/ -q   (プロジェクトルートから)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SOURCE = PROJECT_ROOT / "weekly_drift_report.py"


# ─── 1. bare HTML_TD 残存ゼロ (ソース検査) ────────────────────────────

def test_no_bare_html_td_reference():
    """HTML_TD (末尾に _L/_R が付かない形) は未定義。参照ゼロを保証する。

    \b は単語境界。`_` は単語構成文字なので HTML_TD_L / HTML_TD_R には
    マッチせず、bare の HTML_TD のみを検出する。
    """
    text = SOURCE.read_text(encoding="utf-8")
    hits = [
        (i, line.strip())
        for i, line in enumerate(text.splitlines(), start=1)
        if re.search(r"\bHTML_TD\b", line)
    ]
    assert hits == [], (
        f"未定義の bare HTML_TD 参照が復活: {hits} — "
        "HTML_TD_L (文字セル) か HTML_TD_R (数値セル) を使うこと")


def test_defined_html_constants_exist():
    """置換先の定数が実在することを確認 (改名事故の再発防止)。"""
    import weekly_drift_report as wdr
    for name in ("HTML_TBL", "HTML_TH", "HTML_TH_R", "HTML_TD_L", "HTML_TD_R"):
        assert hasattr(wdr, name), f"{name} が weekly_drift_report に無い"


# ─── 2. HTML 生成経路のレンダリング (合成データ、NameError 検出) ──────

def _synthetic_health() -> dict:
    return {
        "wedge": [
            {"bet": "ST2", "offset": 0.5, "n_races": 210,
             "median_pct": -0.42, "gt10_pct": 3.1, "adverse_pct": 48.6},
            {"bet": "RT3", "offset": 3, "n_races": 180,
             "median_pct": -4.2, "gt10_pct": 12.0, "adverse_pct": 55.0},
        ],
        "overround": [
            {"bet": "ST2", "n_races": 210, "ov_median": 1.342},
            {"bet": "RT3", "n_races": 180, "ov_median": 1.51},
        ],
        "n_wedge_races": 215,
    }


def _synthetic_gate() -> dict:
    week_row = {
        "week": "06-15~06-21", "settled": 160, "coverage_pct": 99.1,
        "post_start": 0, "lag_p95": 4.2, "lag_null_pct": 0.0,
        "shared_pass": True, "shared_fails": [], "bet_fails": {},
    }
    week_row_fail = {
        "week": "06-22~06-28", "settled": 120, "coverage_pct": 97.0,
        "post_start": 1, "lag_p95": None, "lag_null_pct": 82.0,
        "shared_pass": False,
        "shared_fails": ["settled 120<150", "lag未計測 (null 82%>50%)"],
        "bet_fails": {"RT3": ["|med|4.2%>1%"]},
    }
    return {
        "weeks": ["2026-06-15~2026-06-21", "2026-06-22~2026-06-28"],
        "missing_14d": 0,
        "offsets": {
            0.5: {"weeks": [week_row, week_row_fail],
                  "shared_pass": False,
                  "bet_verdicts": {"SH2": True, "ST2": True,
                                   "RH3": False, "RT3": False}},
        },
    }


def test_html_drift_health_renders():
    """959 行を含む経路: wedge 表の券種セル (HTML_TD_L) を実際に評価する。"""
    import weekly_drift_report as wdr
    html = "\n".join(wdr._html_drift_health(
        _synthetic_health(),
        [{"race_date": "2026-07-01", "place_code": "42", "race_no": "1",
          "status": "missing_results"}]))
    assert "ST2" in html and "text-align:left" in html
    assert "HTML_TD" not in html  # f-string 未展開の取りこぼし検知


def test_html_gate_a_renders():
    """1004/1011 行を含む経路: gate A マトリクスのセルを実際に評価する。"""
    import weekly_drift_report as wdr
    html = "\n".join(wdr._html_gate_a(_synthetic_gate()))
    # 2026-08-10 i18n: PASS/FAIL → 合格/不合格 (券種セル) と ✅/❌ (共通条件)
    assert "合格" in html and "不合格" in html
    assert "❌" in html  # 合成データは 0.5 分前のみ・共通条件 fail なので ❌ だけ出る
    assert "HTML_TD" not in html


def test_md_gate_a_renders_with_lag_null_pct():
    """markdown 側も lag null% 列 (表頭「遅延未計測%」) 込みでレンダリングできる。"""
    import weekly_drift_report as wdr
    md = "\n".join(wdr._md_gate_a(_synthetic_gate()))
    assert "遅延未計測%" in md  # 2026-08-10 i18n 後の表頭
    assert "82.0" in md  # fail 週の null 率が表に出る


# ─── 3. daily_shadow_predict の発走時刻ガード ─────────────────────────

def test_earliest_start_dt_parses_min_time(tmp_path, monkeypatch):
    import daily_shadow_predict as dsp
    csv_path = tmp_path / "race_entries.csv"
    csv_path.write_text(
        "race_date,st_time\n"
        "2026-07-11,15:30\n"
        "2026-07-11,10:45:00\n"
        "2026-07-11,\n"          # 空は無視
        "2026-07-11,bogus\n"      # 壊れた値は無視
        "2026-07-10,09:00\n",     # 他日は対象外
        encoding="utf-8")
    monkeypatch.setattr(dsp, "DATA_DIR", tmp_path)
    dt = dsp.earliest_start_dt("2026-07-11")
    assert dt is not None
    assert (dt.hour, dt.minute, dt.second) == (10, 45, 0)
    assert dsp.earliest_start_dt("2026-07-09") is None


def test_earliest_start_dt_missing_file(tmp_path, monkeypatch):
    import daily_shadow_predict as dsp
    monkeypatch.setattr(dsp, "DATA_DIR", tmp_path / "nope")
    assert dsp.earliest_start_dt("2026-07-11") is None
