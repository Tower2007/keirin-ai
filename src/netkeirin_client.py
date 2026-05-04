"""netkeirin (keirin.netkeiba.com) API クライアント

事前/確定オッズの取得用。keirin.jp は過去オッズを保持しない (要ログイン)
ため、第三者サイトの netkeirin から取得する。

API 仕様 (リバースエンジニアリング):
  POST https://keirin.netkeiba.com/api/race/
    Content-Type: application/x-www-form-urlencoded
    body: class=AplRaceOdds&method=get&compress=0&race_id=YYYYMMDD<jyo><race>
    Referer: https://keirin.netkeiba.com/race/odds/?race_id=...

レスポンス: JSON
  status: "OK" | "NG"
  data: { "nkrace_odds::<race_id>": { ... } }

robots.txt: 全許可確認済 (2026-05-04)
リクエスト間隔: 0.8 秒推奨 (keirin.jp と同等)
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

NETKEIRIN_API = "https://keirin.netkeiba.com/api/race/"
NETKEIRIN_REFERER_BASE = "https://keirin.netkeiba.com/race/odds/?race_id={}"


class NetkeirinAPIError(Exception):
    """netkeirin リクエスト失敗"""


class NetkeirinClient:
    """netkeirin オッズ取得クライアント"""

    def __init__(self) -> None:
        self._delay = float(os.getenv("NETKEIRIN_REQUEST_DELAY_SEC", "0.8"))
        self._ua = os.getenv(
            "NETKEIRIN_USER_AGENT",
            "Mozilla/5.0 (keirin-ai research; "
            "+https://github.com/Tower2007/keirin-ai)",
        )
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("https://", adapter)
        s.headers.update({
            "User-Agent": self._ua,
            "Accept-Language": "ja,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })
        return s

    @staticmethod
    def make_race_id(race_date: str, place_code: int, race_no: int) -> str:
        """('2026-04-26', 42, 12) -> '202604264212' (race_id 形式)。"""
        ymd = race_date.replace("-", "")
        return f"{ymd}{place_code:02d}{race_no:02d}"

    def _post_api(self, class_name: str, race_id: str) -> dict:
        """汎用 API 呼び出し。class_name で AplRaceOdds 等を指定。"""
        data = {
            "class": class_name,
            "method": "get",
            "compress": "0",
            "race_id": race_id,
            "input": "UTF-8",
            "output": "json",
        }
        headers = {
            "Referer": NETKEIRIN_REFERER_BASE.format(race_id),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = self._session.post(
            NETKEIRIN_API, data=data, headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            raise NetkeirinAPIError(
                f"{NETKEIRIN_API} returned {resp.status_code}: {resp.text[:200]}"
            )
        time.sleep(self._delay)
        return resp.json()

    # ─── 公開 API ──────────────────────────────────────

    def get_odds(self, race_id: str) -> dict | None:
        """1レースのオッズデータを返す。NG の場合は None。

        戻り値:
          {
            "official_dt": "YYYY-MM-DD HH:MM:SS",
            "list_3": [...],  # WH2 (枠複)
            "list_4": [...],  # WT2 (枠単)
            "list_5": [...],  # SH2 (二車複)
            "list_6": [...],  # ST2 (二車単)
            "list_7": [...],  # W   (ワイド: kumi_ban + min_odds + max_odds)
            "list_8": [...],  # RH3 (三連複)
            "list_9": [...],  # RT3 (三連単)
          }
        """
        obj = self._post_api("AplRaceOdds", race_id)
        if obj.get("status") != "OK":
            logger.debug("netkeirin NG for %s: %s", race_id, obj.get("reason", ""))
            return None
        key = f"nkrace_odds::{race_id}"
        return obj.get("data", {}).get(key)

    def get_horse(self, race_id: str) -> dict | None:
        """1レースの出走表 (selback 確認用)。"""
        obj = self._post_api("AplRaceHorse", race_id)
        if obj.get("status") != "OK":
            return None
        key = f"nkrace_horse::{race_id}"
        return obj.get("data", {}).get(key)
