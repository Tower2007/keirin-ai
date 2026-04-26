"""HTML 内 jsonData 抽出 + CSV 用フラット dict への変換

keirin.jp は HTML に `jsonData['PJ0305'] = {...};` の形で
JSONデータを埋め込んでいるため、正規表現で抽出する。
"""

import json
import re
import unicodedata
from typing import Any


def _zen2han(s: str | None) -> str | None:
    """全角英数字を半角に正規化（日本語かなはそのまま）。"""
    if s is None:
        return None
    return unicodedata.normalize("NFKC", s)


def _clean_str(v: Any) -> str | None:
    """空文字・None を None に統一、全角を半角化。"""
    if v is None or v == "" or v == "null":
        return None
    s = _zen2han(str(v).strip())
    # 「上遠野　拓馬」などの全角スペースは温存
    return s if s else None


def _clean_int(v: Any) -> int | None:
    """int 変換。失敗時 None。"""
    if v is None or v == "" or v == "null":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _clean_float(v: Any) -> float | None:
    """float 変換。失敗時 None。"""
    s = _clean_str(v)
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ─── HTML から jsonData を抽出 ──────────────────────────

def extract_json_data(html: str, key: str) -> dict | None:
    """HTML 内の `jsonData['KEY'] = {...};` または `jsonData["KEY"] = {...};` を抽出。

    存在しない or パース失敗時は None。
    """
    # 単引用符・二重引用符両対応
    pattern = re.compile(
        r"jsonData\[['\"]" + re.escape(key) + r"['\"]\]\s*=\s*(\{.*?\});",
        re.DOTALL,
    )
    m = pattern.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ─── PC0201 (開催プログラム) ────────────────────────────

def parse_program_meta(
    place_code: int, race_date: str, pc0201: dict,
) -> dict:
    """PC0201 から開催全体のメタ情報 + レース番号別 encParaR を抽出。

    戻り値: {"meta": {...}, "races": [{"race_no": 1, "enc_para_r": "..."}, ...]}
    """
    data = pc0201.get("C0201data", {})
    meta = {
        "race_date": race_date,
        "place_code": place_code,
        "place_name": _clean_str(data.get("joName")),
        "race_name": _clean_str(data.get("raceName")),
        "grade": _clean_str(data.get("imgGradeAlt")),  # G1/G2/G3/F1/F2
        "sel_kaisai": _clean_str(data.get("selKaisai")),  # YYYYMMDD
        "enc_para_k": _clean_str(data.get("encSelParaK")),
        "enc_para_s": _clean_str(data.get("encParaS")),
    }
    races = []
    for idx, race in enumerate(data.get("C0201race", []), start=1):
        races.append({
            "race_no": idx,
            "enc_para_r": _clean_str(race.get("encParaR")),
            "rcv_kekka": _clean_int(race.get("rcvKekka")),
            "rcv_refund": _clean_int(race.get("rcvRefund")),
            "rcv_odds": _clean_int(race.get("rcvOdds")),
            "race_end": race.get("flgRaceEnd", False),
        })
    return {"meta": meta, "races": races}


# ─── PJ0305 (出走表一覧) ────────────────────────────────

def parse_race_entries(
    place_code: int, race_date: str, pj0305: dict,
) -> list[dict]:
    """PJ0305 → race_entries 行リスト (全12R分の選手データ)。"""
    rows = []
    for race in pj0305.get("rInfo", []):
        race_no = race.get("raceNo")
        race_meta = {
            "syumoku": _clean_str(race.get("syumoku")),
            "den_time": _clean_str(race.get("denTime")),
            "st_time": _clean_str(race.get("stTime")),
            "line_type": _clean_str(race.get("line")),
            "narabi_flg": _clean_int(race.get("narabiFlg")),
            "ozz_flg": _clean_int(race.get("ozzFlg")),
            "result_flg": _clean_int(race.get("resultFlg")),
        }
        for player in race.get("sInfo", []):
            rows.append({
                "race_date": race_date,
                "place_code": place_code,
                "race_no": race_no,
                "car_no": _clean_int(player.get("syaban")),
                "player_code": _clean_str(player.get("senNo")),
                "player_name": _clean_str(player.get("senName")),
                "prefecture": _clean_str(player.get("huken")),
                "kyaku": _clean_str(player.get("kyaku")),  # 逃/追/両
                "yosoin": _clean_str(player.get("Yosoin")),  # 予想印 ◎○△×注
                "itioshi_flag": _clean_int(player.get("itiosiFlag")),
                "aisyo_flag": _clean_int(player.get("aisyoFlag")),
                "assen": _clean_str(player.get("assen")),  # (補充) 等
                **race_meta,
            })
    return rows


def parse_race_lines(
    place_code: int, race_date: str, pj0305: dict,
) -> list[dict]:
    """PJ0305 → race_lines 行リスト (ライン情報)。

    nInfo は narabiX (ライン内位置) と narabiY (ライン番号) を持つ。
    """
    rows = []
    for race in pj0305.get("rInfo", []):
        race_no = race.get("raceNo")
        for n in race.get("nInfo", []):
            rows.append({
                "race_date": race_date,
                "place_code": place_code,
                "race_no": race_no,
                "car_no": _clean_int(n.get("syaban")),
                "narabi_x": _clean_int(n.get("narabiX")),
                "narabi_y": _clean_int(n.get("narabiY")),
            })
    return rows


# ─── JSJ002 (選手成績データ、全12R分まとめて) ─────────────

def parse_race_stats(
    place_code: int, race_date: str, jsj002: dict,
) -> list[dict]:
    """JSJ002 → race_stats 行リスト (全レース x 全選手)。"""
    rows = []
    for race in jsj002.get("raceInfo", []):
        race_no = _clean_int(race.get("raceNo"))
        race_meta = {
            "shumoku_name": _clean_str(race.get("shumokuName")),
            "kyori": _clean_int(race.get("kyori")),
            "shukai": _clean_int(race.get("shukai")),
            "kyoso_shurui": _clean_str(race.get("kyosoShurui")),
            "yudo_sensyu": _clean_str(race.get("yudoSensyuName")),
        }
        for p in race.get("sensyuTypeInfo", []):
            rows.append({
                "race_date": race_date,
                "place_code": place_code,
                "race_no": race_no,
                "car_no": _clean_int(p.get("syaban")),
                "player_code": _clean_str(p.get("sensyuRegistNo")),
                "player_name": _clean_str(p.get("sensyuName")),
                "prefecture": _clean_str(p.get("huKen")),
                "kyuhan": _clean_str(p.get("kyuhan")),  # S級S/S1/S2/A1/A2 等
                "prev_kyuhan": _clean_str(p.get("prevKyuhan")),
                "kyakusitu": _clean_str(p.get("kyakusitu")),  # 逃/追/両
                "sotugyouki": _clean_int(p.get("sotugyouki")),
                "age": _clean_int(p.get("age")),
                "heikin_tokuten": _clean_float(p.get("heikinTokuten")),
                "nige_cnt": _clean_int(p.get("nigeCnt")),
                "makuri_cnt": _clean_int(p.get("makuriCnt")),
                "sasi_cnt": _clean_int(p.get("sasiCnt")),
                "mark_cnt": _clean_int(p.get("markCnt")),
                "back_cnt": _clean_int(p.get("backCnt")),
                "home_tori": _clean_int(p.get("homeTori")),
                "st_tori": _clean_int(p.get("stTori")),
                "syouritu": _clean_float(p.get("syouritu")),
                "rentairitu2": _clean_float(p.get("rentairitu2")),
                "rentairitu3": _clean_float(p.get("rentairitu3")),
                "ketujyou_tuika_hojyu": _clean_str(p.get("ketujyouTuikaHojyu")),
                **race_meta,
            })
    return rows


# ─── JSJ012 (着順 + 払戻金) ──────────────────────────────

def parse_race_results(
    place_code: int, race_date: str, race_no: int, jsj012: dict,
) -> list[dict]:
    """JSJ012 → race_results 行リスト (1レース分)。"""
    rows = []
    for r in jsj012.get("tyakujyunItemSubData", []):
        rows.append({
            "race_date": race_date,
            "place_code": place_code,
            "race_no": race_no,
            "tyaku": _clean_int(r.get("tyaku")),  # 着順
            "car_no": _clean_int(r.get("syaban")),
            "player_code": _clean_str(r.get("sensyuRegistNo")),
            "player_name": _clean_str(r.get("sensyuName")),
            "age": _clean_int(r.get("age")),
            "prefecture": _clean_str(r.get("huken")),
            "sotugyouki": _clean_int(r.get("sotugyouki")),
            "kyuhan": _clean_str(r.get("kyuhan")),
            "tyakusa": _clean_str(r.get("tyakusa")),  # 着差
            "agari": _clean_float(r.get("agari")),  # 上り
            "kimarite": _clean_str(r.get("kimarite")),  # 決まり手
            "bh": _clean_str(r.get("BH")),  # B(バック取り)/H(ホーム取り)
            "in_line_jyuni": _clean_str(r.get("inLineJyuni")),
            "tenki": _clean_str(jsj012.get("tenki")),  # 天気
            "husoku": _clean_str(jsj012.get("husoku")),  # 風速
        })
    return rows


# 払戻券種コード → 名称
_BET_TYPE_MAP = {
    "WH2": "枠複",
    "WT2": "枠単",
    "SH2": "二車複",
    "ST2": "二車単",
    "RH3": "三連複",
    "RT3": "三連単",
    "W": "ワイド",
}


def parse_payouts(
    place_code: int, race_date: str, race_no: int, jsj012: dict,
) -> list[dict]:
    """JSJ012 → payouts 行リスト (1レース x 7券種 x 当選組合せ)。"""
    rows = []
    hgsd = jsj012.get("haraiGakuSubData", {})
    for code, name in _BET_TYPE_MAP.items():
        items = hgsd.get(f"{code}HaraiGakuDispItemSubData", [])
        for item in items:
            kumi_ban = _clean_str(item.get("kumiBan"))
            harai_gaku_raw = _clean_str(item.get("haraiGaku"))
            # 「【未発売】」「-」等は payout NULL
            if not item.get("kumiDispFlg") or harai_gaku_raw in (None, "-", "【未発売】"):
                continue
            # カンマ区切りを除去して数値化: "8,460" → 8460
            harai_gaku = _clean_int(harai_gaku_raw.replace(",", "")) if harai_gaku_raw else None
            ninki_raw = _clean_str(item.get("ninki"))
            ninki = _clean_int(ninki_raw.strip("()")) if ninki_raw else None

            rows.append({
                "race_date": race_date,
                "place_code": place_code,
                "race_no": race_no,
                "bet_type": code,
                "bet_name": name,
                "kumi_ban": kumi_ban,
                "payout": harai_gaku,
                "popularity": ninki,
            })
    return rows
