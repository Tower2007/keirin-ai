r"""当日開催の全場 lines/entries/stats を回収 (cron 朝実行用)

旧設計 (5/16 まで): JSJ048「今日のレース一覧」で開催場を取得 → ループ。
   → JSJ048 が朝 7:00 時点で前日を返すバグがあり、retry 60 分でも不足する日があった。
   → 5/17〜5/22 の 6 日連続 snapshot 蓄積失敗 (2026-05-23 発覚)。

新設計 (5/23 以降): JSJ048 撤廃、全 43 場 raceprogram (PC0201) 直叩き。
   - 明示日付クエリなので「前日返し」問題が発生しない
   - 開催無しの場は PC0201 空で瞬時に no_race として 0.8 秒で抜ける
   - 全 43 場ループしても 1〜3 分で完走 (ingest_yesterday.py と同じパターン)

results/payouts は当日にはまだ無いため、ingest_one_day 内部で
"No finished races (skip results/payouts)" として skip される。

使い方:
  python ingest_today_lines.py             # 当日の全43場ループ
  python ingest_today_lines.py 2026-05-23  # 日付指定 (デバッグ用)

Windows タスクスケジューラ登録例 (毎朝 7:00):
  schtasks /create /tn "keirin-ai-lines" /sc daily /st 07:00 ^
    /tr "C:\Users\no28a\Claude-project\keirin-ai\scripts\cron_lines.bat"
"""

import sys
import logging
from datetime import date

from src.client import KeirinClient, VENUE_CODES
from src.storage import RaceDayIndex
from ingest_day import ingest_one_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    client = KeirinClient()
    index = RaceDayIndex()
    logger.info("=== Today lines ingest: %s ===", target_date)
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

    logger.info("=== Today lines summary (%s) ===", target_date)
    logger.info(
        "  completed=%d skipped=%d no_race=%d errors=%d",
        summary["completed"],
        summary["skipped"],
        summary["no_race"],
        summary["errors"],
    )


if __name__ == "__main__":
    main()
