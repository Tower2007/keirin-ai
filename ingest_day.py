"""1日分のレースデータを取得 → CSV 保存

Phase 1: 開催メタ + 出走表 + ライン情報
Phase 2: 選手成績 (JSJ002) + 結果 + 払戻 (JSJ012、レース完了分のみ)

per-race 自己治癒 (2026-07-11 Codex レビュー対応):
  旧実装は results セクションを venue-day 単位で skip したため、一部レースだけ
  取得できた日は残りが永久欠損した。**index 無し** かつ **過去日** かつ
  **results 行が既にある** 呼び出しでのみ per-race 完了状態を確認し、
    - results 行が全く無いレース        → JSJ012 再取得 (results+payouts 追記)
    - 着順ありだが払戻が無いレース      → JSJ012 再取得 (payouts のみ追記)
  を行う。append のみで重複を作らない範囲に限定 (置換はしない)。
  中止レース (results 行ありだが着順ゼロの ghost 行) は正当欠損として触らない。

  ⚠️ 発動経路の事実 (2026-07-11 監査で docstring を実装に整合):
  - 日次 cron (ingest_yesterday.py) は RaceDayIndex を渡すため heal は
    **発動しない** (venue-day 粒度のまま)。
  - heal が実際に発動するのは index 無し呼び出し =
    手動 `python ingest_day.py` と手動 recover_missing_races.py のみ
    (recover は cron 登録されていない)。日次の取りこぼしは
    recover_missing_races.py の手動実行で回復する。
  - index 付き呼び出し (backfill / 日次 cron) は従来の venue-day 粒度
    (性能優先、初回取得が仕事であり回復は recover スクリプトの責務)。

  完了台帳 (race_completion.csv) の鮮度 (2026-07-11 艦隊監査 P1):
  結果/払戻を追記した取込の直後に build_race_completion.refresh() を呼び、
  台帳を実態に整合させる。日次 cron は ingest_yesterday.py が、救済は
  recover_missing_races.py が、手動 ingest は本スクリプト main() が呼ぶ
  (ingest_one_day 自体は呼ばない — backfill の連続呼び出しで毎回
  再構築しないため。backfill 後は build_race_completion.py を手動実行)。

使い方:
  python ingest_day.py YYYY-MM-DD placeCode
  python ingest_day.py 2026-04-25 42      # 名古屋
"""

import csv
import sys
import logging
from datetime import date as _date

from src.client import KeirinClient, VENUE_CODES
from src.config import DATA_DIR
from src.parser import (
    extract_json_data,
    parse_program_meta,
    parse_race_entries,
    parse_race_lines,
    parse_race_stats,
    parse_race_results,
    parse_payouts,
)
from src.storage import append_row, append_rows, has_race_day, RaceDayIndex

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _existing_sections(place_code: int, race_date: str,
                       index: RaceDayIndex | None) -> dict[str, bool]:
    """各セクション CSV の存在状況を返す (index があれば O(1) のメモリ参照)。"""
    files = [
        ("meta", "race_meta.csv"),
        ("entries", "race_entries.csv"),
        ("lines", "race_lines.csv"),
        ("stats", "race_stats.csv"),
        ("results", "race_results.csv"),
    ]
    if index is not None:
        return {k: index.has(f, place_code, race_date) for k, f in files}
    return {k: has_race_day(f, place_code, race_date) for k, f in files}


def _ingest_entries_and_lines(client: KeirinClient, place_code: int,
                              race_date: str, enc_para_r: str,
                              has_entries: bool, has_lines: bool,
                              index: RaceDayIndex | None,
                              counts: dict[str, int]) -> None:
    """出走表 + ライン (PJ0305) を取得し、欠けているセクションのみ追記。"""
    racelist_html = client.get_racelist_html(enc_para_r)
    pj0305 = extract_json_data(racelist_html, "PJ0305")
    if not pj0305:
        raise RuntimeError(f"PJ0305 not found for {race_date} venue={place_code}")

    if not has_entries:
        entries = parse_race_entries(place_code, race_date, pj0305)
        counts["entries"] = append_rows("race_entries.csv", entries)
        if index is not None and entries:
            index.mark("race_entries.csv", place_code, race_date)
    if not has_lines:
        lines = parse_race_lines(place_code, race_date, pj0305)
        counts["lines"] = append_rows("race_lines.csv", lines)
        if index is not None and lines:
            index.mark("race_lines.csv", place_code, race_date)
        if not lines:
            logger.warning("  Lines empty (race already finished?): %s %d",
                           race_date, place_code)


def _ingest_stats(client: KeirinClient, place_code: int, race_date: str,
                  enc_para_r: str, index: RaceDayIndex | None,
                  counts: dict[str, int]) -> None:
    """選手成績 (JSJ002) を取得して追記。失敗はログのみで続行。"""
    try:
        jsj002 = client.get_race_stats(enc_para_r)
        if jsj002.get("raceInfo"):
            stats = parse_race_stats(place_code, race_date, jsj002)
            counts["stats"] = append_rows("race_stats.csv", stats)
            if index is not None and stats:
                index.mark("race_stats.csv", place_code, race_date)
    except Exception as e:
        logger.error("  JSJ002 (race_stats) failed: %s", e)


def _per_race_state(place_code: int, race_date: str) -> tuple[set, set, set]:
    """当該 venue-day の per-race 状態を CSV 走査で返す。

    返り値: (results_rows あり race_no 集合,
             着順 (tyaku 非空) あり race_no 集合,
             payouts あり race_no 集合)
    """
    pc = str(place_code)
    res_rows: set[str] = set()
    settled: set[str] = set()
    pays: set[str] = set()
    res_path = DATA_DIR / "race_results.csv"
    if res_path.exists():
        with open(res_path, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("race_date") != race_date or r.get("place_code") != pc:
                    continue
                rn = r.get("race_no", "")
                res_rows.add(rn)
                if str(r.get("tyaku", "")).strip():
                    settled.add(rn)
    pay_path = DATA_DIR / "payouts.csv"
    if pay_path.exists():
        with open(pay_path, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("race_date") == race_date and r.get("place_code") == pc:
                    pays.add(r.get("race_no", ""))
    return res_rows, settled, pays


def compute_result_gaps(place_code: int, race_date: str) -> tuple[set, set]:
    """per-race の欠損を返す (recover_missing_races.py からも使用)。

    返り値: (results_gap, payout_gap)
      results_gap = 着順ありでも行すらも無い race_no (JSJ012 全体を再取得)
        ※ 期待レース集合はここでは不明 (entries 走査は呼び出し側 or
           program["races"] で判定)。ここでは「results 行あり集合」を基準に
           _ingest_results_and_payouts 側で finished_races と突合する。
      payout_gap  = 着順ありなのに payouts が無い race_no (payouts のみ追記)
    中止レース (行ありだが着順ゼロ) はどちらにも入らない (正当欠損)。
    """
    res_rows, settled, pays = _per_race_state(place_code, race_date)
    payout_gap = settled - pays
    return res_rows, payout_gap


def _ingest_results_and_payouts(client: KeirinClient, place_code: int,
                                race_date: str, races: list[dict],
                                index: RaceDayIndex | None,
                                counts: dict[str, int],
                                existing_res_rows: set | None = None,
                                payout_gap: set | None = None) -> None:
    """結果 + 払戻 (JSJ012) をレース完了分のみ per-race で取得して追記。

    existing_res_rows / payout_gap が渡された場合 (per-race 自己治癒モード):
      - results 行が既にあるレースは results を追記しない (重複防止)
      - payout_gap のレースは payouts のみ追記
      - 両方揃っているレースは fetch もしない
    """
    finished_races = [r for r in races if r.get("race_end")]
    if not finished_races:
        logger.info("  No finished races (skip results/payouts)")
        return

    heal_mode = existing_res_rows is not None
    if heal_mode:
        payout_gap = payout_gap or set()
        targets = [r for r in finished_races
                   if str(r["race_no"]) not in existing_res_rows
                   or str(r["race_no"]) in payout_gap]
        if not targets:
            logger.info("  Results/payouts complete per-race (nothing to heal)")
            return
        logger.info("  Healing %d races (results_gap or payout_gap)", len(targets))
    else:
        targets = finished_races
        logger.info("  Fetching results for %d finished races", len(targets))

    results_added = 0
    for race in targets:
        r_no = race["race_no"]
        r_enc = race["enc_para_r"]
        if not r_enc:
            continue
        skip_results = heal_mode and str(r_no) in existing_res_rows
        try:
            jsj012 = client.get_race_result(r_enc)
            if jsj012.get("resultCd") == 0:
                payouts = parse_payouts(place_code, race_date, r_no, jsj012)
                if not skip_results:
                    results = parse_race_results(place_code, race_date, r_no, jsj012)
                    n_res = append_rows("race_results.csv", results)
                    counts["results"] = counts.get("results", 0) + n_res
                    results_added += n_res
                counts["payouts"] = counts.get("payouts", 0) + append_rows(
                    "payouts.csv", payouts)
        except Exception as e:
            logger.error("  JSJ012 R%d failed: %s", r_no, e)
    if index is not None and results_added > 0:
        index.mark("race_results.csv", place_code, race_date)


def ingest_one_day(client: KeirinClient, place_code: int, race_date: str,
                   index: RaceDayIndex | None = None) -> dict:
    """1日分のデータを取得して CSV に保存（差分取り込み対応）。

    既に CSV に存在するセクションはスキップし、欠けている部分のみ取得する。
    これにより、朝に lines/entries/stats を取り、夕方に同じ venue-day を
    再実行して結果+払戻だけを追加することが可能。

    `index` を渡すと存在チェックは O(1) のメモリ参照になり、書き込み後は
    自動で index を更新する。バックフィル等の連続呼び出しで利用。

    戻り値: 各 CSV に追記した行数の dict (skipped=True なら全部既存)。
    """

    # 各セクションの存在状況
    has = _existing_sections(place_code, race_date, index)
    has_meta = has["meta"]
    has_entries = has["entries"]
    has_lines = has["lines"]
    has_stats = has["stats"]
    has_results = has["results"]

    # 全部揃っていればスキップ。
    # past date は lines (PJ0305 nInfo) が取得不能なので、lines を必須から外す。
    is_past = race_date < _date.today().isoformat()
    lines_ok = has_lines or is_past
    # per-race 自己治癒 (index 無し呼び出しのみ): venue-day に results 行があっても
    # 「着順ありなのに払戻無し」のレースが残っていれば skip しない。
    # results 行が無いレース (missing_results) は program 取得後に判明するため
    # ここでは payout_gap だけで fast-path を解除する (results_gap は後段で処理)。
    existing_res_rows: set | None = None
    payout_gap: set | None = None
    heal_mode = index is None and has_results and is_past
    if heal_mode:
        existing_res_rows, payout_gap = compute_result_gaps(place_code, race_date)
    if (has_meta and has_entries and lines_ok and has_stats and has_results
            and not heal_mode):
        logger.info("Already complete: %s place=%d, skipping", race_date, place_code)
        return {"skipped": True}

    venue = VENUE_CODES.get(place_code, f"code{place_code}")
    logger.info("=== Ingest: %s %s (%d) ===", race_date, venue, place_code)
    logger.info("  Existing: meta=%s entries=%s lines=%s stats=%s results=%s",
                has_meta, has_entries, has_lines, has_stats, has_results)

    counts: dict[str, int] = {}

    # 1) 開催プログラム取得 (encParaR を得るため毎回必要)
    prog_html = client.get_raceprogram_html(place_code, race_date)

    pc0201 = extract_json_data(prog_html, "PC0201")
    if not pc0201 or not pc0201.get("C0201data"):
        logger.info("  No race on %s at %s", race_date, venue)
        return counts

    program = parse_program_meta(place_code, race_date, pc0201)

    # 公式キャンセル日 (中止/順延/打切) は entries/results が存在しないので
    # meta に is_canceled=True で記録して return。再 ingest 時の冪等性を保つ。
    if program["meta"]["is_canceled"]:
        if not has_meta:
            counts["meta"] = append_row("race_meta.csv", program["meta"])
            if index is not None:
                index.mark("race_meta.csv", place_code, race_date)
            logger.info("  Canceled day: %s / %s -- meta written (is_canceled=True)",
                        program["meta"]["place_name"], program["meta"]["race_name"])
        else:
            logger.info("  Canceled day (already in meta): %s", race_date)
        return counts

    if not program["races"]:
        logger.info("  No races available")
        return counts

    if not has_meta:
        counts["meta"] = append_row("race_meta.csv", program["meta"])
        if index is not None:
            index.mark("race_meta.csv", place_code, race_date)
        logger.info("  Meta: %s / grade=%s / %d races",
                    program["meta"]["place_name"],
                    program["meta"]["grade"],
                    len(program["races"]))

    enc_para_r = program["races"][0]["enc_para_r"]
    if not enc_para_r:
        raise RuntimeError(f"encParaR not found for {race_date} venue={place_code}")

    # 2) 出走表 + ライン (entries か lines のどちらか欠けていれば取得)
    if not has_entries or not has_lines:
        _ingest_entries_and_lines(client, place_code, race_date, enc_para_r,
                                  has_entries, has_lines, index, counts)

    # 3) 選手成績 (JSJ002)
    if not has_stats:
        _ingest_stats(client, place_code, race_date, enc_para_r, index, counts)

    # 4) 結果 + 払戻 (JSJ012, レース完了分のみ per-race)
    if not has_results:
        _ingest_results_and_payouts(client, place_code, race_date,
                                    program["races"], index, counts)
    elif heal_mode:
        # per-race 自己治癒: 行が無いレース + 払戻欠損レースだけ再取得
        _ingest_results_and_payouts(client, place_code, race_date,
                                    program["races"], index, counts,
                                    existing_res_rows=existing_res_rows,
                                    payout_gap=payout_gap)

    logger.info("=== Done: %s %s ===", race_date, venue)
    for name, count in sorted(counts.items()):
        logger.info("  %s: %d rows", name, count)
    return counts


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python ingest_day.py YYYY-MM-DD placeCode")
        print("  placeCode 例: 11=函館, 31=松戸, 42=名古屋, 87=熊本")
        sys.exit(1)

    race_date = sys.argv[1]
    place_code = int(sys.argv[2])
    client = KeirinClient()
    counts = ingest_one_day(client, place_code, race_date)

    # 完了台帳の更新 (2026-07-11 艦隊監査 P1): 結果/払戻を追記した場合は
    # race_completion.csv を実態に整合させる (stale な missing_* を残さない)。
    # 日次 cron は ingest_yesterday.py 側で、救済は recover_missing_races.py
    # 側でそれぞれ refresh するため、ここは手動 ingest (heal 含む) 用。
    if counts.get("results") or counts.get("payouts"):
        try:
            from build_race_completion import refresh
            days = max(
                (_date.today() - _date.fromisoformat(race_date)).days + 1, 3)
            refresh(days=days)
            logger.info("race_completion.csv refreshed (days=%d)", days)
        except Exception as e:
            logger.error("race_completion refresh failed: %s", e)


if __name__ == "__main__":
    main()
