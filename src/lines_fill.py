"""当日の並び (race_lines.csv) の取り直し (2026-11-15)

背景: 朝 7:00 の取り込み (ingest_today_lines.py) の時刻には、ナイター・ミッドナイト開催の
並び (PJ0305 nInfo) がまだ掲載されていない。7〜11 月の実測で、並び欠損レースの約 93% が
発走 20 時以降のミッドナイト開催で、場・日単位でまるごと欠ける (レース単位の部分欠損は 0)。
2026-11-15 の実測では、ミッドナイト 2 場の並びの掲載更新時刻が 11:12 / 11:25 だった。
並びは過去日のページからは取れないため、取り逃すと永久に欠損になる。

本モジュールは、一日中動いている prerace デーモンの中で「並びが未取得の場・日」を
発走前に取り直すための部品 (純粋関数 + 1 本のワーカースレッド)。

- 取りに行くのは race_lines.csv に行が無いレースだけ (取得済みは再取得しない・二重行を作らない)。
  追記は朝の取り込みと同じ parse_race_lines + append_rows。
- ワーカーは専用スレッド + 専用 KeirinClient で動き、オッズ取得 (本体ループ) と待ち合わせない。
  例外はすべて握ってログへ (デーモン本体を止めない)。
- stdout へは書かない (pythonw 配下で sys.stdout が None のため、print / reconfigure を使わない)。
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable

from .config import DATA_DIR
from .storage import append_rows

logger = logging.getLogger(__name__)

# 場・日の最初の (未発走の) レースの何分前に取りに行くか。ミッドナイトの並びは 11 時台に
# 掲載されるので、120 分前でほぼ取れる。取れなかったときの再試行として 60/30/10 分前も張る。
LINES_CHECK_LEADS_MIN = (120, 60, 30, 10)
# 連続する確認の間隔 (秒)。サイトへのアクセスを詰めない
LINES_CHECK_GAP_SEC = 3.0
LINES_NAME = "race_lines.csv"


def _parse_hm(race_date: str, st_time: str) -> datetime | None:
    if not st_time:
        return None
    try:
        parts = st_time.split(":")
        d = date.fromisoformat(race_date)
        return datetime(d.year, d.month, d.day, int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def load_line_keys(race_date: str) -> set[tuple[str, str]]:
    """race_lines.csv に行がある (place_code, race_no) の集合 (指定日のみ)。"""
    path = DATA_DIR / LINES_NAME
    if not path.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("race_date") == race_date:
                keys.add((str(row.get("place_code")), str(row.get("race_no"))))
    return keys


def missing_by_venue(races: list[dict], have: set[tuple[str, str]]) -> dict[str, list[dict]]:
    """races (race_date/place_code/race_no/st_time を持つ dict) のうち並びが無いものを場ごとに返す。"""
    out: dict[str, list[dict]] = {}
    for r in races:
        if (str(r["place_code"]), str(r["race_no"])) in have:
            continue
        out.setdefault(str(r["place_code"]), []).append(r)
    return out


def plan_line_checks(races: list[dict], have: set[tuple[str, str]], now: datetime,
                     leads=LINES_CHECK_LEADS_MIN) -> list[dict]:
    """並びの取り直し計画: [{"due": datetime, "place_code": str, "race_date": str, "last": bool}] を due 昇順で返す。

    - 場ごとに「並びが無く、まだ発走していない最初のレース」の発走 N 分前 (N=leads) に確認を置く。
    - すでに過ぎた時刻の確認は、場ごとに 1 回だけ「今 (now)」へ寄せる (起動直後の取り直し)。
    - 未発走のレースが 1 つも無い場は計画しない (事後には取れない)。
    - 場ごとの最後の確認に last=True を付ける (ここでも取れなければ欠測台帳へ)。
    """
    plan: list[dict] = []
    for pc, rs in missing_by_venue(races, have).items():
        upcoming = []
        for r in rs:
            st = _parse_hm(r["race_date"], r.get("st_time", ""))
            if st is not None and st > now:
                upcoming.append((st, r))
        if not upcoming:
            continue
        first_st, first = min(upcoming, key=lambda x: x[0])
        dues = sorted({first_st - timedelta(minutes=m) for m in leads})
        past = [d for d in dues if d <= now]
        future = [d for d in dues if d > now]
        items: list[datetime] = ([now] if past else []) + future
        for i, d in enumerate(items):
            plan.append({"due": d, "place_code": pc, "race_date": first["race_date"],
                         "last": i == len(items) - 1})
    plan.sort(key=lambda x: (x["due"], x["place_code"]))
    return plan


def fill_venue_day(client, race_date: str, place_code: int | str,
                   enc_cache: dict | None = None) -> dict:
    """1 場・1 日の並びを取り、race_lines.csv に無いレースだけ追記する。

    戻り値: {"status": "ok"|"empty"|"no_race"|"error", "races": [追記した race_no], "rows": 追記行数}。
    例外は握らない (呼び出し側 = ワーカーが握ってログへ)。
    """
    from .parser import extract_json_data, parse_program_meta, parse_race_lines

    pc = int(place_code)
    enc_cache = enc_cache if enc_cache is not None else {}
    key = (race_date, pc)
    enc = enc_cache.get(key)
    if enc is None:
        pc0201 = extract_json_data(client.get_raceprogram_html(pc, race_date), "PC0201")
        if not pc0201 or not pc0201.get("C0201data"):
            return {"status": "no_race", "races": [], "rows": 0}
        races = parse_program_meta(pc, race_date, pc0201)["races"]
        if not races:
            return {"status": "no_race", "races": [], "rows": 0}
        enc = races[0]["enc_para_r"]
        enc_cache[key] = enc
    pj0305 = extract_json_data(client.get_racelist_html(enc), "PJ0305")
    if not pj0305:
        return {"status": "error", "races": [], "rows": 0}
    rows = parse_race_lines(pc, race_date, pj0305)
    have = load_line_keys(race_date)
    new_rows = [r for r in rows if (str(pc), str(r["race_no"])) not in have]
    if not new_rows:
        return {"status": "empty" if not rows else "ok", "races": [], "rows": 0}
    n = append_rows(LINES_NAME, new_rows)
    added = sorted({int(r["race_no"]) for r in new_rows})
    return {"status": "ok", "races": added, "rows": n}


def run_line_checks(plan: list[dict], races: list[dict], fill: Callable[[str, str], dict],
                    record_gap: Callable[[str, str, str, str], object] | None = None,
                    now_fn: Callable[[], datetime] = datetime.now,
                    sleep_fn: Callable[[float], None] = time.sleep,
                    gap_sec: float = LINES_CHECK_GAP_SEC) -> dict:
    """plan を due 順に実行する (ワーカーの本体。テストでは時計・sleep を差し替える)。

    - 各確認の直前に race_lines.csv を読み直し、その場のレースが揃っていれば何もしない
      (取得済みは再取得しない)。揃った場の以降の確認は捨てる。
    - fill の例外は握ってログへ (次の確認へ進む)。
    - last の確認でも揃わなければ record_gap("lines", 日付, 場, 理由) で欠測台帳へ残す。
    戻り値: {"filled": [(場, 追記R数)], "gave_up": [場], "errors": n, "skipped": n}
    """
    out = {"filled": [], "gave_up": [], "errors": 0, "skipped": 0}
    done: set[str] = set()
    by_venue = {}
    for r in races:
        by_venue.setdefault(str(r["place_code"]), []).append(r)
    first = True
    for item in plan:
        pc, rd = item["place_code"], item["race_date"]
        if pc in done:
            out["skipped"] += 1
            continue
        wait = (item["due"] - now_fn()).total_seconds()
        if wait > 0:
            sleep_fn(wait)
        elif not first:
            sleep_fn(gap_sec)
        first = False
        try:
            have = load_line_keys(rd)
            if not missing_by_venue(by_venue.get(pc, []), have):
                done.add(pc)
                out["skipped"] += 1
                continue
            res = fill(rd, pc)
            if res.get("rows"):
                out["filled"].append((pc, len(res.get("races", []))))
                logger.info("lines retry pc=%s: 並び %d レース分を追記 (%d 行)",
                            pc, len(res.get("races", [])), res["rows"])
            have = load_line_keys(rd)
            if not missing_by_venue(by_venue.get(pc, []), have):
                done.add(pc)
                continue
            if item["last"]:
                out["gave_up"].append(pc)
                logger.warning("lines retry pc=%s: 発走前の最後の確認でも並びが未掲載 (status=%s) → 欠測台帳へ",
                               pc, res.get("status"))
                if record_gap is not None:
                    record_gap("lines", rd, pc,
                               "ミッドナイト/ナイター等で並び (nInfo) が発走前の最終確認まで未掲載 "
                               "(デーモンの取り直しでも取得できず)")
        except Exception as e:  # noqa: BLE001  デーモンを止めない
            out["errors"] += 1
            logger.warning("lines retry pc=%s failed (握って継続): %s", pc, e)
    return out


def start_lines_worker(races: list[dict], now: datetime | None = None) -> threading.Thread | None:
    """デーモンから呼ぶ: 並び未取得の場・日があれば取り直しワーカー (daemon スレッド) を起動する。

    起動できない・計画が空なら None。例外は握ってログのみ。
    """
    try:
        now = now or datetime.now()
        if not races:
            return None
        rd = races[0]["race_date"]
        plan = plan_line_checks(races, load_line_keys(rd), now)
        if not plan:
            logger.info("lines retry: 並び未取得の場・日なし (または全レース発走済み)")
            return None
        venues = sorted({p["place_code"] for p in plan})
        logger.info("lines retry: %d 場の並びが未取得 → %d 回の確認を予約 (最初 %s / 最後 %s)",
                    len(venues), len(plan),
                    plan[0]["due"].strftime("%H:%M"), plan[-1]["due"].strftime("%H:%M"))

        def _work() -> None:
            try:
                from .client import KeirinClient
                from .gaps import record_gap
                client = KeirinClient()
                enc_cache: dict = {}

                def _fill(d: str, p: str) -> dict:
                    try:
                        return fill_venue_day(client, d, p, enc_cache)
                    except Exception:
                        try:
                            client.reset_session()
                        except Exception:
                            pass
                        raise

                run_line_checks(plan, races, fill=_fill, record_gap=record_gap)
            except Exception as e:  # noqa: BLE001
                logger.warning("lines retry worker crashed (握って終了): %s", e)

        t = threading.Thread(target=_work, name="lines-retry", daemon=True)
        t.start()
        return t
    except Exception as e:  # noqa: BLE001
        logger.warning("lines retry の起動に失敗 (オッズ取得は継続): %s", e)
        return None
