"""1日分のレースデータを取得 → CSV 保存

Phase 1: 開催メタ + 出走表 + ライン情報
Phase 2: 選手成績 (JSJ002) + 結果 + 払戻 (JSJ012、レース完了分のみ)

使い方:
  python ingest_day.py YYYY-MM-DD placeCode
  python ingest_day.py 2026-04-25 42      # 名古屋
"""

import sys
import logging
from datetime import date as _date

from src.client import KeirinClient, VENUE_CODES
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


def _ingest_results_and_payouts(client: KeirinClient, place_code: int,
                                race_date: str, races: list[dict],
                                index: RaceDayIndex | None,
                                counts: dict[str, int]) -> None:
    """結果 + 払戻 (JSJ012) をレース完了分のみ per-race で取得して追記。"""
    finished_races = [r for r in races if r.get("race_end")]
    if finished_races:
        logger.info("  Fetching results for %d finished races", len(finished_races))
        results_added = 0
        for race in finished_races:
            r_no = race["race_no"]
            r_enc = race["enc_para_r"]
            if not r_enc:
                continue
            try:
                jsj012 = client.get_race_result(r_enc)
                if jsj012.get("resultCd") == 0:
                    results = parse_race_results(place_code, race_date, r_no, jsj012)
                    payouts = parse_payouts(place_code, race_date, r_no, jsj012)
                    n_res = append_rows("race_results.csv", results)
                    counts["results"] = counts.get("results", 0) + n_res
                    counts["payouts"] = counts.get("payouts", 0) + append_rows("payouts.csv", payouts)
                    results_added += n_res
            except Exception as e:
                logger.error("  JSJ012 R%d failed: %s", r_no, e)
        if index is not None and results_added > 0:
            index.mark("race_results.csv", place_code, race_date)
    else:
        logger.info("  No finished races (skip results/payouts)")


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
    if has_meta and has_entries and lines_ok and has_stats and has_results:
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
    ingest_one_day(client, place_code, race_date)


if __name__ == "__main__":
    main()
