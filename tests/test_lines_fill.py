"""並び (race_lines.csv) 欠損対策 (2026-11-15) の回帰テスト

(a) 取り直し計画: 並びが無い場だけ・未発走だけ・過ぎた時刻は「今」へ寄せる・最後に last
(b) fill_venue_day: 未取得レースだけ追記・取得済みは再取得しない・二重行を作らない
(c) run_line_checks: 揃った場は何もしない / 例外は握って次へ / 最後でも未掲載なら欠測台帳へ
(d) ワーカー起動は絶対に例外を投げない (オッズ取得ループを止めない) + デーモンに配線されている
(e) 取り込みの終了コード: errors>0・backfill 失敗/クラッシュ・台帳更新失敗で非ゼロ、想定内の空は 0
    + cron bat が Python の終了コードをそのまま返す (CRLF・exit /b %ERRORLEVEL%)
(f) 並び欠損 R 数/率 (lines_gap_summary) と週次レポートの 1 行

実行: python -m pytest tests -q -p no:cacheprovider
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import build_race_completion as brc  # noqa: E402
import src.gaps as gaps  # noqa: E402
import src.lines_fill as lf  # noqa: E402
import src.storage as storage  # noqa: E402

D = "2026-11-15"
NOW = datetime(2026, 11, 15, 8, 0)


def _races(pc: str, first: str, n: int = 3, step: int = 20, d: str = D) -> list[dict]:
    h, m = map(int, first.split(":"))
    base = datetime(2026, 11, 15, h, m)
    return [{"race_date": d, "place_code": pc, "race_no": str(i + 1),
             "st_time": (base + timedelta(minutes=step * i)).strftime("%H:%M")}
            for i in range(n)]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    for mod in (lf, storage, gaps, brc):
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    return tmp_path


def _write_lines(path: Path, keys: list[tuple[str, str]], d: str = D) -> None:
    with open(path / "race_lines.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["race_date", "place_code", "race_no", "car_no", "narabi_x", "narabi_y"])
        for pc, rn in keys:
            for car in (1, 2):
                w.writerow([d, pc, rn, car, 1, 1])


def _lines_rows(path: Path) -> list[dict]:
    with open(path / "race_lines.csv", "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ─── (a) 計画 ────────────────────────────────────────────────────────────

def test_plan_only_missing_venues_and_leads():
    races = _races("42", "20:50") + _races("27", "10:00")
    have = {("27", "1"), ("27", "2"), ("27", "3")}   # 27 は揃っている
    plan = lf.plan_line_checks(races, have, NOW)
    assert {p["place_code"] for p in plan} == {"42"}
    dues = [p["due"] for p in plan]
    assert dues == sorted(dues)
    first = datetime(2026, 11, 15, 20, 50)
    assert dues == [first - timedelta(minutes=m) for m in (120, 60, 30, 10)]
    assert [p["last"] for p in plan] == [False, False, False, True]


def test_plan_past_leads_collapse_to_now_and_skip_started():
    # 発走 08:30: 120/60/30 分前はすべて過去 → now に 1 回 + 10 分前 (08:20 は now=08:00 より後)
    plan = lf.plan_line_checks(_races("86", "08:30"), set(), NOW)
    assert [p["due"] for p in plan] == [NOW, datetime(2026, 11, 15, 8, 20)]
    # 全レース発走済みなら計画しない (事後には取れない)
    late = datetime(2026, 11, 15, 23, 59)
    assert lf.plan_line_checks(_races("86", "08:30"), set(), late) == []


def test_plan_uses_first_upcoming_missing_race():
    # R1 は発走済み (並び無し) でも、未発走の最初のレース (R2) を基準にする
    races = _races("34", "07:50", n=3, step=20)   # 07:50 / 08:10 / 08:30
    plan = lf.plan_line_checks(races, set(), NOW)
    assert plan[-1]["due"] == datetime(2026, 11, 15, 8, 0)   # R2 08:10 の 10 分前
    assert plan[0]["due"] == NOW


# ─── (b) 取得・重複排除 ───────────────────────────────────────────────────

def _html(key: str, obj: dict) -> str:
    return f"<html><script>jsonData['{key}'] = {json.dumps(obj)};</script></html>"


class FakeClient:
    """PC0201 / PJ0305 を返すだけの偽クライアント (呼び出し回数を数える)。"""

    def __init__(self, posted_races: list[int], n_cars: int = 3):
        self.posted = posted_races
        self.n_cars = n_cars
        self.program_calls = 0
        self.racelist_calls = 0

    def get_raceprogram_html(self, place_code, race_date):
        self.program_calls += 1
        return _html("PC0201", {"C0201data": {"joName": "x", "cntRace": 3,
                                              "C0201race": [{"encParaR": "ENC"}] * 3}})

    def get_racelist_html(self, enc):
        self.racelist_calls += 1
        rinfo = []
        for rn in (1, 2, 3):
            n = ([{"syaban": c, "narabiX": 1, "narabiY": c} for c in range(1, self.n_cars + 1)]
                 if rn in self.posted else [])
            rinfo.append({"raceNo": rn, "stTime": "20:50", "narabiFlg": 1 if n else 0, "nInfo": n})
        return _html("PJ0305", {"rInfo": rinfo})


def test_fill_appends_only_missing_races_and_no_duplicates(data_dir):
    _write_lines(data_dir, [("42", "1")])          # R1 は取得済み
    cl = FakeClient([1, 2, 3])
    r = lf.fill_venue_day(cl, D, 42)
    assert r["status"] == "ok" and r["races"] == [2, 3] and r["rows"] == 6
    rows = _lines_rows(data_dir)
    keys = [(x["place_code"], x["race_no"], x["car_no"]) for x in rows]
    assert len(keys) == len(set(keys)), "二重行ができた"
    assert {(x["place_code"], x["race_no"]) for x in rows} == {("42", "1"), ("42", "2"), ("42", "3")}
    # 2 回目は何も足さない
    r2 = lf.fill_venue_day(cl, D, 42)
    assert r2["rows"] == 0 and len(_lines_rows(data_dir)) == len(rows)


def test_fill_not_posted_returns_empty_without_writing(data_dir):
    r = lf.fill_venue_day(FakeClient([]), D, 42)
    assert r["status"] == "empty" and r["rows"] == 0
    assert not (data_dir / "race_lines.csv").exists()


# ─── (c) ワーカー本体 ─────────────────────────────────────────────────────

class Clock:
    def __init__(self, t: datetime):
        self.t = t
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s: float):
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)


def test_run_line_checks_fills_then_skips_later_checks(data_dir):
    races = _races("42", "20:50")
    plan = lf.plan_line_checks(races, set(), NOW)
    cl = FakeClient([1, 2, 3])
    ck = Clock(NOW)
    out = lf.run_line_checks(plan, races,
                             fill=lambda d, p: lf.fill_venue_day(cl, d, p, {}),
                             now_fn=ck.now, sleep_fn=ck.sleep)
    assert out["filled"] == [("42", 3)]
    assert out["skipped"] == 3                   # 2 回目以降 (60/30/10 分前) は取りに行かない
    assert cl.racelist_calls == 1
    assert sum(1 for _ in _lines_rows(data_dir)) == 9


def test_run_line_checks_survives_errors_and_records_gap_on_last(data_dir):
    races = _races("42", "20:50")
    plan = lf.plan_line_checks(races, set(), NOW)
    calls = []

    def boom(d, p):
        calls.append(p)
        raise RuntimeError("network down")

    recorded = []
    ck = Clock(NOW)
    out = lf.run_line_checks(plan, races, fill=boom,
                             record_gap=lambda *a: recorded.append(a),
                             now_fn=ck.now, sleep_fn=ck.sleep)
    assert len(calls) == 4 and out["errors"] == 4   # 例外でも全部試す (握って継続)
    # 例外のときは「未掲載で最後まで取れなかった」とは断定せず台帳には書かない
    assert recorded == []

    # 例外ではなく「未掲載」で最後まで取れなかった場合だけ台帳へ
    out2 = lf.run_line_checks(plan, races, fill=lambda d, p: {"status": "empty", "rows": 0, "races": []},
                              record_gap=lambda *a: recorded.append(a),
                              now_fn=ck.now, sleep_fn=ck.sleep)
    assert out2["gave_up"] == ["42"]
    assert len(recorded) == 1 and recorded[0][:3] == ("lines", D, "42")


def test_run_line_checks_skips_when_already_complete(data_dir):
    races = _races("42", "20:50")
    _write_lines(data_dir, [("42", "1"), ("42", "2"), ("42", "3")])
    plan = lf.plan_line_checks(races, set(), NOW)   # 古い (空の) have で計画しても
    called = []
    out = lf.run_line_checks(plan, races, fill=lambda d, p: called.append(p) or {},
                             now_fn=lambda: NOW, sleep_fn=lambda s: None)
    assert called == [] and out["skipped"] == len(plan)   # 直前に読み直して揃っていれば何もしない


def test_gap_record_is_idempotent_with_real_gaps(data_dir):
    races = _races("42", "20:50")
    plan = lf.plan_line_checks(races, set(), NOW)
    empty = lambda d, p: {"status": "empty", "rows": 0, "races": []}  # noqa: E731
    for _ in range(2):
        lf.run_line_checks(plan, races, fill=empty, record_gap=gaps.record_gap,
                           now_fn=lambda: NOW, sleep_fn=lambda s: None)
    g = [x for x in gaps.load_gaps() if x["kind"] == "lines"]
    assert len(g) == 1 and g[0]["place_code"] == "42"


# ─── (d) ワーカー起動の安全性 / デーモン配線 ───────────────────────────────

def test_start_lines_worker_never_raises_and_returns_none_when_nothing_to_do(data_dir):
    assert lf.start_lines_worker([]) is None
    assert lf.start_lines_worker([{"bad": 1}]) is None      # 壊れた入力でも例外にしない
    # 全レース並び取得済み → 何もしない
    races = _races("42", "20:50")
    _write_lines(data_dir, [("42", "1"), ("42", "2"), ("42", "3")])
    assert lf.start_lines_worker(races, now=NOW) is None


def test_worker_thread_swallows_client_failure(data_dir, monkeypatch):
    import src.client as client_mod

    def bad(*a, **k):
        raise RuntimeError("client init failed")

    monkeypatch.setattr(client_mod, "KeirinClient", bad)
    races = _races("42", "20:50")
    # 計画上の最初の確認が「今」になるよう、発走を直近にする
    t = lf.start_lines_worker(races, now=datetime(2026, 11, 15, 20, 45))
    assert t is not None and t.daemon is True
    t.join(timeout=10)
    assert not t.is_alive()        # 例外で落ちず、握って終了した


def test_daemon_wires_lines_worker_before_odds_loop():
    src = (PROJECT_ROOT / "ingest_odds_prerace_daemon.py").read_text(encoding="utf-8")
    assert "from src.lines_fill import start_lines_worker" in src
    assert src.index("start_lines_worker(races)") < src.index("while pending:")
    # pythonw 配下で壊れる stdout 直書きを足していない
    lf_src = (PROJECT_ROOT / "src" / "lines_fill.py").read_text(encoding="utf-8")
    body = lf_src.split('"""', 2)[2]          # モジュール docstring を除いたコード部分
    assert "print(" not in body and "sys.stdout" not in body and "reconfigure" not in body


# ─── (e) 終了コード ───────────────────────────────────────────────────────

def test_exit_code_yesterday():
    import ingest_yesterday as iy
    ok = {"completed": 5, "no_race": 30, "skipped": 8, "errors": 0}
    assert iy.exit_code(ok) == 0                        # 開催無し・スキップは失敗ではない
    assert iy.exit_code({**ok, "errors": 1}) == 1
    assert iy.exit_code(ok, backfill_failures=1) == 1
    assert iy.exit_code(ok, backfill_crashed=True) == 1
    assert iy.exit_code(ok, refresh_failed=True) == 1


def test_exit_code_today_lines_ignores_expected_empty():
    import ingest_today_lines as it
    assert it.exit_code({"completed": 10, "no_race": 30, "skipped": 0, "errors": 0}) == 0
    assert it.exit_code({"completed": 10, "no_race": 30, "skipped": 0, "errors": 2}) == 1


def test_run_backfill_collects_failures(monkeypatch):
    import ingest_yesterday as iy
    monkeypatch.setattr(iy, "VENUE_CODES", {11: "a", 12: "b"})
    monkeypatch.setattr(iy, "DATA_DIR", PROJECT_ROOT)  # 下で pandas を差し替える
    monkeypatch.setattr(pd, "read_csv", lambda *a, **k: pd.DataFrame({"race_date": ["2026-11-01"]}))
    monkeypatch.setattr(iy, "plan_backfill", lambda *a, **k: {"2026-11-05": [11, 12]})

    def fake_ingest(client, pc, d, index=None):
        if pc == 12:
            raise RuntimeError("x")
        return {}

    monkeypatch.setattr(iy, "ingest_one_day", fake_ingest)
    fails: list = []

    class C:
        def reset_session(self):
            pass

    n = iy.run_backfill(C(), object(), fails)
    assert n == 2 and fails == [("2026-11-05", 12)]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe 専用")
@pytest.mark.parametrize("name,script", [("cron_lines.bat", "ingest_today_lines.py"),
                                         ("cron_results.bat", "ingest_yesterday.py")])
def test_cron_bat_propagates_python_exit_code(tmp_path, name, script):
    raw = (PROJECT_ROOT / "scripts" / name).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "CRLF でない"
    text = raw.decode("ascii")
    lines = [ln for ln in text.split("\r\n") if ln]
    i = next(k for k, ln in enumerate(lines) if script in ln)
    assert lines[i + 1].strip() == "exit /b %ERRORLEVEL%", "python の直後で ERRORLEVEL を返していない"
    # 実際に cmd で動かして、python の代わりに exit 7 するスタブでも 7 が返ることを確認
    stub = text.replace("cd /d C:\\Users\\no28a\\Claude-project\\keirin-ai", "cd /d " + str(tmp_path))
    stub = stub.replace("C:\\Python313\\python.exe " + script, "cmd /c exit 7")
    (tmp_path / "logs").mkdir()
    bat = tmp_path / "stub.bat"
    bat.write_bytes(stub.encode("ascii"))
    r = subprocess.run(["cmd", "/c", str(bat)], capture_output=True, timeout=60)
    assert r.returncode == 7


# ─── (f) 並び欠損の可視化 ─────────────────────────────────────────────────

def test_lines_gap_summary_counts_races_and_bands(data_dir):
    d1, d2 = "2026-11-10", "2026-11-11"
    ent = ["race_date,place_code,race_no,car_no,st_time"]
    for d in (d1, d2):
        for rn in (1, 2):
            ent.append(f"{d},42,{rn},1,{20 + rn}:00")      # ミッドナイト場
            ent.append(f"{d},27,{rn},1,1{rn}:00")          # 昼の場
    (data_dir / "race_entries.csv").write_text("\n".join(ent) + "\n", encoding="utf-8")
    with open(data_dir / "race_lines.csv", "w", encoding="utf-8", newline="") as f:
        f.write("race_date,place_code,race_no,car_no,narabi_x,narabi_y\n")
        for d in (d1, d2):
            for rn in (1, 2):
                f.write(f"{d},27,{rn},1,1,1\n")            # 昼は全部ある。42 は無い
    g = brc.lines_gap_summary(start=d1, end=d2)
    assert (g["races"], g["missing_races"]) == (8, 4)
    assert g["rate"] == 0.5 and (g["venue_days"], g["missing_venue_days"]) == (4, 2)
    assert g["by_band"]["midnight"] == {"races": 4, "missing": 4}
    assert g["by_band"]["day"] == {"races": 4, "missing": 0}


def test_weekly_report_line_marks_warning_over_5pct():
    import weekly_drift_report as w
    g = {"start": "2026-11-01", "end": "2026-11-14", "races": 100, "missing_races": 16, "rate": 0.16,
         "venue_days": 10, "missing_venue_days": 2, "by_band": {"midnight": {"races": 18, "missing": 16}}}
    line = w._lines_gap_line(g)
    assert "16 / 100 R (16.0%)" in line and "⚠️" in line
    ok = {**g, "missing_races": 1, "rate": 0.01}
    assert "✅" in w._lines_gap_line(ok)
    assert "失敗" in w._lines_gap_line({"error": "x"})
