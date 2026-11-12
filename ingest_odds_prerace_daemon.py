"""N 分前 odds snapshot daemon (1 日 1 回起動、コンソール非表示)

毎分 cron の代替: 1 日に 1 回だけ pythonw で起動して、当日のレース全部を
複数 offset で順次処理する。CMD ウィンドウは pythonw のため出ない (UX 改善)。

設計 (Codex 5/15 提案 B' 案、複数 offset 同時取得):
- 起動時に race_entries.csv から今日のレース一覧と st_time を読む
- 取得済 (prerace_snapshot_log.csv の status=ok) は (race, target_offset_min)
  単位で除外 → 同じレースの 5/3/2 分前を別 snapshot として扱える
- 各レースについて、複数 lead_min ごとに snapshot 時刻を計算
  - lead_mins は CSV "5,3,2" / 環境変数 KEIRIN_LEAD_MINS / CLI --lead-mins で切替
  - デフォルト "5,3,2" (蓄積期、drift 比較用)
  - 本番運用に移ったら "3" や "2" 単一値に絞る想定
- snapshot_at が 2 分以上前のものは「乗り遅れ」扱いで skip
- 全 (race, offset) ペアを時刻順に処理:
    sleep until snapshot_at → capture → 次のペアへ
- 最終 capture 後に exit

ログは logs/cron_prerace_daemon_YYYY-MM-DD.log に書く (pythonw は stdout なし)。

7:00 朝の cron が race_entries.csv に当日データを書き込むため、本デーモンは
8:00 に起動するのが安全。entries 未準備の場合は最大 50 分まで retry してから
諦める。

使い方:
  pythonw ingest_odds_prerace_daemon.py                       # default "5,3,2"
  python  ingest_odds_prerace_daemon.py                       # 同上 (stdout 出る)
  python  ingest_odds_prerace_daemon.py --lead-mins 5         # 単一値運用
  python  ingest_odds_prerace_daemon.py --lead-mins 5,3,2     # 蓄積期
  python  ingest_odds_prerace_daemon.py --date 2026-05-16 --lead-mins 2

環境変数:
  KEIRIN_LEAD_MINS="5,3,2" pythonw ingest_odds_prerace_daemon.py
  KEIRIN_LEAD_MIN=3 (旧、後方互換)
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# pythonw で起動された場合に cwd と sys.path を補正 (相対 import が壊れないように)
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DATA_DIR
from src.netkeirin_client import NetkeirinClient
from src.netkeirin_parser import parse_odds_lists
from src.odds_prerace import monthly_name
from src.storage import append_rows

# logging: pythonw だと stdout が無いのでファイル必須
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"cron_prerace_daemon_{date.today().isoformat()}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),  # python (not pythonw) 時のみ stdout に出る
    ],
)
logger = logging.getLogger(__name__)

# 設定
# 蓄積期 default: drift 比較用に複数 offset 同時取得。
# Phase A (約定実現性検証, 2026-06-06) で締切直前 1 / 0.5 分前を追加。
# 5 分前 snapshot は確定配当と中央値 -2〜-7% 乖離する (analyze_settlement_wedge.py)
# ため、2 分前以降の収束過程と最終約定価格との残差ウェッジを測る目的。
LEAD_MINS_DEFAULT = "5,3,2,1,0.5"  # float 可 (0.5 = 30 秒前)
ENTRIES_RETRY_MAX = 10           # entries 未準備時の retry 回数
ENTRIES_RETRY_INTERVAL = 300     # entries retry 待機 (秒) = 5 分
MISS_TOLERANCE_MIN = 2           # snapshot_at がこの分数以上前なら乗り遅れ扱い
RETRY_BEFORE_START_SEC = 10      # fail 後の再試行は発走の何秒前まで許すか (2026-11-12)
RETRY_WAIT_SEC = 3               # fail 後の再試行までの待機 (秒)
REFRESH_INTERVAL_SEC = 600       # 同一場の発走時刻を再確認する間隔 (2026-11-12)
REFRESH_MIN_IDLE_SEC = 45        # 次の取得までこれ以上空くときだけ 1 場ぶん更新する
TIMING_LOG_NAME = "prerace_capture_timing.csv"


def _fmt_off(off: float) -> int | float:
    """offset を表示・保存用に正規化 (5.0 → 5, 0.5 → 0.5)。"""
    f = float(off)
    return int(f) if f.is_integer() else f


def _parse_lead_mins(raw: str) -> list[float]:
    """'5,3,2,1,0.5' → [5.0, 3.0, 2.0, 1.0, 0.5]。float 可、降順、重複削除。"""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    vals: list[float] = []
    for p in parts:
        try:
            vals.append(float(p))
        except ValueError:
            raise ValueError(f"invalid lead_min value: {p!r}")
    # 降順 (大きい offset = 早い時刻、処理順を時刻昇順にするため後で sort)
    return sorted(set(vals), reverse=True)


def _resolve_lead_mins(cli_value: str | None) -> list[float]:
    """LEAD_MINS を決定。優先順位: CLI > KEIRIN_LEAD_MINS > KEIRIN_LEAD_MIN > default。"""
    if cli_value:
        return _parse_lead_mins(cli_value)
    env_mins = os.environ.get("KEIRIN_LEAD_MINS")
    if env_mins:
        return _parse_lead_mins(env_mins)
    # 後方互換: 単一値の KEIRIN_LEAD_MIN
    env_min = os.environ.get("KEIRIN_LEAD_MIN")
    if env_min:
        try:
            return [float(env_min)]
        except ValueError:
            pass
    return _parse_lead_mins(LEAD_MINS_DEFAULT)


def parse_st_time(race_date: str, st_time: str) -> datetime | None:
    """'HH:MM' or 'HH:MM:SS' を datetime に。空なら None。"""
    if not st_time:
        return None
    try:
        parts = st_time.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) >= 3 else 0
        d = date.fromisoformat(race_date)
        return datetime(d.year, d.month, d.day, h, m, s)
    except (ValueError, IndexError):
        return None


def load_today_entries(target_date: str) -> list[dict]:
    """race_entries.csv から target_date のユニーク (race, st_time) 一覧を返す。"""
    path = DATA_DIR / "race_entries.csv"
    if not path.exists():
        return []

    seen: set[tuple[str, str, str]] = set()
    races: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("race_date") != target_date:
                continue
            key = (row["race_date"], row["place_code"], row["race_no"])
            if key in seen:
                continue
            seen.add(key)
            races.append({
                "race_date": row["race_date"],
                "place_code": row["place_code"],
                "race_no": row["race_no"],
                "st_time": row.get("st_time", ""),
            })
    return races


def load_captured(target_date: str) -> set[tuple[str, str, str, float]]:
    """既に status=ok で取得済みの (date, place, race_no, target_offset_min) を返す。

    target_offset_min を含むことで、同一レースの 5/3/2 分前 snapshot を別物として
    扱える (Codex 5/15 提案 B' 案)。
    """
    path = DATA_DIR / "prerace_snapshot_log.csv"
    if not path.exists():
        return set()
    captured: set[tuple[str, str, str, float]] = set()
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("race_date") != target_date:
                continue
            if row.get("status") != "ok":
                continue
            try:
                offset = _fmt_off(float(row.get("target_offset_min", "")))
            except (ValueError, TypeError):
                # 旧スキーマ (target_offset_min 無し) の行は default 5 として扱う
                offset = 5
            captured.add(
                (row["race_date"], row["place_code"], row["race_no"], offset)
            )
    return captured


def capture_race(
    client: NetkeirinClient,
    race_date: str,
    place_code: int,
    race_no: int,
    snapshot_dt: datetime,
    minutes_before: int,
    target_offset_min: float,
) -> dict:
    """1 レースのオッズ snapshot を取得し、CSV へ追記。"""
    race_id = NetkeirinClient.make_race_id(race_date, place_code, race_no)
    snapshot_iso = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S")
    off_disp = _fmt_off(target_offset_min)

    fetch_start = datetime.now()
    try:
        odds_data = client.get_odds(race_id)
    except Exception as e:
        logger.error("  R%d off=%s (%s) FETCH ERROR: %s",
                     race_no, off_disp, race_id, e)
        return {"status": "fail", "n_rows": 0, "note": f"fetch_error: {e}"[:200],
                "fetch_start": fetch_start, "fetch_end": datetime.now(),
                "server_date": getattr(client, "last_response_date", "")}
    fetch_end = datetime.now()
    timing = {"fetch_start": fetch_start, "fetch_end": fetch_end,
              "server_date": getattr(client, "last_response_date", "")}

    if odds_data is None:
        logger.info("  R%d off=%s (%s) NG (no odds)",
                    race_no, off_disp, race_id)
        return {"status": "no_odds", "n_rows": 0, "note": "netkeirin_NG", **timing}

    rows = parse_odds_lists(place_code, race_date, race_no, odds_data)
    if not rows:
        return {"status": "no_odds", "n_rows": 0, "note": "empty_rows", **timing}

    for r in rows:
        r["snapshot_dt"] = snapshot_iso
        r["minutes_before_start"] = minutes_before
        r["target_offset_min"] = off_disp

    # 月次パーティション race_odds_prerace_YYYY-MM.csv (race_date の年月) に追記。
    # 旧単一ファイル race_odds_prerace.csv にはもう書かない (2026-09-04、
    # 800MB 全読みの解消。読み手は src/odds_prerace.py 経由で月次+旧を統合)。
    n = append_rows(monthly_name(race_date), rows)
    logger.info(
        "  R%d off=%s: captured %d rows (mins_before=%d, official_dt=%s)",
        race_no, off_disp, n, minutes_before,
        odds_data.get("official_dt", ""),
    )
    return {"status": "ok", "n_rows": n, "note": "", **timing}


def timing_row(race: dict, target_offset: float, snapshot_at: datetime,
               result: dict, st_dt: datetime) -> dict | None:
    """取得完了基準の遅延記録 1 行 (prerace_capture_timing.csv)。

    既存 capture_lag_sec は「sleep 明けの時刻 - 予定」で、HTTP 所要・リトライ待ちを含まない
    (2026-11-12 監査: p95 が常に 0.0 で遅い取得を検知できなかった)。ここでは fetch 完了時刻を
    基準にした done_lag_sec と、発走までの残り seconds_before_done を記録する。
    official_dt は発走前の API が返さず 5 月から一貫して空のため、HTTP の Date ヘッダ
    (サーバ時計) を server_date に残して独立な時刻の手掛かりにする。
    """
    fs, fe = result.get("fetch_start"), result.get("fetch_end")
    if fs is None or fe is None:
        return None
    return {
        "race_date": race["race_date"], "place_code": race["place_code"],
        "race_no": race["race_no"], "target_offset_min": _fmt_off(target_offset),
        "status": result["status"],
        "scheduled_snapshot_dt": snapshot_at.strftime("%Y-%m-%d %H:%M:%S"),
        "fetch_start_dt": fs.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "fetch_end_dt": fe.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "fetch_sec": round((fe - fs).total_seconds(), 2),
        "done_lag_sec": round((fe - snapshot_at).total_seconds(), 2),
        "seconds_before_done": round((st_dt - fe).total_seconds(), 1),
        "server_date": result.get("server_date", ""),
    }


def apply_program_update(pending: list, place_code: str, race_date: str,
                         rinfo: list) -> dict:
    """出走表 (PJ0305 rInfo) の最新値で、その場の未取得スケジュールを更新する。

    pending: [{"race": {...}, "st_dt": datetime, "snapshot_at": datetime, "offset": float}]
    - 発走時刻 (stTime) が変わったレース -> st_dt / snapshot_at を再計算 (繰上げ・繰下げ)
    - 結果が既に出ている (resultFlg) / rInfo から消えた (中止・番組変更) -> 未取得分を破棄
    戻り値: {"moved": n, "dropped": n}。pending は破壊的に更新する。
    """
    info = {str(r.get("raceNo")): r for r in rinfo or []}
    moved = dropped = 0
    keep = []
    for it in pending:
        race = it["race"]
        if race["place_code"] != place_code or race["race_date"] != race_date:
            keep.append(it)
            continue
        r = info.get(str(race["race_no"]))
        if r is None or r.get("resultFlg"):
            dropped += 1
            continue
        new_st = str(r.get("stTime") or "").strip()
        new_dt = parse_st_time(race_date, new_st) if new_st else None
        if new_dt is not None and new_dt != it["st_dt"]:
            it["st_dt"] = new_dt
            it["snapshot_at"] = new_dt - timedelta(minutes=it["offset"])
            race["st_time"] = new_st
            moved += 1
        keep.append(it)
    pending[:] = keep
    return {"moved": moved, "dropped": dropped}


def refresh_one_venue(client_keirin, pending: list, last_refresh: dict,
                      enc_cache: dict, now: datetime) -> str | None:
    """最も古く確認した場を 1 つだけ更新する。更新した場コードを返す (対象なしは None)。

    例外はすべて握ってログのみ (更新に失敗しても元のスケジュールで取得を続ける)。
    """
    venues = sorted({(it["race"]["race_date"], it["race"]["place_code"]) for it in pending})
    due = [v for v in venues
           if (now - last_refresh.get(v, datetime.min)).total_seconds() >= REFRESH_INTERVAL_SEC]
    if not due:
        return None
    v = min(due, key=lambda x: last_refresh.get(x, datetime.min))
    last_refresh[v] = now
    rd, pc = v
    try:
        from src.parser import extract_json_data, parse_program_meta
        enc = enc_cache.get(v)
        if enc is None:
            pc0201 = extract_json_data(client_keirin.get_raceprogram_html(int(pc), rd), "PC0201")
            if not pc0201 or not pc0201.get("C0201data"):
                return pc
            races = parse_program_meta(int(pc), rd, pc0201)["races"]
            if not races:
                return pc
            enc = races[0]["enc_para_r"]
            enc_cache[v] = enc
        pj0305 = extract_json_data(client_keirin.get_racelist_html(enc), "PJ0305")
        if not pj0305:
            return pc
        res = apply_program_update(pending, pc, rd, pj0305.get("rInfo", []))
        if res["moved"] or res["dropped"]:
            logger.info("Program refresh pc=%s: 発走時刻変更 %d 件 / 破棄 (終了・中止) %d 件",
                        pc, res["moved"], res["dropped"])
    except Exception as e:
        logger.warning("Program refresh pc=%s failed (元のスケジュールで継続): %s", pc, e)
    return pc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", "--target-date", dest="target_date", default=None,
                    help="対象日 (YYYY-MM-DD)、デフォルトは today")
    ap.add_argument("--lead-mins", default=None,
                    help="発走の何分前に snapshot を取るか、CSV 指定可 "
                         "(default '5,3,2'、環境変数 KEIRIN_LEAD_MINS でも指定可)。"
                         "単一値も OK (例: '3')。")
    # 後方互換: 旧 --lead-min 単一指定も受け付け
    ap.add_argument("--lead-min", type=int, default=None, help=argparse.SUPPRESS)
    # 後方互換: 旧 `python daemon.py 2026-05-14` 形式の positional 引数も許容
    ap.add_argument("positional_date", nargs="?", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    target_date = args.target_date or args.positional_date or date.today().isoformat()
    # 旧 --lead-min が指定されたら CSV 形式に変換
    cli_value = args.lead_mins
    if cli_value is None and args.lead_min is not None:
        cli_value = str(args.lead_min)
    lead_mins = _resolve_lead_mins(cli_value)

    logger.info("=" * 60)
    logger.info("=== Daemon start: target_date=%s, lead_mins=%s 分前 ===",
                target_date, lead_mins)
    logger.info("=" * 60)

    # entries を最大 50 分 retry (朝 7:00 lines cron 失敗対策)
    races: list[dict] = []
    for attempt in range(ENTRIES_RETRY_MAX):
        races = load_today_entries(target_date)
        if races:
            logger.info("Loaded %d unique races for %s", len(races), target_date)
            break
        logger.info("No race_entries for %s, retry %d/%d in %ds",
                    target_date, attempt + 1, ENTRIES_RETRY_MAX,
                    ENTRIES_RETRY_INTERVAL)
        time.sleep(ENTRIES_RETRY_INTERVAL)
    if not races:
        logger.info("No races after %d retries. Exit.", ENTRIES_RETRY_MAX)
        return

    captured = load_captured(target_date)
    logger.info("Already captured: %d (race,offset) pairs", len(captured))

    # snapshot スケジュール組み立て: (race, st_dt, snapshot_at, target_offset_min)
    # 各レースについて lead_mins 全部分 entry を作る
    schedule: list[tuple[dict, datetime, datetime, float]] = []
    for race in races:
        st_dt = parse_st_time(race["race_date"], race["st_time"])
        if st_dt is None:
            continue
        for offset in lead_mins:
            off_norm = _fmt_off(offset)
            key = (race["race_date"], race["place_code"],
                   race["race_no"], off_norm)
            if key in captured:
                continue
            snapshot_at = st_dt - timedelta(minutes=offset)
            schedule.append((race, st_dt, snapshot_at, offset))

    schedule.sort(key=lambda x: x[2])

    # 既に乗り遅れたエントリ除外
    now = datetime.now()
    cutoff = now - timedelta(minutes=MISS_TOLERANCE_MIN)
    fresh_schedule = [
        (r, st, sa, off) for r, st, sa, off in schedule if sa > cutoff
    ]
    missed = len(schedule) - len(fresh_schedule)
    if missed:
        logger.info("Missed %d (race, offset) pairs (snapshot window already past)",
                    missed)

    if not fresh_schedule:
        logger.info("Nothing to capture today. Exit.")
        return

    logger.info("Schedule: %d captures planned (across offsets %s)",
                len(fresh_schedule), lead_mins)
    for r, st, sa, off in fresh_schedule[:5]:
        logger.info("  %s pc=%s R%s off=%s: st=%s snapshot_at=%s",
                    r["race_date"], r["place_code"], r["race_no"],
                    _fmt_off(off), st.strftime("%H:%M"), sa.strftime("%H:%M:%S"))
    if len(fresh_schedule) > 5:
        logger.info("  ... (+%d more)", len(fresh_schedule) - 5)

    client = NetkeirinClient()
    try:
        from src.client import KeirinClient
        keirin = KeirinClient()
    except Exception as e:  # 時刻更新は任意機能 (失敗しても取得は続ける)
        logger.warning("KeirinClient 初期化失敗 (発走時刻の更新は無効): %s", e)
        keirin = None
    log_rows: list[dict] = []
    pending = [{"race": r, "st_dt": st, "snapshot_at": sa, "offset": off}
               for r, st, sa, off in fresh_schedule]
    last_refresh: dict = {}
    enc_cache: dict = {}
    for v in {(it["race"]["race_date"], it["race"]["place_code"]) for it in pending}:
        last_refresh[v] = datetime.now()   # 起動直後は朝の entries を信用する

    while pending:
        pending.sort(key=lambda it: it["snapshot_at"])
        it = pending[0]
        race, st_dt, snapshot_at, target_offset = (
            it["race"], it["st_dt"], it["snapshot_at"], it["offset"])
        now = datetime.now()
        wait_sec = (snapshot_at - now).total_seconds()
        if wait_sec > REFRESH_MIN_IDLE_SEC and keirin is not None:
            # 次の取得まで余裕があるときだけ 1 場ぶん発走時刻を再確認 (変更・中止への追随)
            if refresh_one_venue(keirin, pending, last_refresh, enc_cache, now):
                continue
        pending.pop(0)
        if wait_sec > 0:
            logger.info("Sleep %.0fs (%s) → %s pc=%s R%s off=%s @ %s",
                        wait_sec,
                        str(timedelta(seconds=int(wait_sec))),
                        race["race_date"], race["place_code"],
                        race["race_no"], _fmt_off(target_offset),
                        snapshot_at.strftime("%H:%M:%S"))
            time.sleep(wait_sec)

        now = datetime.now()
        secs_before = (st_dt - now).total_seconds()
        mins_before = int(round(secs_before / 60.0))

        result = capture_race(
            client,
            race["race_date"], int(race["place_code"]), int(race["race_no"]),
            now, mins_before, target_offset,
        )
        # fail (通信/JSON エラー) は発走前なら 1 回だけ再試行 (2026-11-12)。
        # no_odds (サイトが「オッズなし」と返した) は再試行しない。
        if result["status"] == "fail" and (
                st_dt - datetime.now()).total_seconds() > RETRY_BEFORE_START_SEC:
            time.sleep(RETRY_WAIT_SEC)
            first_note = result["note"]
            result = capture_race(
                client,
                race["race_date"], int(race["place_code"]), int(race["race_no"]),
                now, mins_before, target_offset,
            )
            result["note"] = ("retried_ok" if result["status"] == "ok"
                              else "retried; first=%s" % first_note)[:200]
        row = {
            "race_date": race["race_date"],
            "place_code": race["place_code"],
            "race_no": race["race_no"],
            "st_time": race["st_time"],
            "snapshot_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            "minutes_before_start": mins_before,
            "target_offset_min": _fmt_off(target_offset),
            "status": result["status"],
            "n_rows": result["n_rows"],
            "note": result["note"],
            # 秒精度 3 列 (監査 P1-5): 0.5 分 offset の厳密判定・発走後取得の除外用
            "scheduled_snapshot_dt": snapshot_at.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds_before_start": int(secs_before),
            "capture_lag_sec": round((now - snapshot_at).total_seconds(), 1),
        }
        log_rows.append(row)
        # 逐次 append: PC sleep/シャットダウン耐性 (5/27, 5/29 で
        # 一括 append 設計が log ロストを起こした教訓、2026-05-31)
        try:
            append_rows("prerace_snapshot_log.csv", [row])
        except Exception as e:
            logger.error("snapshot_log append failed: %s", e)
        try:
            trow = timing_row(race, target_offset, snapshot_at, result, st_dt)
            if trow:
                append_rows(TIMING_LOG_NAME, [trow])
        except Exception as e:
            logger.error("timing_log append failed: %s", e)

    n_ok = sum(1 for r in log_rows if r["status"] == "ok")
    n_no = sum(1 for r in log_rows if r["status"] == "no_odds")
    n_fail = sum(1 for r in log_rows if r["status"] == "fail")
    # offset 別サマリ
    by_offset: dict[float, dict[str, int]] = {}
    for r in log_rows:
        off = float(r["target_offset_min"])
        by_offset.setdefault(off, {"ok": 0, "no_odds": 0, "fail": 0})
        by_offset[off][r["status"]] = by_offset[off].get(r["status"], 0) + 1
    logger.info("=" * 60)
    logger.info("=== Daemon done: ok=%d, no_odds=%d, fail=%d ===",
                n_ok, n_no, n_fail)
    for off in sorted(by_offset.keys(), reverse=True):
        s = by_offset[off]
        logger.info("  offset=%s 分前: ok=%d, no_odds=%d, fail=%d",
                    _fmt_off(off), s.get("ok", 0), s.get("no_odds", 0), s.get("fail", 0))
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Daemon crashed")
        raise
