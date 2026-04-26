"""keirin.jp HTML/JSON クライアント

セッションCookie自動取得 + リトライ/指数バックオフ。
GET のみ使用、CSRF 不要。

エンドポイント方式:
- /pc/json (純JSON、type=JSJxxx で分岐)
- /pc/dfw/dataplaza/guest/raceprogram?KCD=&KST= (HTML 内に jsonData 埋め込み)
- /pc/racelist?encp=  (HTML 内に jsonData['PJ0305'] 埋め込み)
- /pc/racelive?encp=  (HTML 内に jsonData['PC0201']/'PJ0314' 等)
"""

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

BASE_URL = "https://keirin.jp"

# 競輪場コード (jocd / KCD / naibuKeirinCd) → 名称
# 11 函館, 12 青森, 13 いわき平,
# 21 弥彦, 22 前橋, 23 取手, 24 宇都宮, 25 大宮, 26 西武園, 27 京王閣, 28 立川,
# 31 松戸, 32 千葉,
# 34 川崎, 35 平塚, 36 小田原, 37 伊東, 38 静岡,
# 42 名古屋, 43 岐阜, 44 大垣, 45 豊橋, 46 富山, 47 松阪, 48 四日市,
# 51 福井, 53 奈良, 54 向日町, 55 和歌山, 56 岸和田,
# 61 玉野, 62 広島, 63 防府,
# 71 高松, 73 小松島, 74 高知, 75 松山,
# 81 小倉, 83 久留米, 84 武雄, 85 佐世保, 86 別府, 87 熊本
VENUE_CODES: dict[int, str] = {
    11: "函館", 12: "青森", 13: "いわき平",
    21: "弥彦", 22: "前橋", 23: "取手", 24: "宇都宮", 25: "大宮",
    26: "西武園", 27: "京王閣", 28: "立川",
    31: "松戸", 32: "千葉", 34: "川崎", 35: "平塚",
    36: "小田原", 37: "伊東", 38: "静岡",
    42: "名古屋", 43: "岐阜", 44: "大垣", 45: "豊橋",
    46: "富山", 47: "松阪", 48: "四日市",
    51: "福井", 53: "奈良", 54: "向日町", 55: "和歌山", 56: "岸和田",
    61: "玉野", 62: "広島", 63: "防府",
    71: "高松", 73: "小松島", 74: "高知", 75: "松山",
    81: "小倉", 83: "久留米", 84: "武雄", 85: "佐世保",
    86: "別府", 87: "熊本",
}


class KeirinAPIError(Exception):
    """keirin.jp リクエスト失敗"""

    def __init__(self, url: str, status_code: int, detail: str = ""):
        self.url = url
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{url} returned {status_code}: {detail}")


class KeirinClient:
    """keirin.jp スクレイピングクライアント"""

    def __init__(self) -> None:
        self._delay = float(os.getenv("KEIRIN_REQUEST_DELAY_SEC", "0.8"))
        self._ua = os.getenv(
            "KEIRIN_USER_AGENT",
            "Mozilla/5.0 (keirin-ai research; "
            "+https://github.com/Tower2007/keirin-ai)",
        )
        self._session = self._build_session()
        self._session_initialized = False

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1.0,  # 1, 2, 4, 8, 16 sec
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": self._ua,
            "Accept-Language": "ja,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })
        return s

    def _ensure_session(self) -> None:
        """初回のみ /pc/top にアクセスして Cookie を確立。"""
        if self._session_initialized:
            return
        url = f"{BASE_URL}/pc/top"
        logger.info("Session bootstrap: GET %s", url)
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        self._session_initialized = True

    def reset_session(self) -> None:
        """セッション破棄 → 次の呼び出しで再 bootstrap。

        ネットワーク障害後 / Cookie 失効時に呼ぶ。
        """
        logger.warning("Resetting session (cookies cleared)")
        self._session.cookies.clear()
        self._session_initialized = False

    def _get(self, path: str, params: dict | None = None,
             referer: str | None = None) -> requests.Response:
        self._ensure_session()
        url = f"{BASE_URL}{path}"
        headers = {}
        if referer:
            headers["Referer"] = f"{BASE_URL}{referer}" if referer.startswith("/") else referer
        logger.debug("GET %s params=%s", url, params)
        resp = self._session.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise KeirinAPIError(url, resp.status_code, resp.text[:200])
        time.sleep(self._delay)
        return resp

    # ─── 公開 API ──────────────────────────────────────

    def get_today_race_list(self) -> Any:
        """JSJ048: 当日全場のレース一覧 (純JSON)。"""
        resp = self._get(
            "/pc/json",
            params={
                "type": "JSJ048",
                "kaisaibikbn": "0",
                "kanyusyaflg": "0",
                "dispid": "top",
                "shccp": "",
            },
        )
        return resp.json()

    def get_raceprogram_html(self, place_code: int, race_date: str) -> str:
        """開催プログラムHTML。race_date='YYYY-MM-DD' を 'YYYYMMDD' に変換。

        埋め込み jsonData['PC0201'] にレース番号別 encParaR を含む。
        """
        kst = race_date.replace("-", "")
        resp = self._get(
            "/pc/dfw/dataplaza/guest/raceprogram",
            params={"KCD": place_code, "KST": kst},
            referer="/pc/top",
        )
        return resp.text

    def get_racelist_html(self, enc_para_r: str) -> str:
        """出走表一覧HTML。jsonData['PJ0305'] に全12Rの選手データ。"""
        resp = self._get(
            "/pc/racelist",
            params={"encp": enc_para_r},
            referer="/pc/dfw/dataplaza/guest/raceprogram",
        )
        return resp.text

    def get_racelive_html(self, enc_para_r: str) -> str:
        """レース詳細HTML。jsonData['PC0201']/'PJ0314'/'PJ0315' 等を含む。"""
        resp = self._get(
            "/pc/racelive",
            params={"encp": enc_para_r},
            referer="/pc/racelist",
        )
        return resp.text

    # ─── JSJ JSON エンドポイント (per-race AJAX) ────────────

    def _get_jsj(self, jsj_id: str, enc_para_r: str) -> Any:
        """汎用 JSJ エンドポイント呼び出し (純JSON返却)。

        Referer は /pc/racelive 配下を装う必要がある。
        """
        resp = self._get(
            "/pc/json",
            params={"type": jsj_id, "encp": enc_para_r},
            referer=f"/pc/racelive?encp={enc_para_r}",
        )
        return resp.json()

    def get_race_stats(self, enc_para_r: str) -> Any:
        """JSJ002: 開催1日分の選手成績データ (raceInfo[12] を返す)。

        どのレースの encParaR でも、その日の全12レース分の選手成績が返る。
        """
        return self._get_jsj("JSJ002", enc_para_r)

    def get_race_result(self, enc_para_r: str) -> Any:
        """JSJ012: 1レース分の着順 + 払戻金。"""
        return self._get_jsj("JSJ012", enc_para_r)
