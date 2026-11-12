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
from src.config import DATA_DIR
from src.gaps import record_gap
from src.storage import RaceDayIndex
from ingest_day import ingest_one_day

# 遡り再取得の窓 (日)。PC 停止・更新再起動で 05:00 の取込が飛んでも、次回以降に自動で補う
# (2026-11-12: 10/14 結果 79 レース未取得 + 10/15 丸ごと欠落が 1 か月放置された実害への対応)
LOOKBACK_DAYS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def plan_backfill(meta_dates: set, completion, today: date,
                  target_date: str, lookback: int = LOOKBACK_DAYS) -> dict:
    """遡り再取得の計画を返す: {race_date: None (全 43 場を試す) | [place_code, ...]}。

    対象日 = today-lookback 〜 today-2 のうち target_date 以外 (当日・昨日は通常取込の担当)。
      - race_meta に 1 行も無い日 -> None (開催日なのに丸ごと取り逃した可能性。全場を当たる。
        開催が無い日は各場が即 no_race で終わる)
      - race_completion に missing_* がある日 -> その場だけ per-race 自己治癒で再取得
    completion は DataFrame (race_date, place_code, status 列) か None。
    """
    plan: dict = {}
    miss: dict = {}
    if completion is not None and len(completion):
        m = completion[completion["status"].astype(str).str.startswith("missing")]
        for rd, pc in zip(m["race_date"].astype(str), m["place_code"].astype(str)):
            miss.setdefault(rd, set()).add(int(pc))
    for back in range(lookback, 1, -1):
        d = (today - timedelta(days=back)).isoformat()
        if d == target_date:
            continue
        if d not in meta_dates:
            plan[d] = None
        elif d in miss:
            plan[d] = sorted(miss[d])
    return plan


def run_backfill(client: KeirinClient, index: RaceDayIndex) -> int:
    """plan_backfill に従って遡り取得する。戻り値 = 試行した venue-day 数。"""
    import pandas as pd
    try:
        meta_dates = set(pd.read_csv(DATA_DIR / "race_meta.csv", usecols=["race_date"],
                                     dtype=str)["race_date"])
        comp_path = DATA_DIR / "race_completion.csv"
        comp = pd.read_csv(comp_path, dtype=str) if comp_path.exists() else None
    except Exception as e:
        logger.error("backfill plan failed: %s", e)
        return 0
    plan = plan_backfill(meta_dates, comp, date.today(), "")
    tried = 0
    for d, venues in sorted(plan.items()):
        logger.info("=== Backfill %s (%s) ===", d,
                    "全場 (race_meta に無い日)" if venues is None else "missing 場 %s" % venues)
        whole_day = venues is None
        for pc in (sorted(VENUE_CODES) if whole_day else venues):
            tried += 1
            try:
                # 全場走査は index 付き (既存セクションは O(1) で skip)、
                # 個別場は index 無し = per-race 自己治癒 (払戻の二重追記防止込み)
                ingest_one_day(client, pc, d, index=index if whole_day else None)
            except Exception as e:
                logger.error("BACKFILL FAILED %s venue=%d: %s", d, pc, e)
                try:
                    client.reset_session()
                except Exception:
                    pass
        if whole_day and _day_has_meta(d):
            # 並び・発走前オッズ・shadow は事後に取れない: 欠測として台帳へ
            for kind, note in (("lines", "遡り取得 (並びは発走後に取得不能)"),
                               ("prerace_odds", "朝の daemon 未稼働のため発走前オッズ無し (事後取得不能)"),
                               ("shadow", "朝の shadow 予測無し (事後に official を名乗れない)")):
                try:
                    record_gap(kind, d, "", note)
                except Exception as e:
                    logger.error("record_gap failed: %s", e)
    return tried


def _day_has_meta(d: str) -> bool:
    import pandas as pd
    try:
        m = pd.read_csv(DATA_DIR / "race_meta.csv", usecols=["race_date"], dtype=str)
        return bool((m["race_date"] == d).any())
    except Exception:
        return False


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

    # 遡り再取得 (欠測日の自動補完)。失敗しても通常取込の成否には影響させない。
    try:
        n_bf = run_backfill(client, index)
        logger.info("backfill: %d venue-days 試行", n_bf)
    except Exception as e:
        logger.error("backfill crashed: %s", e)

    # 完了台帳の更新 (2026-07-11 艦隊監査 P1): results/payouts を取り込んだ
    # 直後に race_completion.csv を実態に整合させる。旧運用は週次 retrain の
    # --days 30 のみで、取込済みレースが最長 1 週間 missing_results のまま残り
    # 週次 drift レポートが誤警告していた。台帳更新の失敗は収集の成否に
    # 影響させない (次回 cron / 週次 retrain でも再計算されるため)。
    try:
        from build_race_completion import refresh
        days = max((date.today() - date.fromisoformat(target_date)).days + 1, LOOKBACK_DAYS + 2)
        n = len(refresh(days=days))
        logger.info("race_completion.csv refreshed (days=%d, %d races)", days, n)
    except Exception as e:
        logger.error("race_completion refresh failed: %s", e)


if __name__ == "__main__":
    main()
