r"""今日開催の全場の出走表 + ライン情報を取得 (cron 朝実行用)

PJ0305 の nInfo (ライン情報) はレース完了後に空になるため、
レース開始前に取得する必要がある。

朝早めに実行して race_lines.csv に蓄積。
ingest_day.py が差分取り込み対応のため、夕方に同じ venue-day を再実行すれば
結果+払戻だけが追加される。

使い方:
  python ingest_today_lines.py          # 今日の全開催場
  python ingest_today_lines.py 2026-04-26  # 日付指定 (デバッグ用)

Windows タスクスケジューラ登録例 (毎朝 7:00):
  schtasks /create /tn "keirin-ai-lines" /tr "python C:\Users\user\Claude-Project\keirin-ai\ingest_today_lines.py" /sc daily /st 07:00
"""

import os
import sys
import time
import logging
from datetime import date

from src.client import KeirinClient, VENUE_CODES
from ingest_day import ingest_one_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# JSJ048 の "今日のレース一覧" は朝 7:00 時点ではまだ前日分を返す
# (実測: 5/16 07:52 で前日、08:04 で当日に切替)。
# 当日 kaisaiDate が出るまでリトライする (daemon と同じ思想)。
# 環境変数で無効化/調整可。手動デバッグ時は retry 不要なので 0 回も可。
JSJ048_RETRY_MAX = int(os.environ.get("KEIRIN_LINES_RETRY_MAX", "12"))
JSJ048_RETRY_INTERVAL = int(os.environ.get("KEIRIN_LINES_RETRY_SEC", "300"))


def _fetch_open_venues(client: KeirinClient, target_date: str) -> list[int]:
    """JSJ048 から target_date 開催の場コード一覧を取得。"""
    j048 = client.get_today_race_list()
    return sorted({
        int(r["naibuKeirinCd"])
        for r in j048.get("RaceList", [])
        if r.get("kaisaiDate", "").replace("-", "") == target_date.replace("-", "")
    })


def main() -> None:
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    client = KeirinClient()

    # 手動で日付指定した場合 (デバッグ) は retry しない。
    # cron (引数なし = today) のときだけ JSJ048 切替待ち retry を有効化。
    is_today_cron = len(sys.argv) <= 1
    retry_max = JSJ048_RETRY_MAX if is_today_cron else 1

    venues_today: list[int] = []
    for attempt in range(1, retry_max + 1):
        try:
            venues_today = _fetch_open_venues(client, target_date)
        except Exception as e:
            logger.error("JSJ048 failed (attempt %d/%d): %s",
                         attempt, retry_max, e)
            venues_today = []

        if venues_today:
            if attempt > 1:
                logger.info("JSJ048 returned %s venues on attempt %d",
                            len(venues_today), attempt)
            break

        if attempt < retry_max:
            logger.info(
                "JSJ048 not showing %s yet (attempt %d/%d). "
                "朝は前日分が返るため %ds 待って retry",
                target_date, attempt, retry_max, JSJ048_RETRY_INTERVAL,
            )
            time.sleep(JSJ048_RETRY_INTERVAL)

    if not venues_today:
        logger.info(
            "No venues open on %s after %d attempts "
            "(本当に開催なし、または JSJ048 異常)",
            target_date, retry_max,
        )
        return

    logger.info("=== Lines ingest: %s ===", target_date)
    logger.info("  Open venues today: %d -> %s",
                len(venues_today),
                [f"{pc}({VENUE_CODES.get(pc,'?')})" for pc in venues_today])

    summary = {"ingested": 0, "skipped": 0, "errors": 0}
    for pc in venues_today:
        try:
            counts = ingest_one_day(client, pc, target_date)
            if counts.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["ingested"] += 1
        except Exception as e:
            logger.error("FAILED %s venue=%d: %s", target_date, pc, e)
            summary["errors"] += 1
            try:
                client.reset_session()
            except Exception:
                pass

    logger.info("=== Lines ingest summary ===")
    logger.info("  Ingested: %d / Skipped: %d / Errors: %d",
                summary["ingested"], summary["skipped"], summary["errors"])


if __name__ == "__main__":
    main()
