"""直前オッズ月次パーティション (src/odds_prerace.py, scripts/partition_odds_prerace.py) の回帰テスト

(a) 月別ファイル + 旧単一ファイル混在の読み込みで dedup 結果が単一ファイル時と一致
(b) 確定月キャッシュ利用時と非利用時で一致 (+ 元ファイル変化でキャッシュ無効化)
(c) 移行スクリプトが行数を保存し冪等 (dry-run は無書き込み、既存月次へは追記)
(d) デーモンの追記先が月別ファイルになる (旧単一ファイルには書かない)

実行: python -m pytest tests -q -p no:cacheprovider
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import odds_prerace as op  # noqa: E402
from src.storage import CSV_SCHEMAS, PRERACE_LEGACY_NAME  # noqa: E402

COLS = CSV_SCHEMAS[PRERACE_LEGACY_NAME]
BETS = ["SH2", "ST2"]
OFFSETS = [5, 3, 2, 1, 0.5]


# ─── 合成データ ────────────────────────────────────────────────────────────

def _rows_for_day(race_date: str, place: int, n_races: int = 2) -> list[dict]:
    """1 日分: n_races × 券種 2 × 組 3 × offset 5 の snapshot 行 (odds は offset ごとに変える)。"""
    rows = []
    for rn in range(1, n_races + 1):
        for bt in BETS:
            for k in range(1, 4):
                kumi = f"{k}={k + 1}" if bt == "SH2" else f"{k}-{k + 1}"
                for off in OFFSETS:
                    # 発走 10:00 固定、offset 分前の snapshot
                    mins = int(off) if float(off).is_integer() else 0
                    sec = 0 if float(off).is_integer() else 30
                    snap = f"{race_date} 09:{60 - int(off) - (1 if sec else 0):02d}:{sec:02d}"
                    rows.append({
                        "race_date": race_date, "place_code": place, "race_no": rn,
                        "bet_type": bt, "kumi_ban": kumi,
                        "odds": round(10 + k + off * 0.1 + rn, 1), "max_odds": "",
                        "popularity": k, "official_dt": "",
                        "snapshot_dt": snap, "minutes_before_start": mins,
                        "target_offset_min": int(off) if float(off).is_integer() else off,
                    })
    # odds=0 (未発売) の行を 1 つ混ぜる → dedup で落ちるべき
    rows.append({**rows[0], "odds": 0, "snapshot_dt": f"{race_date} 09:59:59",
                 "target_offset_min": 0.5})
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def _all_rows() -> dict[str, list[dict]]:
    """月 → 行 (2026-05 .. 2026-09、5 か月)。"""
    days = {
        "2026-05": ["2026-05-16", "2026-05-31"],
        "2026-06": ["2026-06-01", "2026-06-15"],
        "2026-07": ["2026-07-20"],
        "2026-08": ["2026-08-31"],
        "2026-09": ["2026-09-01", "2026-09-04"],
    }
    out: dict[str, list[dict]] = {}
    for mon, ds in days.items():
        out[mon] = []
        for i, d in enumerate(ds):
            out[mon] += _rows_for_day(d, place=30 + i)
    return out


def _reference_dedup(csv_path: Path, start=None, end=None) -> pd.DataFrame:
    """ev_prerace_pipeline.load_prerace_dedup の旧実装 (単一ファイル) をそのまま再現。"""
    df = pd.read_csv(
        csv_path,
        usecols=["race_date", "place_code", "race_no", "bet_type",
                 "kumi_ban", "odds", "snapshot_dt", "target_offset_min"],
        dtype={"race_date": str, "place_code": str, "race_no": str,
               "bet_type": str, "kumi_ban": str},
        low_memory=False,
    )
    if start:
        df = df[df["race_date"] >= start]
    if end:
        df = df[df["race_date"] <= end]
    df["odds"] = pd.to_numeric(df["odds"], errors="coerce")
    df = df[df["odds"] > 0]
    return df.sort_values("snapshot_dt").drop_duplicates(
        subset=op.JOIN_KEYS, keep="last")


def _canon(df: pd.DataFrame) -> pd.DataFrame:
    """比較用に並び・index・数値型を正規化。"""
    df = df[op.DEDUP_COLS].copy()
    df["odds"] = df["odds"].astype(float)
    df["target_offset_min"] = pd.to_numeric(df["target_offset_min"]).astype(float)
    for c in op.JOIN_KEYS + ["snapshot_dt"]:
        df[c] = df[c].astype(str)
    return df.sort_values(op.JOIN_KEYS).reset_index(drop=True)


@pytest.fixture
def mixed_dir(tmp_path: Path) -> tuple[Path, Path]:
    """月別 (07, 08, 09) + 旧単一ファイル (05, 06 + 07 の一部) の混在ディレクトリと、
    同じ全行を 1 ファイルにした参照 CSV を返す。"""
    data = tmp_path / "data"
    data.mkdir()
    rows = _all_rows()
    # 旧ファイル: 05, 06 全部 + 07 の前半 (同じ月が旧と月次に跨る状況)
    legacy_rows = rows["2026-05"] + rows["2026-06"] + rows["2026-07"][: len(rows["2026-07"]) // 2]
    _write_csv(data / PRERACE_LEGACY_NAME, legacy_rows)
    _write_csv(data / "race_odds_prerace_2026-07.csv", rows["2026-07"][len(rows["2026-07"]) // 2:])
    _write_csv(data / "race_odds_prerace_2026-08.csv", rows["2026-08"])
    _write_csv(data / "race_odds_prerace_2026-09.csv", rows["2026-09"])
    ref = tmp_path / "reference_single.csv"
    _write_csv(ref, sum(rows.values(), []))
    return data, ref


# ─── 命名 ──────────────────────────────────────────────────────────────────

def test_monthly_name_and_parse():
    assert op.monthly_name("2026-09-04") == "race_odds_prerace_2026-09.csv"
    assert op.month_from_name("race_odds_prerace_2026-09.csv") == "2026-09"
    assert op.month_from_name("race_odds_prerace.csv") is None
    assert op.month_from_name("race_odds_prerace_2026-09.csv.bak-x") is None
    with pytest.raises(ValueError):
        op.monthly_name("bad")


def test_list_partition_files_range(mixed_dir):
    data, _ = mixed_dir
    files = [p.name for p in op.list_partition_files(data, "2026-08-01", "2026-09-30")]
    # 期間内の月次 (昇順) + 旧ファイルは常に末尾
    assert files == ["race_odds_prerace_2026-08.csv", "race_odds_prerace_2026-09.csv",
                     PRERACE_LEGACY_NAME]
    files = [p.name for p in op.list_partition_files(data, "2026-08-01", "2026-09-30",
                                                     include_legacy=False)]
    assert files == ["race_odds_prerace_2026-08.csv", "race_odds_prerace_2026-09.csv"]


# ─── (a) 混在読み込みの dedup == 単一ファイル ──────────────────────────────

@pytest.mark.parametrize("start,end", [(None, None), ("2026-06-10", "2026-08-31"),
                                       ("2026-07-01", "2026-07-31"), ("2027-01-01", None)])
def test_dedup_matches_single_file(mixed_dir, start, end):
    data, ref = mixed_dir
    got, n_raw = op.load_prerace_dedup(start, end, data_dir=data, use_cache=False)
    exp = _reference_dedup(ref, start, end)
    pd.testing.assert_frame_equal(_canon(got), _canon(exp))
    if start is None:
        # odds=0 行は dedup 前行数から除外されない (旧実装は odds>0 後に数える) ため
        # n_raw は「読んだ生行数」として odds=0 込みで数える
        assert n_raw == sum(len(v) for v in _all_rows().values())
    # 最遅 snapshot (0.5 分前) が残る
    if len(got):
        assert set(pd.to_numeric(got["target_offset_min"]).unique()) == {0.5}


def test_read_prerace_concat_and_filter(mixed_dir):
    data, ref = mixed_dir
    cols = op.JOIN_KEYS + ["odds", "target_offset_min"]
    got = op.read_prerace(usecols=cols, dtype={k: str for k in op.JOIN_KEYS},
                          start="2026-07-01", end="2026-07-31", data_dir=data)
    exp = pd.read_csv(ref, usecols=cols, dtype={k: str for k in op.JOIN_KEYS})
    exp = exp[(exp["race_date"] >= "2026-07-01") & (exp["race_date"] <= "2026-07-31")]
    assert len(got) == len(exp) == len(_all_rows()["2026-07"])
    assert list(got.columns) == cols
    # 何も無い期間は空 DataFrame (列は保持)
    empty = op.read_prerace(usecols=cols, start="2030-01-01", data_dir=data)
    assert len(empty) == 0 and list(empty.columns) == cols


# ─── (b) キャッシュ利用/非利用の一致 ──────────────────────────────────────

def test_cache_equivalence_and_invalidation(mixed_dir):
    data, ref = mixed_dir
    today = date(2026, 9, 15)  # 05..08 が確定月、09 が当月
    nocache, _ = op.load_prerace_dedup(None, None, data_dir=data, use_cache=False, today=today)
    first, _ = op.load_prerace_dedup(None, None, data_dir=data, use_cache=True, today=today)
    cache_dir = data / op.CACHE_DIRNAME
    cached_months = sorted(p.name for p in cache_dir.glob("*.parquet"))
    assert cached_months == [f"odds_prerace_dedup_2026-0{m}.parquet" for m in (5, 6, 7, 8)]
    second, n_raw2 = op.load_prerace_dedup(None, None, data_dir=data, use_cache=True, today=today)
    pd.testing.assert_frame_equal(_canon(first), _canon(nocache))
    pd.testing.assert_frame_equal(_canon(second), _canon(nocache))
    assert n_raw2 == sum(len(v) for v in _all_rows().values())

    # 期間指定でもキャッシュ経由の結果は参照と一致
    got, _ = op.load_prerace_dedup("2026-06-10", "2026-07-31", data_dir=data,
                                   use_cache=True, today=today)
    pd.testing.assert_frame_equal(_canon(got), _canon(_reference_dedup(ref, "2026-06-10", "2026-07-31")))

    # 確定月 (08) のファイルにより新しい snapshot を追記 → フィンガープリント不一致で再計算
    extra = {**_rows_for_day("2026-08-31", place=30)[0],
             "odds": 99.9, "snapshot_dt": "2026-08-31 09:59:59", "target_offset_min": 0.5}
    with open(data / "race_odds_prerace_2026-08.csv", "a", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=COLS).writerow(extra)
    third, _ = op.load_prerace_dedup(None, None, data_dir=data, use_cache=True, today=today)
    key = (third["race_date"] == "2026-08-31") & (third["place_code"] == "30") \
        & (third["race_no"] == "1") & (third["bet_type"] == "SH2") & (third["kumi_ban"] == "1=2")
    assert float(third.loc[key, "odds"].iloc[0]) == 99.9
    fresh, _ = op.load_prerace_dedup(None, None, data_dir=data, use_cache=False, today=today)
    pd.testing.assert_frame_equal(_canon(third), _canon(fresh))


# ─── (c) 移行スクリプト: 行数保存・冪等・dry-run 無書き込み ────────────────

def _load_partition_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "partition_odds_prerace", PROJECT_ROOT / "scripts" / "partition_odds_prerace.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _data_lines(p: Path) -> int:
    with open(p, encoding="utf-8", newline="") as f:
        return sum(1 for _ in f) - 1


def test_partition_script_preserves_rows_and_is_idempotent(tmp_path: Path):
    mod = _load_partition_module()
    data = tmp_path / "data"
    data.mkdir()
    rows = _all_rows()
    legacy_rows = sum(rows.values(), [])
    _write_csv(data / PRERACE_LEGACY_NAME, legacy_rows)
    # 既存の月次 (09) がある状態 → 追記される
    pre_existing = _rows_for_day("2026-09-05", place=50)
    _write_csv(data / "race_odds_prerace_2026-09.csv", pre_existing)
    logs: list[str] = []

    # dry-run: 何も変わらない
    s = mod.partition(data, dry_run=True, log=logs.append)
    assert s["status"] == "dry_run"
    assert s["n_src_rows"] == len(legacy_rows)
    assert (data / PRERACE_LEGACY_NAME).exists()
    assert sorted(p.name for p in data.glob("race_odds_prerace_*.csv")) == ["race_odds_prerace_2026-09.csv"]
    assert not list(data.glob("*.bak-*"))

    # 実行
    s = mod.partition(data, dry_run=False, log=logs.append)
    assert s["status"] == "done", logs
    assert not (data / PRERACE_LEGACY_NAME).exists()
    baks = list(data.glob("race_odds_prerace.csv.bak-*"))
    assert len(baks) == 2 and any(b.name.endswith(".migrated.json") for b in baks)
    bak = [b for b in baks if not b.name.endswith(".json")][0]
    assert _data_lines(bak) == len(legacy_rows)  # 元は残す
    monthly = sorted(p.name for p in data.glob("race_odds_prerace_*.csv"))
    assert monthly == [f"race_odds_prerace_{m}.csv" for m in rows]
    for mon, r in rows.items():
        expect = len(r) + (len(pre_existing) if mon == "2026-09" else 0)
        assert _data_lines(data / f"race_odds_prerace_{mon}.csv") == expect
    assert s["n_written"] == len(legacy_rows) and s["invalid"] == 0
    assert not list(data.glob("*.part"))
    # 内容も同一 (行順は月内で保存される)
    total = pd.concat([pd.read_csv(data / f"race_odds_prerace_{m}.csv", dtype=str) for m in rows])
    assert len(total) == len(legacy_rows) + len(pre_existing)

    # 冪等: 再実行は no-op
    s2 = mod.partition(data, dry_run=False, log=logs.append)
    assert s2["status"] == "nothing_to_do"
    for mon, r in rows.items():
        expect = len(r) + (len(pre_existing) if mon == "2026-09" else 0)
        assert _data_lines(data / f"race_odds_prerace_{mon}.csv") == expect

    # 旧ファイルが再生成された (旧コードのデーモンが追記した) ケース → 再実行で吸収
    late = _rows_for_day("2026-09-06", place=60)
    _write_csv(data / PRERACE_LEGACY_NAME, late)
    s3 = mod.partition(data, dry_run=False, log=logs.append)
    assert s3["status"] == "done"
    assert _data_lines(data / "race_odds_prerace_2026-09.csv") == \
        len(rows["2026-09"]) + len(pre_existing) + len(late)

    # 移行後の読み手 = 移行前の単一ファイル (bak) と dedup 一致
    got, _ = op.load_prerace_dedup(None, "2026-09-04", data_dir=data, use_cache=False)
    pd.testing.assert_frame_equal(_canon(got), _canon(_reference_dedup(bak, None, "2026-09-04")))


def test_partition_script_quarantines_invalid_rows(tmp_path: Path):
    mod = _load_partition_module()
    data = tmp_path / "data"
    data.mkdir()
    rows = _rows_for_day("2026-08-01", place=30)
    _write_csv(data / PRERACE_LEGACY_NAME, rows)
    with open(data / PRERACE_LEGACY_NAME, "a", encoding="utf-8", newline="") as f:
        f.write("garbage,line,without,date\r\n")
    s = mod.partition(data, dry_run=False, log=lambda *_: None)
    assert s["status"] == "done" and s["invalid"] == 1
    assert _data_lines(data / "race_odds_prerace_2026-08.csv") == len(rows)
    inv = list(data.glob("race_odds_prerace_invalid-*.csv"))
    assert len(inv) == 1 and _data_lines(inv[0]) == 1


# ─── (d) デーモンの追記先が月別 ───────────────────────────────────────────

def test_storage_schema_for_monthly_name():
    from src import storage
    assert storage.schema_for("race_odds_prerace_2026-09.csv") == COLS
    assert storage.schema_for(PRERACE_LEGACY_NAME) == COLS
    with pytest.raises(ValueError):
        storage.schema_for("race_odds_prerace_2026-9.csv")


def test_daemon_appends_to_monthly_file(tmp_path: Path, monkeypatch):
    from src import storage
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(storage, "DATA_DIR", data)

    import ingest_odds_prerace_daemon as daemon
    # import 時の basicConfig が logs/ に FileHandler を付けるので、テスト中は黙らせる
    quiet = logging.getLogger("test_daemon_quiet")
    quiet.propagate = False
    quiet.addHandler(logging.NullHandler())
    monkeypatch.setattr(daemon, "logger", quiet)

    class FakeClient:
        @staticmethod
        def make_race_id(race_date, place_code, race_no):
            return f"{race_date}{place_code}{race_no}"

        def get_odds(self, race_id):
            return {"official_dt": ""}

    def fake_parse(place_code, race_date, race_no, odds_data):
        return [{"race_date": race_date, "place_code": place_code, "race_no": race_no,
                 "bet_type": "SH2", "kumi_ban": "1=2", "odds": 7.5, "max_odds": "",
                 "popularity": 1, "official_dt": ""}]

    monkeypatch.setattr(daemon, "parse_odds_lists", fake_parse)
    monkeypatch.setattr(daemon.NetkeirinClient, "make_race_id", FakeClient.make_race_id)

    from datetime import datetime
    res = daemon.capture_race(FakeClient(), "2026-09-04", 84, 1,
                              datetime(2026, 9, 4, 16, 5, 30), 0, 0.5)
    assert res["status"] == "ok" and res["n_rows"] == 1
    res = daemon.capture_race(FakeClient(), "2026-10-01", 84, 2,
                              datetime(2026, 10, 1, 10, 0, 0), 5, 5)
    assert res["status"] == "ok"

    assert not (data / PRERACE_LEGACY_NAME).exists()
    assert sorted(p.name for p in data.glob("race_odds_prerace_*.csv")) == \
        ["race_odds_prerace_2026-09.csv", "race_odds_prerace_2026-10.csv"]
    with open(data / "race_odds_prerace_2026-09.csv", encoding="utf-8", newline="") as f:
        got = list(csv.DictReader(f))
    assert got[0]["snapshot_dt"] == "2026-09-04 16:05:30"
    assert got[0]["target_offset_min"] == "0.5"
    assert list(got[0].keys()) == COLS
    # 月次ファイルはヘッダ 1 行 + データ (2 回目の追記でヘッダが重複しない)
    daemon.capture_race(FakeClient(), "2026-09-04", 84, 3,
                        datetime(2026, 9, 4, 16, 6, 0), 0, 0.5)
    with open(data / "race_odds_prerace_2026-09.csv", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 3 and lines[0].startswith("race_date,")
