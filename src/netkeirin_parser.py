"""netkeirin オッズ JSON → race_odds.csv 行リスト変換

API レスポンスの list_3..list_9 は kumi_ban (車番文字列) + odds + ... の配列。
keirin.jp 互換の bet_type コード (WH2/WT2/SH2/ST2/W/RH3/RT3) と
kumi_ban フォーマット (X=Y / X-Y / X=Y=Z / X-Y-Z) に変換する。
"""

from __future__ import annotations

# list_X → bet_type マッピング
# 9 -> 7 (要素数の範囲もここに対応)
BET_TYPE_MAP: dict[str, str] = {
    "list_3": "WH2",  # 枠複
    "list_4": "WT2",  # 枠単
    "list_5": "SH2",  # 二車複
    "list_6": "ST2",  # 二車単
    "list_7": "W",    # ワイド (3列目に max_odds あり)
    "list_8": "RH3",  # 三連複
    "list_9": "RT3",  # 三連単
}


def _format_kumi_ban(raw: str, bet_type: str) -> str:
    """netkeirin の "0102" / "010203" を keirin.jp 形式に変換。

    順序型 (ST2/WT2/RT3): X-Y / X-Y-Z
    無順序型 (SH2/WH2/W/RH3): X=Y / X=Y=Z (車番昇順)
    """
    # 2文字ずつに分解 (先頭ゼロ削除)
    cars = [str(int(raw[i:i+2])) for i in range(0, len(raw), 2)]
    if bet_type in ("ST2", "WT2", "RT3"):
        # 順序保持
        sep = "-"
        return sep.join(cars)
    # 無順序: 車番昇順
    cars_sorted = sorted(cars, key=lambda x: int(x))
    sep = "="
    return sep.join(cars_sorted)


def parse_odds_lists(
    place_code: int, race_date: str, race_no: int, odds_data: dict,
) -> list[dict]:
    """odds_data → race_odds.csv 行リスト。

    odds_data: NetkeirinClient.get_odds() の戻り値
    """
    rows: list[dict] = []
    official_dt = odds_data.get("official_dt", "") or ""

    for list_key, bet_type in BET_TYPE_MAP.items():
        items = odds_data.get(list_key, [])
        for item in items:
            if not item or len(item) < 2:
                continue
            raw_kumi = str(item[0])
            try:
                odds = float(item[1]) if item[1] not in ("", None) else None
            except (ValueError, TypeError):
                odds = None
            # ワイドのみ 3列目が max_odds (それ以外は 0.0 で意味なし)
            max_odds = None
            if bet_type == "W" and len(item) >= 3:
                try:
                    max_odds = float(item[2]) if item[2] not in ("", None) else None
                except (ValueError, TypeError):
                    max_odds = None
            popularity = None
            if len(item) >= 4:
                try:
                    popularity = int(item[3]) if item[3] not in ("", None) else None
                except (ValueError, TypeError):
                    popularity = None

            # オッズが 0 / None の組合せはスキップ (未発売 / 売却前)
            if odds is None or odds <= 0:
                continue

            rows.append({
                "race_date": race_date,
                "place_code": place_code,
                "race_no": race_no,
                "bet_type": bet_type,
                "kumi_ban": _format_kumi_ban(raw_kumi, bet_type),
                "odds": odds,
                "max_odds": max_odds,  # ワイドのみ非 NULL
                "popularity": popularity,
                "official_dt": official_dt,
            })
    return rows
