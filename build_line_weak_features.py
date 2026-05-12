"""弱ライン特徴量生成

Phase 6 6a-2: ライン逆引きは諦め、Codex 提案の「信頼度付き弱特徴量」を
race_entries + race_stats から作る。過去 5 年すべてに付与可能。

⚠️ 完全なライン復元ではない。誤った line_id を断定的に入れる代わりに、
probabilistic な弱特徴量として LightGBM の重み調整に任せる方針 (Codex 提案)。

特徴量 (6 種):
- is_probable_line_leader: 脚質・nige_cnt から推定する先頭候補フラグ
- same_region_support_count: 同地区 (8地区) で追/両脚質の支援車数
- has_clear_regional_partner: 同地区支援車がいるか
- is_likely_tanki: 先頭候補 AND 同地区支援なし → 単騎疑い
- same_pref_support_count: 同県の支援車数 (より厳しい指標)
- nige_propensity: nige_cnt / (nige + makuri + sasi + 1) = 逃げ志向

使い方:
  python build_line_weak_features.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR

sys.stdout.reconfigure(encoding="utf-8")

# 競輪 8 地区マッピング (keirinkai 公式地区)
PREFECTURE_TO_REGION = {
    "北海道": "北日本", "青森": "北日本", "秋田": "北日本",
    "岩手": "北日本", "宮城": "北日本", "福島": "北日本",
    "茨城": "関東", "栃木": "関東", "群馬": "関東",
    "埼玉": "関東", "千葉": "関東", "東京": "関東",
    "神奈川": "関東", "山梨": "関東", "新潟": "関東",
    "静岡": "中部", "愛知": "中部", "岐阜": "中部",
    "三重": "中部", "富山": "中部", "石川": "中部",
    "福井": "中部", "長野": "中部",
    "滋賀": "近畿", "京都": "近畿", "大阪": "近畿",
    "兵庫": "近畿", "奈良": "近畿", "和歌山": "近畿",
    "鳥取": "中国", "島根": "中国", "岡山": "中国",
    "広島": "中国", "山口": "中国",
    "徳島": "四国", "香川": "四国", "愛媛": "四国", "高知": "四国",
    "福岡": "九州", "佐賀": "九州", "長崎": "九州",
    "熊本": "九州", "大分": "九州", "宮崎": "九州",
    "鹿児島": "九州", "沖縄": "九州",
}


def normalize_prefecture(s) -> str:
    """'福 岡' → '福岡' のように半角・全角スペース削除。"""
    if pd.isna(s):
        return ""
    return str(s).replace(" ", "").replace("　", "")


def main() -> None:
    t0 = time.time()
    print("[1/4] Loading data...")
    entries = pd.read_csv(
        DATA_DIR / "race_entries.csv", low_memory=False,
        usecols=["race_date", "place_code", "race_no", "car_no",
                 "prefecture", "kyaku"],
    )
    stats = pd.read_csv(
        DATA_DIR / "race_stats.csv", low_memory=False,
        usecols=["race_date", "place_code", "race_no", "car_no",
                 "nige_cnt", "makuri_cnt", "sasi_cnt",
                 "back_cnt", "mark_cnt", "home_tori"],
    )
    print(f"  entries: {len(entries):,}, stats: {len(stats):,}")

    keys = ["race_date", "place_code", "race_no", "car_no"]
    print("[2/4] Merging entries + stats...")
    df = entries.merge(stats, on=keys, how="left")
    print(f"  merged: {len(df):,}")

    print("[3/4] Computing features...")
    # 都道府県の全角/半角スペース削除 + 地区マッピング
    df["prefecture_norm"] = df["prefecture"].apply(normalize_prefecture)
    df["region"] = df["prefecture_norm"].map(PREFECTURE_TO_REGION).fillna("不明")

    # 1. is_probable_line_leader
    # 「逃」型は確実な先頭候補、「両」型は nige>=makuri なら先頭寄り
    df["nige_cnt"] = df["nige_cnt"].fillna(0).astype(int)
    df["makuri_cnt"] = df["makuri_cnt"].fillna(0).astype(int)
    df["sasi_cnt"] = df["sasi_cnt"].fillna(0).astype(int)
    df["is_probable_line_leader"] = (
        (df["kyaku"] == "逃") |
        ((df["kyaku"] == "両") & (df["nige_cnt"] >= df["makuri_cnt"]))
    ).astype(int)

    # 2. same_region_support_count
    # 同地区の「追」「両」脚質を「支援候補」とカウント、自分自身は除外
    df["is_support"] = df["kyaku"].isin(["追", "両"]).astype(int)
    df["region_support_total"] = df.groupby(
        ["race_date", "place_code", "race_no", "region"],
    )["is_support"].transform("sum")
    df["same_region_support_count"] = (
        (df["region_support_total"] - df["is_support"]).clip(lower=0).astype(int)
    )

    # 3. has_clear_regional_partner
    df["has_clear_regional_partner"] = (
        df["same_region_support_count"] > 0
    ).astype(int)

    # 4. is_likely_tanki: 先頭候補 + 同地区支援なし = 単騎疑い
    df["is_likely_tanki"] = (
        (df["is_probable_line_leader"] == 1) &
        (df["has_clear_regional_partner"] == 0)
    ).astype(int)

    # 5. same_pref_support_count (同県のみ、より厳しい指標)
    df["pref_support_total"] = df.groupby(
        ["race_date", "place_code", "race_no", "prefecture_norm"],
    )["is_support"].transform("sum")
    df["same_pref_support_count"] = (
        (df["pref_support_total"] - df["is_support"]).clip(lower=0).astype(int)
    )

    # 6. nige_propensity (脚質傾向、戦績ベース)
    total_attacks = df["nige_cnt"] + df["makuri_cnt"] + df["sasi_cnt"]
    df["nige_propensity"] = df["nige_cnt"] / (total_attacks + 1)

    print("[4/4] Writing output...")
    out_cols = keys + [
        "is_probable_line_leader",
        "same_region_support_count",
        "has_clear_regional_partner",
        "is_likely_tanki",
        "same_pref_support_count",
        "nige_propensity",
    ]
    out = df[out_cols].copy()
    out_path = DATA_DIR / "line_features.csv"
    out.to_csv(out_path, index=False)

    elapsed = time.time() - t0
    print(f"  wrote {len(out):,} rows → {out_path}")
    print(f"  elapsed: {elapsed:.1f}s")

    print("\n=== 特徴量サマリ ===")
    print(f"is_probable_line_leader rate: {df['is_probable_line_leader'].mean():.3f}")
    print(f"has_clear_regional_partner rate: {df['has_clear_regional_partner'].mean():.3f}")
    print(f"is_likely_tanki rate: {df['is_likely_tanki'].mean():.3f}")
    print(f"mean same_region_support_count: {df['same_region_support_count'].mean():.2f}")
    print(f"mean same_pref_support_count: {df['same_pref_support_count'].mean():.2f}")
    print(f"mean nige_propensity: {df['nige_propensity'].mean():.3f}")

    print("\n=== region 分布 ===")
    print(df["region"].value_counts().to_string())


if __name__ == "__main__":
    main()
