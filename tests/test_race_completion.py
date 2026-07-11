"""build_race_completion.refresh の回帰テスト (2026-07-11 艦隊監査 P1)

修正内容: 結果/払戻の取込後に完了台帳 (race_completion.csv) が更新されず、
実データがあるのに missing_results のまま残って週次 drift レポートが
誤警告する問題。refresh() を新設し ingest_yesterday.py / ingest_day.py /
recover_missing_races.py から取込直後に呼ぶよう接続した。

このテストが保証すること:
1. refresh(None) が実データから status を正しく導出する
2. results/payouts 追記後の refresh(days=N) で missing_results → complete に
   実態整合する (= 監査 P1 の再発防止)
3. days 差分更新が対象期間外の既存行を保持する

実行: python -m pytest tests/ -q   (プロジェクトルートから)
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import build_race_completion as brc

D1 = (date.today() - timedelta(days=2)).isoformat()  # 完了済みの日
D2 = (date.today() - timedelta(days=1)).isoformat()  # 昨日 (結果は後から入る)
OLD = (date.today() - timedelta(days=30)).isoformat()  # days 窓の外


def _write_sources(tmp_path: Path, with_d2_results: bool) -> None:
    """最小構成のソース CSV 群を tmp DATA_DIR に書く。"""
    (tmp_path / "race_entries.csv").write_text(
        "race_date,place_code,race_no,car_no\n"
        f"{D1},11,1,1\n{D1},11,1,2\n"
        f"{D2},11,1,1\n{D2},11,1,2\n",
        encoding="utf-8")
    results = f"race_date,place_code,race_no,car_no,tyaku\n{D1},11,1,1,1\n{D1},11,1,2,2\n"
    payouts = f"race_date,place_code,race_no,bet_type,kumi_ban\n{D1},11,1,ST2,1-2\n"
    if with_d2_results:
        results += f"{D2},11,1,1,1\n{D2},11,1,2,2\n"
        payouts += f"{D2},11,1,ST2,1-2\n"
    (tmp_path / "race_results.csv").write_text(results, encoding="utf-8")
    (tmp_path / "payouts.csv").write_text(payouts, encoding="utf-8")
    (tmp_path / "race_meta.csv").write_text(
        "race_date,place_code,is_canceled\n"
        f"{D1},11,False\n{D2},11,False\n",
        encoding="utf-8")


def _status_of(df: pd.DataFrame, rd: str) -> str:
    row = df[(df["race_date"] == rd) & (df["place_code"].astype(str) == "11")]
    assert len(row) == 1, f"{rd} の行が {len(row)} 件"
    return row["status"].iloc[0]


def test_refresh_full_rebuild_derives_status(tmp_path, monkeypatch):
    """refresh(None): 実データから complete / missing_results を導出。"""
    monkeypatch.setattr(brc, "DATA_DIR", tmp_path)
    _write_sources(tmp_path, with_d2_results=False)
    out = brc.refresh(None)
    assert _status_of(out, D1) == "complete"
    assert _status_of(out, D2) == "missing_results"
    assert (tmp_path / "race_completion.csv").exists()


def test_refresh_after_ingest_reconciles_missing(tmp_path, monkeypatch):
    """監査 P1 の核心: 結果/払戻の追記後に refresh すると missing が消える。"""
    monkeypatch.setattr(brc, "DATA_DIR", tmp_path)
    _write_sources(tmp_path, with_d2_results=False)
    brc.refresh(None)   # 台帳: D2 は missing_results (取込前の状態)

    # results/payouts の取込 (ingest_yesterday 相当) 後の refresh
    _write_sources(tmp_path, with_d2_results=True)
    out = brc.refresh(days=3)
    assert _status_of(out, D2) == "complete", \
        "取込済みレースが missing_results のまま = 監査 P1 の再発"

    # ファイル側も整合していること (weekly_drift_report が読むのはこちら)
    on_disk = pd.read_csv(tmp_path / "race_completion.csv", dtype=str)
    assert _status_of(on_disk, D2) == "complete"


def test_refresh_days_keeps_rows_outside_window(tmp_path, monkeypatch):
    """days 差分更新は窓の外 (race_date < start) の既存行を保持する。"""
    monkeypatch.setattr(brc, "DATA_DIR", tmp_path)
    _write_sources(tmp_path, with_d2_results=True)
    old_row = pd.DataFrame([{
        "race_date": OLD, "place_code": "99", "race_no": "1",
        "n_entries": 7, "n_result_rows": 7, "n_tyaku": 7,
        "has_payout": 1, "status": "complete", "canceled_day": 0,
    }])
    old_row.to_csv(tmp_path / "race_completion.csv", index=False)

    out = brc.refresh(days=3)
    kept = out[(out["race_date"] == OLD) & (out["place_code"].astype(str) == "99")]
    assert len(kept) == 1, "days 窓の外の既存行が消えた"
    assert _status_of(out, D1) == "complete"
    assert _status_of(out, D2) == "complete"
