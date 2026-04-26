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

import sys
import logging
from datetime import date

from src.client import KeirinClient, VENUE_CODES
from ingest_day import ingest_one_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    client = KeirinClient()

    # JSJ048 で当日開催中の場リストを取得
    try:
        j048 = client.get_today_race_list()
    except Exception as e:
        logger.error("JSJ048 failed: %s", e)
        sys.exit(1)

    venues_today = sorted({
        int(r["naibuKeirinCd"])
        for r in j048.get("RaceList", [])
        if r.get("kaisaiDate", "").replace("-", "") == target_date.replace("-", "")
    })

    if not venues_today:
        logger.info("No venues open on %s", target_date)
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
