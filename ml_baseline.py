"""ML ベースライン: 4ターゲット同時学習 + 5券種バックテスト

学習データ: race_quality.csv で status='normal' の venue-day のみ
分割: 2025-01-01 を境に train / test
ターゲット: y_win (1着) / y_top2 (連対) / y_top3 (3着内) / y_rank (回帰)
特徴量: race_entries (脚質・予想印) + race_stats (戦績) + race_meta (グレード)
バックテスト: 二車複(SH2)/二車単(ST2)/三連複(RH3)/三連単(RT3)/ワイド(W)

⚠️ Phase 6 用途分類 (2026-05-15 Codex 提案、命名は将来 rename 予定):

  PRODUCTION (本番候補):
    - flag 無し           = production_no_odds (本線、market 非依存)
    - --use-line          = base_line_shadow (弱ライン特徴量、shadow 候補)

  DIAGNOSTIC / UPPER BOUND (研究・市場監査用、本番投票判定に使わない):
    - --use-odds          = final_odds_upper_bound (市場の蒸留 upper bound)
    - --use-odds --use-w  = final_odds_w_upper_bound (W 追加 upper bound)
    - --use-odds --use-line = final_odds_line_upper_bound
    - --use-odds --use-mp = final_odds_mp_upper_bound (mispricing、5/13 失敗確認済)

  理由: race_odds.csv の official_dt は発走後 0〜5 分 = 本番では観測不可。
  本番 EV 計算は live snapshot (race_odds_prerace.csv) を別途使う。

使い方:
  python ml_baseline.py                          # production_no_odds (本線)
  python ml_baseline.py --use-odds               # final_odds_upper_bound (研究用)
  python ml_baseline.py --use-line               # base_line_shadow (shadow 候補)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, mean_absolute_error

from src.config import DATA_DIR as DATA

sys.stdout.reconfigure(encoding="utf-8")

# ⚠️ オッズ特徴量 (Phase 6 6a-3 upper-bound 用)
# race_odds.csv の official_dt は st_time + 0〜5 分のため運用リーケージあり。
# ここを ML に入れて測る AUC/ROI は "市場込みの上限性能" であり、本番の
# 期待値とは別物。本番運用は race_odds_prerace.csv (発走 5 分前) を別途使う。
ODDS_NUM_FEATURES = [
    "imp_winprob_st2", "imp_top2prob_sh2",
    "pop_rank_st2_1st", "pop_rank_sh2_min",
    "odds_min_st2_1st", "log_odds_min_st2_1st",
    "n_st2_first", "is_incomplete_st2", "has_odds",
]

# W (ワイド) odds 特徴量 (Phase 6 N2, 2026-05-13)
# Codex 段階投入の第 2 段。y_top3 (3 着内予測) で効く可能性が高い。
# W は kumi_ban に min/max odds の幅がある特殊形式。
W_ODDS_NUM_FEATURES = [
    "imp_top3prob_w",       # W contains-car の 3 着内圏 marginal (sum=1 normalized)
    "pop_rank_w_min",       # W 含む組合せの min popularity
    "w_odds_min_avg",       # W min odds の平均
    "w_odds_range_avg",     # W (max - min) odds の平均 (不確実性指標)
    "has_w_odds",           # W にこの車の組合せが存在するか
]

# 弱ライン特徴量 (Phase 6 6a-2)
# 完全なライン復元は非推奨 (Codex feasibility 調査)。代わりに probabilistic な
# 弱特徴量として LightGBM の重み調整に任せる。過去 5 年すべてに付与可能。
LINE_NUM_FEATURES = [
    "is_probable_line_leader",
    "same_region_support_count",
    "has_clear_regional_partner",
    "is_likely_tanki",
    "same_pref_support_count",
    "nige_propensity",
]

# 市場ミスプライシング特徴量 (Phase 6 N3, 2026-05-13)
# Feature importance 分析でモデルが 80%+ 市場依存と判明。
# 市場 vs 戦績の食い違いを明示的に特徴量化することで、市場非合理性に
# モデルが「異議を唱える」余地を作る (Gemini 提案の方向性)。
# use_odds=True 前提 (内部で pop_rank_st2_1st / imp_winprob_st2 を参照)。
MISPRICING_NUM_FEATURES = [
    "pop_minus_syouritu_rank",         # 正: 戦績の割に人気薄
    "pop_minus_rentairitu2_rank",      # 連対率ベース
    "pop_minus_heikin_tokuten_rank",   # 平均得点ベース
    "imp_winprob_residual",            # 確率レベルの食い違い
    "is_underpriced_stats_top",        # 穴目フラグ: 戦績上位 + 人気下位
    "is_overpriced_market_top",        # 過熱フラグ: 人気上位 + 戦績下位
]

TRAIN_END = "2024-12-31"  # 含む
TEST_START = "2025-01-01"

# 学習に使う特徴量
FEAT_NUM = [
    "sotugyouki", "age", "heikin_tokuten",
    "nige_cnt", "makuri_cnt", "sasi_cnt", "mark_cnt", "back_cnt",
    "home_tori", "st_tori",
    "syouritu", "rentairitu2", "rentairitu3",
    "kyori", "shukai",
    "itioshi_flag", "aisyo_flag",
    "car_no",
    # ─── v2: レース内相対化特徴 ────────────────────
    "syouritu_rank", "rentairitu2_rank", "rentairitu3_rank",
    "heikin_tokuten_rank", "age_rank",
    "syouritu_gap", "rentairitu2_gap", "heikin_tokuten_gap",
    # ─── v2: レース集計特徴 ────────────────────
    "race_n_cars",
    "race_mean_syouritu", "race_std_syouritu",
    "n_nige_in_race", "n_oikomi_in_race", "n_ryouten_in_race",
    "n_S_class_in_race",
]
FEAT_CAT = [
    "place_code",      # 開催場 (43カテゴリ)
    "grade",           # G1/G2/G3/F1/F2
    "kyuhan",          # S級S/S1/S2/A1/A2
    "prev_kyuhan",
    "kyakusitu",       # 逃/追/両 (stats側)
    "kyaku",           # 逃/追/両 (entries側)
    "yosoin",          # 予想印 ◎○△×注
    "kyoso_shurui",    # 競争種類
]

LGB_PARAMS_CLF = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.85,
    "bagging_fraction": 0.85,
    "bagging_freq": 5,
    "verbose": -1,
}
LGB_PARAMS_REG = {
    **LGB_PARAMS_CLF,
    "objective": "regression_l1",
    "metric": "mae",
}


# ─── 1. データロード ─────────────────────────────────────────────────────

def load_data(use_odds: bool = False, use_line: bool = False) -> dict[str, pd.DataFrame]:
    """status='normal' の venue-day だけに絞った各 CSV を返す。"""
    quality = pd.read_csv(DATA / "race_quality.csv")
    normal = quality.loc[quality["status"] == "normal", ["race_date", "place_code"]]
    print(f"normal venue-days: {len(normal):,}")

    meta = pd.read_csv(DATA / "race_meta.csv").merge(
        normal, on=["race_date", "place_code"]
    )
    entries = pd.read_csv(DATA / "race_entries.csv", low_memory=False).merge(
        normal, on=["race_date", "place_code"]
    )
    stats = pd.read_csv(DATA / "race_stats.csv", low_memory=False).merge(
        normal, on=["race_date", "place_code"]
    )
    results = pd.read_csv(DATA / "race_results.csv", low_memory=False).merge(
        normal, on=["race_date", "place_code"]
    )
    payouts = pd.read_csv(DATA / "payouts.csv").merge(
        normal, on=["race_date", "place_code"]
    )

    out = {
        "meta": meta, "entries": entries, "stats": stats,
        "results": results, "payouts": payouts,
    }

    if use_odds:
        odds_path = DATA / "odds_features.csv"
        if not odds_path.exists():
            print(f"⚠️ odds_features.csv not found at {odds_path}")
            print("   先に `python build_odds_features.py` を実行してください")
            sys.exit(1)
        odds = pd.read_csv(odds_path).merge(
            normal, on=["race_date", "place_code"]
        )
        out["odds_features"] = odds
        print(f"odds_features: {len(odds):,}")

    if use_line:
        line_path = DATA / "line_features.csv"
        if not line_path.exists():
            print(f"⚠️ line_features.csv not found at {line_path}")
            print("   先に `python build_line_weak_features.py` を実行してください")
            sys.exit(1)
        line = pd.read_csv(line_path).merge(
            normal, on=["race_date", "place_code"]
        )
        out["line_features"] = line
        print(f"line_features: {len(line):,}")

    print(f"meta: {len(meta):,}, entries: {len(entries):,}, "
          f"stats: {len(stats):,}, results: {len(results):,}, "
          f"payouts: {len(payouts):,}")
    return out


# ─── 2. 特徴量 + ターゲット ────────────────────────────────────────────

def add_race_features(df: pd.DataFrame) -> pd.DataFrame:
    """レース内 rank / gap / 集計特徴を追加 (v2)。

    rank: ascending=False で大きい値が 1位 (上位ほど 1 に近い)。
    gap: race内 max からの差 (リーダーとの差。0 ならリーダー本人)。
    """
    keys = ["race_date", "place_code", "race_no"]
    grp = df.groupby(keys, observed=True, sort=False)

    # 順位 (大きい方が良い指標)
    for col in ["syouritu", "rentairitu2", "rentairitu3", "heikin_tokuten"]:
        df[f"{col}_rank"] = grp[col].rank(ascending=False, method="dense")
    # 年齢 rank (若い順)
    df["age_rank"] = grp["age"].rank(ascending=True, method="dense")

    # gap from race max
    for col in ["syouritu", "rentairitu2", "heikin_tokuten"]:
        df[f"{col}_gap"] = grp[col].transform("max") - df[col]

    # race-level aggregations
    df["race_n_cars"] = grp["car_no"].transform("count")
    df["race_mean_syouritu"] = grp["syouritu"].transform("mean")
    df["race_std_syouritu"] = grp["syouritu"].transform("std").fillna(0)

    # kyaku counts within race (entries.kyaku は 逃/追/両 + NaN)
    kyaku_str = df["kyaku"].astype(str)
    for label, col in [("逃", "n_nige_in_race"),
                       ("追", "n_oikomi_in_race"),
                       ("両", "n_ryouten_in_race")]:
        is_label = (kyaku_str == label).astype(int)
        df[col] = is_label.groupby([df[k] for k in keys], sort=False).transform("sum")

    # S級カウント (kyuhan が S で始まる)
    is_s = df["kyuhan"].astype(str).str.startswith("S").astype(int)
    df["n_S_class_in_race"] = is_s.groupby(
        [df[k] for k in keys], sort=False
    ).transform("sum")

    return df


def _add_mispricing_features(df: pd.DataFrame) -> pd.DataFrame:
    """市場と戦績の食い違いを検出する派生特徴量を追加。

    前提: build_dataset 内で odds_features merge 済 + add_race_features 済
    (= pop_rank_st2_1st / imp_winprob_st2 / syouritu_rank 等が全部揃っている)。

    注意: pop_rank_st2_1st は組合せレベル popularity (1〜72) なので、
    そのままだと stats rank (1〜9) と比較できない。レース内で **車番レベル**の
    rank に正規化 (pop_rank_car_in_race) してから差を取る。
    """
    keys = ["race_date", "place_code", "race_no"]
    grp = df.groupby(keys, observed=True, sort=False)

    # 車番レベル popularity rank (1〜n_cars、低い = 市場人気上位)
    df["pop_rank_car_in_race"] = grp["pop_rank_st2_1st"].rank(
        ascending=True, method="dense",
    )

    # rank diffs: 正 = 戦績の割に市場で人気薄 (穴目候補)
    #             負 = 戦績の割に市場で人気 (過熱気味)
    df["pop_minus_syouritu_rank"] = (
        df["pop_rank_car_in_race"] - df["syouritu_rank"]
    )
    df["pop_minus_rentairitu2_rank"] = (
        df["pop_rank_car_in_race"] - df["rentairitu2_rank"]
    )
    df["pop_minus_heikin_tokuten_rank"] = (
        df["pop_rank_car_in_race"] - df["heikin_tokuten_rank"]
    )

    # 確率残差: imp_winprob - 戦績から推定した確率
    # 戦績推定確率 = syouritu をレース内で正規化 (合計 1)
    syouritu_sum = grp["syouritu"].transform("sum")
    df["stats_implied_prob"] = df["syouritu"] / syouritu_sum.replace(0, 1)
    df["imp_winprob_residual"] = (
        df["imp_winprob_st2"] - df["stats_implied_prob"]
    )

    # フラグ: 穴目候補 (戦績 top2 + 市場 popularity 3 位以下)
    df["is_underpriced_stats_top"] = (
        (df["pop_rank_car_in_race"] >= 3) & (df["syouritu_rank"] <= 2)
    ).astype(int)
    # フラグ: 過熱気味 (市場 top2 + 戦績 5 位以下)
    df["is_overpriced_market_top"] = (
        (df["pop_rank_car_in_race"] <= 2) & (df["syouritu_rank"] >= 5)
    ).astype(int)

    n_under = df["is_underpriced_stats_top"].sum()
    n_over = df["is_overpriced_market_top"].sum()
    print(f"  mispricing flags: underpriced={n_under:,}, overpriced={n_over:,}")
    return df


def build_dataset(data: dict[str, pd.DataFrame],
                  use_mispricing: bool = False) -> pd.DataFrame:
    """車番単位の DataFrame に特徴量とターゲットを結合。"""
    keys = ["race_date", "place_code", "race_no", "car_no"]

    # stats をベースに entries の脚質・予想印 + meta の grade を merge
    df = data["stats"].merge(
        data["entries"][keys + ["kyaku", "yosoin", "itioshi_flag", "aisyo_flag"]],
        on=keys, how="left",
    )
    df = df.merge(
        data["meta"][["race_date", "place_code", "grade"]],
        on=["race_date", "place_code"], how="left",
    )

    # オッズ特徴量を merge (use_odds=True で load_data が用意済)
    if "odds_features" in data:
        # ODDS_NUM_FEATURES と W_ODDS_NUM_FEATURES の全列を一気に merge
        # (use_w_odds は feat_cols 側で取捨選択する設計)
        odds_cols_all = ODDS_NUM_FEATURES + W_ODDS_NUM_FEATURES
        # 存在する列のみ抽出 (旧 odds_features.csv 互換)
        present = [c for c in odds_cols_all
                   if c in data["odds_features"].columns]
        df = df.merge(
            data["odds_features"][keys + present],
            on=keys, how="left",
        )
        # flag 系を 0 埋め (NaN 残しはモデル側で困る)
        df["has_odds"] = df["has_odds"].fillna(0).astype(int)
        df["is_incomplete_st2"] = df["is_incomplete_st2"].fillna(1).astype(int)
        if "has_w_odds" in df.columns:
            df["has_w_odds"] = df["has_w_odds"].fillna(0).astype(int)
        n_with_odds = df["has_odds"].sum()
        print(f"  rows with odds features: {n_with_odds:,} / {len(df):,} "
              f"({n_with_odds / len(df):.1%})")

    # 弱ライン特徴量を merge (use_line=True で load_data が用意済)
    if "line_features" in data:
        df = df.merge(
            data["line_features"][keys + LINE_NUM_FEATURES],
            on=keys, how="left",
        )
        # flag 系は 0 埋め、propensity は中央値で埋める
        for col in ["is_probable_line_leader", "has_clear_regional_partner",
                    "is_likely_tanki"]:
            df[col] = df[col].fillna(0).astype(int)
        for col in ["same_region_support_count", "same_pref_support_count"]:
            df[col] = df[col].fillna(0).astype(int)
        df["nige_propensity"] = df["nige_propensity"].fillna(
            df["nige_propensity"].median()
        )

    # 着順 (results) を結合してターゲット作成
    df = df.merge(
        data["results"][keys + ["tyaku"]],
        on=keys, how="left",
    )

    # tyaku が NaN の行 (失格・落車含む) は学習対象外として
    # ターゲットは 0 扱い（連対しなかった）
    df["y_win"] = (df["tyaku"] == 1).astype(int)
    df["y_top2"] = (df["tyaku"].between(1, 2)).astype(int)
    df["y_top3"] = (df["tyaku"].between(1, 3)).astype(int)
    df["y_rank"] = df["tyaku"].fillna(8)  # 失格は 8 着扱い (回帰)

    # 不正な kyori/shukai の補正
    df["kyori"] = pd.to_numeric(df["kyori"], errors="coerce").fillna(0)
    df["shukai"] = pd.to_numeric(df["shukai"], errors="coerce").fillna(0)

    # v2: レース内相対化 + 集計特徴 (category 化前に行う)
    df = add_race_features(df)

    # 市場ミスプライシング特徴 (use_mispricing=True かつ odds 必須)
    # add_race_features の後で syouritu_rank 等が揃っているので、ここで計算
    if use_mispricing:
        if "odds_features" not in data:
            raise ValueError("use_mispricing requires use_odds=True")
        df = _add_mispricing_features(df)

    # カテゴリ列を category 型に (race-level 計算後に変換)
    for c in FEAT_CAT:
        if c in df.columns:
            df[c] = df[c].astype("category")

    print(f"dataset: {len(df):,} rows, "
          f"win rate: {df['y_win'].mean():.3f}, "
          f"top2 rate: {df['y_top2'].mean():.3f}, "
          f"top3 rate: {df['y_top3'].mean():.3f}")
    return df


def split_train_test(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    train = df[df["race_date"] <= TRAIN_END].copy()
    test = df[df["race_date"] >= TEST_START].copy()
    print(f"train: {len(train):,} ({train['race_date'].min()} 〜 {train['race_date'].max()})")
    print(f"test:  {len(test):,} ({test['race_date'].min()} 〜 {test['race_date'].max()})")
    return train, test


# ─── 3. 学習 ─────────────────────────────────────────────────────────

def train_one(
    train: pd.DataFrame, test: pd.DataFrame,
    target: str, params: dict, feat_cols: list[str],
) -> tuple[lgb.Booster, np.ndarray, float]:
    X_train = train[feat_cols]
    y_train = train[target]
    X_test = test[feat_cols]
    y_test = test[target]

    cat_in_feat = [c for c in FEAT_CAT if c in feat_cols]
    train_set = lgb.Dataset(X_train, y_train, categorical_feature=cat_in_feat)
    valid_set = lgb.Dataset(X_test, y_test, reference=train_set,
                             categorical_feature=cat_in_feat)
    model = lgb.train(
        params, train_set,
        num_boost_round=800,
        valid_sets=[valid_set],
        valid_names=["test"],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
    )
    pred = model.predict(X_test)
    if params["objective"] == "binary":
        score = roc_auc_score(y_test, pred)
        print(f"  {target}: AUC = {score:.4f} (best iter {model.best_iteration})")
    else:
        score = mean_absolute_error(y_test, pred)
        print(f"  {target}: MAE = {score:.4f} (best iter {model.best_iteration})")
    return model, pred, score


def train_all_models(
    train: pd.DataFrame, test: pd.DataFrame,
    feat_cols: list[str] | None = None,
) -> dict[str, np.ndarray]:
    if feat_cols is None:
        feat_cols = FEAT_NUM + FEAT_CAT
    print(f"features ({len(feat_cols)}): {feat_cols}")
    print()
    print("=== Training ===")
    preds: dict[str, np.ndarray] = {}
    for target in ["y_win", "y_top2", "y_top3"]:
        _, p, _ = train_one(train, test, target, LGB_PARAMS_CLF, feat_cols)
        preds[target] = p
    _, p, _ = train_one(train, test, "y_rank", LGB_PARAMS_REG, feat_cols)
    preds["y_rank"] = p
    return preds


# ─── 4. バックテスト ──────────────────────────────────────────────────

def make_picks(test: pd.DataFrame, preds: dict[str, np.ndarray]) -> pd.DataFrame:
    """各レースの券種別おすすめ車番 + confidence スコアを出す。"""
    df = test[["race_date", "place_code", "race_no", "car_no"]].copy()
    df["p_win"] = preds["y_win"]
    df["p_top2"] = preds["y_top2"]
    df["p_top3"] = preds["y_top3"]

    rows = []
    for (rd, pc, rn), g in df.groupby(["race_date", "place_code", "race_no"], sort=False):
        g = g.sort_values("p_win", ascending=False).reset_index(drop=True)
        win_order = g["car_no"].tolist()  # P(win) 降順

        g2 = g.sort_values("p_top2", ascending=False)
        top2 = g2["car_no"].head(2).tolist()

        g3 = g.sort_values("p_top3", ascending=False)
        top3 = g3["car_no"].head(3).tolist()

        # 二車複 (unordered): "X=Y" with X<Y
        ni_huku = "=".join(map(str, sorted(top2)))
        # 二車単 (ordered): P(win) 1位 と P(top2) 1位 (重複したら2位)
        first = win_order[0]
        second_candidate = [c for c in g2["car_no"].tolist() if c != first][:1]
        ni_tan_pair = [first] + second_candidate
        ni_tan = "-".join(map(str, ni_tan_pair))

        # 三連複: 上位3 unordered (sorted ascending)
        san_huku = "=".join(map(str, sorted(top3)))

        # 三連単 (ordered): P(win) 順
        san_tan = "-".join(map(str, win_order[:3]))

        # ワイド: top3 のうち 3組み合わせ
        wide_pairs = [
            "=".join(map(str, sorted([top3[0], top3[1]]))),
            "=".join(map(str, sorted([top3[0], top3[2]]))),
            "=".join(map(str, sorted([top3[1], top3[2]]))),
        ]

        # confidence: 上位N車の確率合計 (大きいほど自信あり)
        conf_ni_huku = float(g2["p_top2"].head(2).sum())
        conf_san_huku = float(g3["p_top3"].head(3).sum())
        conf_ordered = float(g["p_win"].head(3).sum())  # 順序付き券種共通

        rows.append({
            "race_date": rd, "place_code": pc, "race_no": rn,
            "SH2": ni_huku, "ST2": ni_tan,
            "RH3": san_huku, "RT3": san_tan,
            "W1": wide_pairs[0], "W2": wide_pairs[1], "W3": wide_pairs[2],
            "conf_SH2": conf_ni_huku,
            "conf_ST2": conf_ordered,
            "conf_RH3": conf_san_huku,
            "conf_RT3": conf_ordered,
            "conf_W": conf_san_huku,
        })
    return pd.DataFrame(rows)


def backtest(picks: pd.DataFrame, payouts: pd.DataFrame) -> pd.DataFrame:
    """各券種ごとの ROI/的中率/総払戻を集計。"""
    # payouts を (race_date, place_code, race_no, bet_type, kumi_ban) → payout 辞書化
    payouts = payouts.copy()
    payouts["key"] = (
        payouts["race_date"] + "|" + payouts["place_code"].astype(str) + "|"
        + payouts["race_no"].astype(str) + "|" + payouts["bet_type"] + "|"
        + payouts["kumi_ban"].astype(str)
    )
    # 同じ kumi_ban が複数行あるケース (W) — 最大値を使う
    pmap = payouts.groupby("key")["payout"].max().to_dict()

    results = []

    def lookup(rd, pc, rn, bt, kb):
        key = f"{rd}|{pc}|{rn}|{bt}|{kb}"
        return pmap.get(key, 0)

    for bt_col, bt_code, label, n_bets in [
        ("SH2", "SH2", "二車複",   1),
        ("ST2", "ST2", "二車単",   1),
        ("RH3", "RH3", "三連複",   1),
        ("RT3", "RT3", "三連単",   1),
    ]:
        payouts_won = picks.apply(
            lambda r: lookup(r["race_date"], r["place_code"], r["race_no"], bt_code, r[bt_col]),
            axis=1,
        )
        n_races = len(picks)
        n_hits = (payouts_won > 0).sum()
        total_payout = payouts_won.sum()
        total_cost = n_races * 100 * n_bets
        results.append({
            "bet_type": label,
            "n_bets": n_races * n_bets,
            "n_hits": int(n_hits),
            "hit_rate": round(n_hits / n_races, 4),
            "total_payout": int(total_payout),
            "total_cost": total_cost,
            "roi": round(total_payout / total_cost - 1, 4) if total_cost else None,
        })

    # ワイド (W) — 1レースあたり 3組合せ
    w_won = []
    for _, r in picks.iterrows():
        for w_col in ["W1", "W2", "W3"]:
            v = lookup(r["race_date"], r["place_code"], r["race_no"], "W", r[w_col])
            w_won.append(v)
    w_won = np.array(w_won)
    n_w_bets = len(picks) * 3
    results.append({
        "bet_type": "ワイド",
        "n_bets": n_w_bets,
        "n_hits": int((w_won > 0).sum()),
        "hit_rate": round((w_won > 0).sum() / n_w_bets, 4),
        "total_payout": int(w_won.sum()),
        "total_cost": n_w_bets * 100,
        "roi": round(w_won.sum() / (n_w_bets * 100) - 1, 4) if n_w_bets else None,
    })

    return pd.DataFrame(results)


def backtest_thresholds(
    picks: pd.DataFrame, payouts: pd.DataFrame,
    thresholds: list[float] = (1.0, 0.50, 0.25, 0.10, 0.05),
) -> pd.DataFrame:
    """confidence 上位 X% のレースだけ買った場合の ROI を集計。

    券種ごとに conf_<bet> 列でソートし、上位 thresh % を抽出して ROI 計算。
    """
    payouts = payouts.copy()
    payouts["key"] = (
        payouts["race_date"] + "|" + payouts["place_code"].astype(str) + "|"
        + payouts["race_no"].astype(str) + "|" + payouts["bet_type"] + "|"
        + payouts["kumi_ban"].astype(str)
    )
    pmap = payouts.groupby("key")["payout"].max().to_dict()

    def lookup(rd, pc, rn, bt, kb):
        return pmap.get(f"{rd}|{pc}|{rn}|{bt}|{kb}", 0)

    results = []
    n_total = len(picks)

    # 4 メイン券種 (1 ticket / race)
    bet_specs = [
        ("二車複", "SH2", "conf_SH2"),
        ("二車単", "ST2", "conf_ST2"),
        ("三連複", "RH3", "conf_RH3"),
        ("三連単", "RT3", "conf_RT3"),
    ]
    for label, bt_code, conf_col in bet_specs:
        sorted_picks = picks.sort_values(conf_col, ascending=False)
        for thresh in thresholds:
            n_top = max(1, int(n_total * thresh))
            sub = sorted_picks.head(n_top)
            won = sub.apply(
                lambda r: lookup(r["race_date"], r["place_code"], r["race_no"],
                                 bt_code, r[bt_code]),
                axis=1,
            )
            cost = len(sub) * 100
            payout = int(won.sum())
            n_hits = int((won > 0).sum())
            results.append({
                "bet_type": label,
                "threshold_pct": int(thresh * 100),
                "n_races": len(sub),
                "n_hits": n_hits,
                "hit_rate": round(n_hits / len(sub), 4),
                "total_payout": payout,
                "total_cost": cost,
                "roi": round(payout / cost - 1, 4),
            })

    # ワイド (1 race = 3 tickets)
    sorted_picks = picks.sort_values("conf_W", ascending=False)
    for thresh in thresholds:
        n_top = max(1, int(n_total * thresh))
        sub = sorted_picks.head(n_top)
        all_won = []
        for _, r in sub.iterrows():
            for w_col in ["W1", "W2", "W3"]:
                all_won.append(lookup(r["race_date"], r["place_code"],
                                       r["race_no"], "W", r[w_col]))
        all_won = np.array(all_won)
        cost = len(sub) * 3 * 100
        payout = int(all_won.sum())
        n_hits = int((all_won > 0).sum())
        results.append({
            "bet_type": "ワイド",
            "threshold_pct": int(thresh * 100),
            "n_races": len(sub),
            "n_hits": n_hits,
            "hit_rate": round(n_hits / (len(sub) * 3), 4),
            "total_payout": payout,
            "total_cost": cost,
            "roi": round(payout / cost - 1, 4),
        })

    return pd.DataFrame(results)


# ─── 5. メイン ────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-odds", action="store_true",
                    help="オッズ特徴量を追加 (odds_features.csv を merge)。"
                         "Phase 6 6a-3 upper-bound 検証用。")
    ap.add_argument("--use-line", action="store_true",
                    help="弱ライン特徴量を追加 (line_features.csv を merge)。"
                         "Phase 6 6a-2 ライン構造の弱信号。")
    ap.add_argument("--use-mispricing", action="store_true",
                    help="市場ミスプライシング派生特徴量を追加 (要 --use-odds)。"
                         "Phase 6 N3 市場 vs 戦績の食い違い検出。")
    ap.add_argument("--use-w-odds", action="store_true",
                    help="W (ワイド) odds 特徴量を追加 (要 --use-odds)。"
                         "Phase 6 N2 段階投入の第 2 段。")
    args = ap.parse_args()

    if args.use_mispricing and not args.use_odds:
        ap.error("--use-mispricing requires --use-odds")
    if args.use_w_odds and not args.use_odds:
        ap.error("--use-w-odds requires --use-odds")

    t0 = time.time()
    print("=" * 70)
    print("ML Baseline: 4 targets + 5 ticket types backtest")
    print(f"  use_odds = {args.use_odds}, use_w_odds = {args.use_w_odds}, "
          f"use_line = {args.use_line}, "
          f"use_mispricing = {args.use_mispricing}")
    print("=" * 70)

    print("\n[1/4] Loading data...")
    data = load_data(use_odds=args.use_odds, use_line=args.use_line)

    print("\n[2/4] Building dataset...")
    df = build_dataset(data, use_mispricing=args.use_mispricing)
    train, test = split_train_test(df)

    print("\n[3/4] Training models...")
    feat_cols = list(FEAT_NUM)
    if args.use_odds:
        feat_cols = feat_cols + ODDS_NUM_FEATURES
    if args.use_w_odds:
        feat_cols = feat_cols + W_ODDS_NUM_FEATURES
    if args.use_line:
        feat_cols = feat_cols + LINE_NUM_FEATURES
    if args.use_mispricing:
        feat_cols = feat_cols + MISPRICING_NUM_FEATURES
    feat_cols = feat_cols + FEAT_CAT
    preds = train_all_models(train, test, feat_cols=feat_cols)

    print("\n[4/4] Backtest...")
    picks = make_picks(test, preds)
    print(f"  picks generated for {len(picks):,} races")

    # 出力ファイル名に suffix
    suffix_parts = []
    if args.use_odds:
        suffix_parts.append("odds")
    if args.use_w_odds:
        suffix_parts.append("w")
    if args.use_line:
        suffix_parts.append("line")
    if args.use_mispricing:
        suffix_parts.append("mp")
    suffix = f"_with_{'_'.join(suffix_parts)}" if suffix_parts else ""
    picks_out = DATA / f"ml_picks{suffix}.csv"
    picks.to_csv(picks_out, index=False)
    print(f"  picks saved to {picks_out}")

    bt = backtest(picks, data["payouts"])
    bt_thresh = backtest_thresholds(picks, data["payouts"])

    print()
    print("=" * 70)
    print("=== Backtest: 全レース購入 (1ticket/race) ===")
    print("=" * 70)
    print(bt.to_string(index=False))

    print()
    print("=" * 70)
    print("=== Backtest: confidence 上位X% のみ購入 ===")
    print("=" * 70)
    # 券種別 pivot で見やすく
    pivot = bt_thresh.pivot(
        index="bet_type", columns="threshold_pct",
        values="roi"
    ).round(4)
    pivot.columns = [f"top{c}%_ROI" for c in pivot.columns]
    print(pivot.to_string())
    print()
    print("ROI: -1.0 = 全損, 0 = 損益±0, +0.5 = +50% (元手 100円→150円)")
    print("controlled rate (公営競技控除): -25% 程度が損益分岐の目安")

    # 結果保存 (suffix で odds あり/なし区別)
    out = DATA / f"ml_baseline_v2_result{suffix}.csv"
    bt.to_csv(out, index=False)
    out_th = DATA / f"ml_threshold_result{suffix}.csv"
    bt_thresh.to_csv(out_th, index=False)
    print(f"\nSaved: {out}")
    print(f"Saved: {out_th}")
    print(f"Total runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
