"""正当な欠損 (全返還・日全体中止) の台帳 void_races.csv (2026-11-15) の回帰テスト

(a) record_void は冪等・理由検証 / is_full_refund は取得済みレスポンスだけで判定
(b) build_race_completion は台帳のレースを欠測から除外し void_reason で数える (日全体も)
(c) ingest_day が全返還を検出したとき自動で台帳に残す / 検出できないものは残さない
(d) 本当の欠測は missing のまま残る

実行: python -m pytest tests -q -p no:cacheprovider
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import build_race_completion as brc  # noqa: E402
import ingest_day as idy  # noqa: E402
import src.storage as storage  # noqa: E402
import src.voids as voids  # noqa: E402

D = (date.today() - timedelta(days=20)).isoformat()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    for mod in (voids, storage, brc, idy):
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    return tmp_path


def test_record_void_idempotent_and_validates(data_dir):
    assert voids.record_void(D, 11, 3, "full_refund", "x") is True
    assert voids.record_void(D, "11", "3", "full_refund", "x") is False
    assert voids.record_void(D, 11, "", "day_canceled") is True
    assert len(voids.load_voids()) == 2
    with pytest.raises(ValueError):
        voids.record_void(D, 11, 4, "unknown")


def test_is_full_refund():
    ok = {"resultCd": 0, "haraiGakuSubData": {"SH2": [{"haraiGaku": "【全返還】"}]}}
    assert voids.is_full_refund(ok)
    assert not voids.is_full_refund(
        {"resultCd": 0, "haraiGakuSubData": {"SH2": [{"haraiGaku": "1,230円"}]}})
    assert not voids.is_full_refund({**ok, "resultCd": 1})


def _sources(p: Path) -> None:
    # 場 11: R1 全返還(結果なし) / R2 正常 / R3 本当の欠測。場 12: 日全体中止 (entries あり)
    (p / "race_entries.csv").write_text(
        "race_date,place_code,race_no,car_no\n"
        f"{D},11,1,1\n{D},11,2,1\n{D},11,3,1\n{D},12,1,1\n{D},12,2,1\n", encoding="utf-8")
    (p / "race_results.csv").write_text(
        f"race_date,place_code,race_no,car_no,tyaku\n{D},11,2,1,1\n", encoding="utf-8")
    (p / "payouts.csv").write_text(
        f"race_date,place_code,race_no,bet_type,kumi_ban\n{D},11,2,ST2,1-2\n", encoding="utf-8")
    (p / "race_meta.csv").write_text(
        f"race_date,place_code,is_canceled\n{D},11,False\n{D},12,False\n", encoding="utf-8")


def _st(df, pc, rn):
    r = df[(df.race_date == D) & (df.place_code.astype(str) == pc)
           & (df.race_no.astype(str) == rn)]
    return r["status"].iloc[0], r["void_reason"].iloc[0]


def test_build_excludes_voids_and_keeps_real_missing(data_dir):
    _sources(data_dir)
    before = brc.build(None)
    assert _st(before, "11", "1")[0] == "missing_results"
    assert _st(before, "12", "1")[0] == "missing_results"
    voids.record_void(D, 11, 1, "full_refund")
    voids.record_void(D, 12, "", "day_canceled")   # meta は is_canceled=False のまま → 台帳側で上書き判定
    after = brc.build(None)
    assert _st(after, "11", "1") == ("canceled", "full_refund")
    assert _st(after, "12", "1") == ("canceled", "day_canceled")
    assert _st(after, "12", "2") == ("canceled", "day_canceled")
    assert _st(after, "11", "3") == ("missing_results", "")      # 本当の欠測は残る
    assert _st(after, "11", "2") == ("complete", "")
    assert int(after["status"].str.startswith("missing").sum()) == 1


def test_refresh_days_adds_column_to_old_table(data_dir):
    _sources(data_dir)
    voids.record_void(D, 11, 1, "full_refund")
    pd.DataFrame([{"race_date": "2020-01-01", "place_code": "99", "race_no": "1", "n_entries": 7,
                   "n_result_rows": 7, "n_tyaku": 7, "has_payout": 1, "status": "complete",
                   "canceled_day": 0}]).to_csv(data_dir / "race_completion.csv", index=False)
    out = brc.refresh(days=40)
    assert "void_reason" in out.columns
    assert (out["void_reason"].fillna("") == "full_refund").sum() == 1


class _Client:
    def __init__(self, j):
        self.j = j

    def get_race_result(self, enc):
        return self.j


def _run_ingest(j) -> None:
    idy._ingest_results_and_payouts(
        _Client(j), 11, D, [{"race_no": 1, "enc_para_r": "E", "race_end": True}], None, {})


def test_ingest_records_full_refund_automatically(data_dir):
    _run_ingest({"resultCd": 0, "tyakujyunItemSubData": [],
                 "haraiGakuSubData": {"SH2HaraiGakuDispItemSubData": [{"haraiGaku": "【全返還】"}]}})
    v = voids.load_voids()
    assert len(v) == 1 and (v[0]["race_no"], v[0]["reason"]) == ("1", "full_refund")


def test_ingest_does_not_record_when_not_full_refund(data_dir):
    # 全返還の文言なし (= 取得できていないだけ) は台帳に残さない
    _run_ingest({"resultCd": 0, "tyakujyunItemSubData": [], "haraiGakuSubData": {}})
    assert voids.load_voids() == []
