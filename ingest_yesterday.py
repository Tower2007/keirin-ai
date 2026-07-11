r"""昨日開催の全場の結果+払戻を回収 (cron 翌朝実行用)

ingest_today_lines.py で取った lines/entries/stats に加え、
完了後に出る results/payouts を翌朝まとめて回収する。

開催が無かった場 (= PC0201 が空) は瞬時に no_race として終了するため、
全43場ループしても数分で終わる。

使い方:
  python ingest_yesterday.py             # 昨日の全43場ループ
  python ingest_yesterday.py 2026-04-26  # 日付指定 (デバッグ用)

Windows タスクスケジューラ登録例 (毎朝 5:00):
  schtasks /create /tn "keirin-ai-results" /sc daily /st 05:00 ^
    /tr "C:\Users\no28a\Claude-project\keirin-ai\scripts\cron_results.bat"
"""

import sys
import logging
from datetime import date, timedelta

from src.client import KeirinClient, VENUE_CODES
from src.storage import RaceDayIndex
from ingest_day import ingest_one_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    target_date = (
        sys.argv[1] if len(sys.argv) > 1
        else (date.today() - timedelta(days=1)).isoformat()
    )

    client = KeirinClient()
    index = RaceDayIndex()
    logger.info("=== Yesterday ingest: %s ===", target_date)
    logger.info("  Index sizes: %s", index.sizes())

    summary = {"completed": 0, "no_race": 0, "skipped": 0, "errors": 0}
    for pc in sorted(VENUE_CODES):
        try:
            counts = ingest_one_day(client, pc, target_date, index=index)
            if counts.get("skipped"):
                summary["skipped"] += 1
            elif not counts:
                summary["no_race"] += 1
            else:
                summary["completed"] += 1
        except Exception as e:
            logger.error("FAILED %s venue=%d: %s", target_date, pc, e)
            summary["errors"] += 1
            try:
                client.reset_session()
            except Exception:
                pass

    logger.info("=== Yesterday ingest summary (%s) ===", target_date)
    logger.info(
        "  completed=%d skipped=%d no_race=%d errors=%d",
        summary["completed"],
        summary["skipped"],
        summary["no_race"],
        summary["errors"],
    )

    # 完了台帳の更新 (2026-07-11 艦隊監査 P1): results/payouts を取り込んだ
    # 直後に race_completion.csv を実態に整合させる。旧運用は週次 retrain の
    # --days 30 のみで、取込済みレースが最長 1 週間 missing_results のまま残り
    # 週次 drift レポートが誤警告していた。台帳更新の失敗は収集の成否に
    # 影響させない (次回 cron / 週次 retrain でも再計算されるため)。
    try:
        from build_race_completion import refresh
        days = max((date.today() - date.fromisoformat(target_date)).days + 1, 3)
        n = len(refresh(days=days))
        logger.info("race_completion.csv refreshed (days=%d, %d races)", days, n)
    except Exception as e:
        logger.error("race_completion refresh failed: %s", e)


if __name__ == "__main__":
    main()
