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
