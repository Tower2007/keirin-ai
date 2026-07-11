"""週次 ML 自動再学習 + 採用品質ゲート (production_no_odds 系)

毎週 1 回 (タスクスケジューラ想定) 実行し、以下を自動で行う:

  1. 最新データで production_no_odds 構成 (ml_baseline.py と同じ特徴量・パラメータ)
     のモデル 4 本 (y_win / y_top2 / y_top3 / y_rank) を学習
       - 学習期間: 全期間 〜 (OOS 開始前日)
       - 内部 val:  学習期間の末尾 10% (early stopping 用、auto の train_production 方式)
  2. 直近 OOS 期間 (デフォルト 8 週間、学習に一切使わない) で
     y_win の AUC / logloss / calibration error (ECE) を算出
  3. 旧モデルのメタ (ml_weekly_meta.json) と比較して OK / WARN / NG 判定
       - NG: 旧モデル維持。rejected メタ保存 + 履歴 append + exit 0 (タスクは正常終了)
       - OK/WARN: 旧メタ/旧モデルを .prev にバックアップ → 新モデル採用
         → ml_pred_probs.csv / ml_picks.csv を差分再生成
         → ev_prerace_pipeline.py で ev_prerace_merged.csv を更新
  4. 判定・指標を retrain_history.csv に毎回 append (塩漬け監視用)

採用ゲートの閾値設計 (Auto_racing_AI/ml/train_production.py の _should_adopt を競輪向けに調整):
  auto は「同一 val 定義での新旧比較」なので AUC 低下即 NG の厳格設計だが、
  本スクリプトの OOS 窓は毎週 1 週間ずつスライドする (新旧メタは別期間の指標)。
  期間差由来のノイズを許容するため、NG 閾値に余裕を持たせ WARN 帯を挟む:
    - AUC 絶対床 (AUC_FLOOR=0.65) 割れ        → NG (壊れたモデルの初回採用も防ぐ)
    - OOS AUC が旧比 -0.010 超低下             → NG  (-0.003 超で WARN)
    - OOS logloss が旧比 +3% 超悪化            → NG  (+1% 超で WARN)
    - OOS ECE が旧比 +0.010 超悪化             → NG  (+0.005 超で WARN)
  旧メタ無し (初回) は床チェックのみで採用。--force でゲート無視 (床含む) 強制採用。

学習コスト方針: ml_baseline.py のフル実行実測が約 245 秒 (logs/ml_baseline_phaseB.log)
のため、学習期間の短縮や n_estimators 削減は行わず baseline と同条件
(num_boost_round=800, lr=0.05, early stopping 50) を踏襲する。30 分制約に対し十分余裕。

probs/picks の再生成範囲 (差分方式):
  既存 ml_pred_probs.csv の最終日を regen_start とし (最終日は取りこぼし防止のため
  再生成で置換)、昨日までを新モデルで追記する。全量を新モデルで再生成すると
  学習期間 (〜OOS 開始前日) と重なる過去日の確率が in-sample になり、Phase C 再判定
  (確率×直前オッズ) が楽観バイアスを持つため、差分方式が正攻法であり軽量化のためではない。
  同じ理由で regen_start が学習期間に食い込む場合は train_end+1 に切り上げる。

race_quality.csv について:
  本スクリプトは race_quality.csv を **書き換えない** (管理 PJ 側の禁止事項)。
  race_quality.csv の最終日以降の venue-day は build_race_quality.py と同一ロジックで
  in-memory に品質判定し、status='normal' の日だけ学習/生成対象に加える。

使い方:
  python weekly_retrain.py            # 通常 (週次タスク)
  python weekly_retrain.py --force    # ゲート無視で強制採用
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, log_loss, mean_absolute_error

from src.config import DATA_DIR as DATA
import ml_baseline as mlb  # 特徴量定義・dataset 構築・picks 生成を本線と完全共有

ROOT = Path(__file__).resolve().parent

# 成果物の置き場 (D:\keirin-ai-data\weekly_model\)
MODEL_DIR = DATA / "weekly_model"
META_PATH = MODEL_DIR / "ml_weekly_meta.json"
REJECTED_PATH = MODEL_DIR / "ml_weekly_meta.rejected.json"
HISTORY_PATH = DATA / "retrain_history.csv"

PROBS_PATH = DATA / "ml_pred_probs.csv"
PICKS_PATH = DATA / "ml_picks.csv"

KEYS = ["race_date", "place_code", "race_no"]

# 学習ターゲット (ml_baseline と同一)
CLF_TARGETS = ["y_win", "y_top2", "y_top3"]
REG_TARGET = "y_rank"

# ─── 採用ゲート閾値 (設計根拠は docstring 参照) ─────────────────────────
OOS_WEEKS_DEFAULT = 8
AUC_FLOOR = 0.65            # y_win OOS AUC の絶対床 (壊れモデル検知)
AUC_DROP_NG = 0.010         # 旧比 AUC 低下が これを超えたら NG
AUC_DROP_WARN = 0.003       # 〃 WARN
LOGLOSS_RATIO_NG = 1.03     # 旧比 logloss 悪化率 NG
LOGLOSS_RATIO_WARN = 1.01   # 〃 WARN
ECE_DELTA_NG = 0.010        # 旧比 ECE 悪化幅 NG
ECE_DELTA_WARN = 0.005      # 〃 WARN


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ─── 1. データロード (race_quality を in-memory 拡張) ───────────────────

def _status_of(row: pd.Series) -> str:
    """build_race_quality.py の status_of と同一ロジック (同スクリプト参照)。"""
    if row["is_canceled"]:
        return "canceled"
    if row["n_planned"] == 0:
        return "no_entries"
    if row["n_results"] == 0:
        return "all_canceled"
    if row["n_results"] < row["n_planned"]:
        return "partial_canceled"
    if row["n_payouts"] < row["n_results"]:
        return "payout_missing"
    return "normal"


def load_data_extended() -> tuple[dict[str, pd.DataFrame], str]:
    """ml_baseline.load_data 相当 + race_quality.csv 以降の日を in-memory 品質判定。

    race_quality.csv は書き換えず、最終日より後の venue-day を
    build_race_quality.py と同じ集計で判定して normal リストに加える。
    返り値: (data dict, 利用可能な最終 race_date)
    """
    quality = pd.read_csv(DATA / "race_quality.csv")
    q_max = str(quality["race_date"].max())

    meta_all = pd.read_csv(DATA / "race_meta.csv")
    entries_all = pd.read_csv(DATA / "race_entries.csv", low_memory=False)
    stats_all = pd.read_csv(DATA / "race_stats.csv", low_memory=False)
    results_all = pd.read_csv(DATA / "race_results.csv", low_memory=False)

    normal = quality.loc[
        quality["status"] == "normal", ["race_date", "place_code"]
    ].copy()

    # race_quality.csv 最終日より後を in-memory 判定
    recent_meta = meta_all[meta_all["race_date"] > q_max].copy()
    if len(recent_meta):
        ent_r = entries_all[entries_all["race_date"] > q_max]
        res_r = results_all[results_all["race_date"] > q_max]
        pay_r = pd.read_csv(
            DATA / "payouts.csv",
            usecols=["race_date", "place_code", "race_no"],
        )
        pay_r = pay_r[pay_r["race_date"] > q_max]

        n_planned = (
            ent_r.groupby(["race_date", "place_code"])["race_no"]
            .nunique().rename("n_planned")
        )
        n_results = (
            res_r[res_r["tyaku"].notna()]
            .groupby(["race_date", "place_code"])["race_no"]
            .nunique().rename("n_results")
        )
        n_payouts = (
            pay_r.groupby(["race_date", "place_code"])["race_no"]
            .nunique().rename("n_payouts")
        )
        if "is_canceled" not in recent_meta.columns:
            recent_meta["is_canceled"] = False
        else:
            recent_meta["is_canceled"] = (
                recent_meta["is_canceled"].astype(str).str.lower()
                .isin(["true", "1"])
            )
        rq = recent_meta[["race_date", "place_code", "is_canceled"]].copy()
        rq = rq.merge(n_planned, on=["race_date", "place_code"], how="left")
        rq = rq.merge(n_results, on=["race_date", "place_code"], how="left")
        rq = rq.merge(n_payouts, on=["race_date", "place_code"], how="left")
        cols = ["n_planned", "n_results", "n_payouts"]
        rq[cols] = rq[cols].fillna(0).astype(int)
        rq["status"] = rq.apply(_status_of, axis=1)
        add = rq.loc[rq["status"] == "normal", ["race_date", "place_code"]]
        print(f"  race_quality 拡張 (in-memory): {q_max} より後 "
              f"{rq['race_date'].nunique()} 日 → normal {len(add)} venue-days 追加")
        normal = pd.concat([normal, add], ignore_index=True)

    print(f"  normal venue-days: {len(normal):,}")

    data = {
        "meta": meta_all.merge(normal, on=["race_date", "place_code"]),
        "entries": entries_all.merge(normal, on=["race_date", "place_code"]),
        "stats": stats_all.merge(normal, on=["race_date", "place_code"]),
        "results": results_all.merge(normal, on=["race_date", "place_code"]),
    }

    # 実ライン特徴量 (公式並び)。存在すれば読み込み、build_dataset で left merge。
    # 被覆外 (2026-04-26 より前) は NaN のまま → LightGBM native 欠損処理。
    lr_path = DATA / "line_real_features.csv"
    if lr_path.exists():
        line_real = pd.read_csv(lr_path)
        data["line_real_features"] = line_real
        print(f"  line_real_features: {len(line_real):,} "
              f"({line_real['race_date'].min()}..{line_real['race_date'].max()})")
    else:
        print("  line_real_features.csv 未検出 → 実ライン特徴量なしで学習")

    data_max = str(data["results"]["race_date"].max())
    print(f"  meta: {len(data['meta']):,}, entries: {len(data['entries']):,}, "
          f"stats: {len(data['stats']):,}, results: {len(data['results']):,}")
    return data, data_max


# ─── 2. 学習 + OOS 評価 ──────────────────────────────────────────────────

def calibration_error(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    """ECE (expected calibration error)。分位 10 ビンの |平均予測 - 実際率| 加重平均。"""
    df = pd.DataFrame({"y": y, "p": p})
    df["bin"] = pd.qcut(df["p"], n_bins, duplicates="drop")
    g = df.groupby("bin", observed=True)
    w = g.size() / len(df)
    gap = (g["y"].mean() - g["p"].mean()).abs()
    return float((w * gap).sum())


def train_and_evaluate(
    df: pd.DataFrame, train_end: str, oos_start: str, oos_end: str,
    feat_cols: list[str],
) -> tuple[dict[str, lgb.Booster], dict, dict]:
    """学習期間で 4 モデルを学習し、OOS 期間で評価する。

    返り値: (models, oos_metrics, train_info)
    """
    train = df[df["race_date"] <= train_end]
    oos = df[(df["race_date"] >= oos_start) & (df["race_date"] <= oos_end)]
    if len(train) == 0 or len(oos) == 0:
        raise RuntimeError(
            f"学習/OOS データ不足: train={len(train):,}, oos={len(oos):,}"
        )

    # 内部 val = 学習期間の末尾 10% (early stopping 用。OOS は触らない)
    dt = pd.to_datetime(train["race_date"])
    cutoff = dt.quantile(0.9)
    is_val = (dt >= cutoff).to_numpy()
    X_tr, X_va = train.loc[~is_val, feat_cols], train.loc[is_val, feat_cols]
    X_oos = oos[feat_cols]
    cat_in_feat = [c for c in mlb.FEAT_CAT if c in feat_cols]
    print(f"  train: {len(X_tr):,} / val: {len(X_va):,} "
          f"(val cutoff {cutoff.date()}) / oos: {len(X_oos):,}")

    models: dict[str, lgb.Booster] = {}
    oos_metrics: dict[str, dict] = {}
    best_iters: dict[str, int] = {}

    for target in CLF_TARGETS:
        params = {**mlb.LGB_PARAMS_CLF, "seed": 42}  # 再現性のため seed 固定
        tr_set = lgb.Dataset(X_tr, train.loc[~is_val, target],
                             categorical_feature=cat_in_feat)
        va_set = lgb.Dataset(X_va, train.loc[is_val, target],
                             reference=tr_set, categorical_feature=cat_in_feat)
        model = lgb.train(
            params, tr_set, num_boost_round=800,
            valid_sets=[va_set], valid_names=["val"],
            callbacks=[lgb.early_stopping(50, verbose=False),
                       lgb.log_evaluation(0)],
        )
        models[target] = model
        best_iters[target] = int(model.best_iteration)
        p_oos = model.predict(X_oos, num_iteration=model.best_iteration)
        y_oos = oos[target].to_numpy()
        m = {
            "auc": float(roc_auc_score(y_oos, p_oos)),
            "logloss": float(log_loss(y_oos, np.clip(p_oos, 1e-6, 1 - 1e-6))),
            "ece": calibration_error(y_oos, p_oos),
        }
        oos_metrics[target] = m
        print(f"  {target}: OOS AUC={m['auc']:.4f} logloss={m['logloss']:.4f} "
              f"ECE={m['ece']:.4f} (best_iter {best_iters[target]})")

    params = {**mlb.LGB_PARAMS_REG, "seed": 42}
    tr_set = lgb.Dataset(X_tr, train.loc[~is_val, REG_TARGET],
                         categorical_feature=cat_in_feat)
    va_set = lgb.Dataset(X_va, train.loc[is_val, REG_TARGET],
                         reference=tr_set, categorical_feature=cat_in_feat)
    model = lgb.train(
        params, tr_set, num_boost_round=800,
        valid_sets=[va_set], valid_names=["val"],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
    )
    models[REG_TARGET] = model
    best_iters[REG_TARGET] = int(model.best_iteration)
    p_oos = model.predict(X_oos, num_iteration=model.best_iteration)
    oos_metrics[REG_TARGET] = {
        "mae": float(mean_absolute_error(oos[REG_TARGET], p_oos)),
    }
    print(f"  {REG_TARGET}: OOS MAE={oos_metrics[REG_TARGET]['mae']:.4f} "
          f"(best_iter {best_iters[REG_TARGET]})")

    train_info = {
        "n_train": int(len(X_tr)),
        "n_val": int(len(X_va)),
        "n_oos": int(len(X_oos)),
        "train_date_min": str(train["race_date"].min()),
        "train_date_max": train_end,
        "best_iterations": best_iters,
    }
    return models, oos_metrics, train_info


# ─── 3. 採用ゲート ───────────────────────────────────────────────────────

def _load_current_meta() -> dict | None:
    if not META_PATH.exists():
        return None
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _should_adopt(new_oos: dict, old_meta: dict | None,
                  force: bool = False) -> tuple[str, str]:
    """新モデルを採用すべきか判定 (閾値設計はモジュール docstring 参照)。

    Returns: (verdict: "OK" | "WARN" | "NG", reason: str)
    """
    nw = new_oos["y_win"]
    if force:
        return "OK", "--force 指定、強制採用"

    # 絶対床 (初回採用でも壊れモデルは弾く)
    if nw["auc"] < AUC_FLOOR:
        return "NG", (
            f"y_win OOS AUC {nw['auc']:.4f} が絶対床 {AUC_FLOOR} 割れ "
            "(データ/特徴量の破損疑い)"
        )

    if old_meta is None:
        return "OK", "旧メタなし、初回採用"

    old = old_meta.get("oos_metrics", {}).get("y_win", {})
    old_auc = old.get("auc", 0.0)
    old_logloss = old.get("logloss", 999.0)
    old_ece = old.get("ece", 999.0)

    ng_reasons: list[str] = []
    warn_reasons: list[str] = []

    # 注: 新旧の OOS 窓は 1 週間ずれた別期間。期間差ノイズを見込んだ閾値 (docstring)。
    auc_diff = nw["auc"] - old_auc
    if auc_diff < -AUC_DROP_NG:
        ng_reasons.append(f"AUC 低下 {old_auc:.4f}->{nw['auc']:.4f} ({auc_diff:+.4f})")
    elif auc_diff < -AUC_DROP_WARN:
        warn_reasons.append(f"AUC 微低下 {old_auc:.4f}->{nw['auc']:.4f} ({auc_diff:+.4f})")

    if old_logloss > 0:
        ratio = nw["logloss"] / old_logloss
        if ratio > LOGLOSS_RATIO_NG:
            ng_reasons.append(
                f"logloss 悪化 {old_logloss:.5f}->{nw['logloss']:.5f} "
                f"(+{(ratio - 1) * 100:.1f}%)")
        elif ratio > LOGLOSS_RATIO_WARN:
            warn_reasons.append(
                f"logloss 微悪化 {old_logloss:.5f}->{nw['logloss']:.5f} "
                f"(+{(ratio - 1) * 100:.1f}%)")

    ece_diff = nw["ece"] - old_ece
    if ece_diff > ECE_DELTA_NG:
        ng_reasons.append(f"ECE 悪化 {old_ece:.4f}->{nw['ece']:.4f} ({ece_diff:+.4f})")
    elif ece_diff > ECE_DELTA_WARN:
        warn_reasons.append(f"ECE 微悪化 {old_ece:.4f}->{nw['ece']:.4f} ({ece_diff:+.4f})")

    if ng_reasons:
        return "NG", "; ".join(ng_reasons)
    if warn_reasons:
        return "WARN", "; ".join(warn_reasons)
    return "OK", (
        f"OK: AUC {old_auc:.4f}->{nw['auc']:.4f}, "
        f"logloss {old_logloss:.5f}->{nw['logloss']:.5f}, "
        f"ECE {old_ece:.4f}->{nw['ece']:.4f}"
    )


def _append_history(row: dict) -> None:
    """判定・採否・指標を retrain_history.csv に append (塩漬け監視用)。"""
    header = [
        "timestamp", "verdict", "adopted", "reason",
        "train_end", "oos_start", "oos_end", "n_train", "n_oos",
        "auc_win", "logloss_win", "ece_win", "auc_top2", "auc_top3",
        "best_iter_win",
        "old_auc_win", "old_logloss_win", "old_ece_win",
        "regen_start", "regen_end", "runtime_sec",
    ]
    new_file = not HISTORY_PATH.exists()
    with open(HISTORY_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerow([row.get(k, "") for k in header])
    print(f"  履歴 append: {HISTORY_PATH}")


# ─── 4. 採用処理 (バックアップ → 保存 → probs/picks 差分再生成) ─────────

def _model_path(target: str) -> Path:
    return MODEL_DIR / f"ml_weekly_model_{target}.lgb"


def _model_hash() -> str:
    """保存済み 4 モデルファイル連結の SHA-256 先頭 12 桁 (成果物同一性)。

    daily_shadow_predict.py の model_hash と同一定義。予測ログとの突合キー。
    """
    h = hashlib.sha256()
    for target in list(CLF_TARGETS) + [REG_TARGET]:
        h.update(_model_path(target).read_bytes())
    return h.hexdigest()[:12]


def _append_manifest(meta: dict, verdict: str, reason: str, mhash: str) -> None:
    """weekly_model/MANIFEST.md の自動追記ログへ 1 行追加 (監査 P1-2)。

    採用・特徴量数・hash・判定理由を機械的に残す。ロールバック等の手動操作も
    MANIFEST に手で記録する運用 (MANIFEST 冒頭の説明参照)。
    """
    manifest = MODEL_DIR / "MANIFEST.md"
    line = (f"- {meta['trained_at']} 採用 (verdict={verdict}): "
            f"{meta['n_features']} 特徴量, model_hash={mhash}, "
            f"OOS y_win AUC {meta['oos_metrics']['y_win']['auc']:.4f} — {reason}\n")
    if manifest.exists():
        with open(manifest, "a", encoding="utf-8") as f:
            f.write(line)
    else:
        with open(manifest, "w", encoding="utf-8") as f:
            f.write("# weekly_model MANIFEST\n\n## 自動追記ログ\n"
                    "<!-- AUTO-APPEND-BELOW -->\n" + line)
    print(f"  MANIFEST 追記: {manifest}")


def adopt_models(models: dict[str, lgb.Booster], meta: dict) -> None:
    """旧メタ/旧モデルを .prev にバックアップして新モデルを採用。"""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if META_PATH.exists():
        shutil.copy2(META_PATH, META_PATH.with_suffix(".prev.json"))
    for target in list(CLF_TARGETS) + [REG_TARGET]:
        mp = _model_path(target)
        if mp.exists():
            shutil.copy2(mp, mp.with_suffix(".prev.lgb"))
        models[target].save_model(
            str(mp), num_iteration=models[target].best_iteration
        )
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"  採用保存: {MODEL_DIR} (meta + 4 boosters, 旧は .prev)")


def _merge_replace(path: Path, new_df: pd.DataFrame, regen_start: str,
                   sort_keys: list[str]) -> int:
    """既存 CSV の race_date >= regen_start を new_df で置換して保存。"""
    if path.exists():
        old = pd.read_csv(path)
        kept = old[old["race_date"].astype(str) < regen_start]
        out = pd.concat([kept, new_df], ignore_index=True)[list(old.columns)]
    else:
        out = new_df
    out = out.sort_values(sort_keys).reset_index(drop=True)
    out.to_csv(path, index=False)
    return len(out)


def regenerate_outputs(
    models: dict[str, lgb.Booster], df: pd.DataFrame,
    regen_start: str, regen_end: str, feat_cols: list[str],
) -> dict:
    """ml_pred_probs.csv / ml_picks.csv を [regen_start, regen_end] で差分再生成。"""
    sub = df[(df["race_date"] >= regen_start) & (df["race_date"] <= regen_end)].copy()
    if len(sub) == 0:
        print(f"  [skip] 再生成対象 0 行 ({regen_start} 〜 {regen_end})")
        return {"n_new_rows": 0}

    preds = {
        t: models[t].predict(sub[feat_cols],
                             num_iteration=models[t].best_iteration)
        for t in CLF_TARGETS + [REG_TARGET]
    }

    # per-car 確率 (ml_baseline.py の prob_df と同一カラム)
    prob_df = sub[KEYS + ["car_no"]].copy()
    prob_df["p_win"] = preds["y_win"]
    prob_df["p_top2"] = preds["y_top2"]
    prob_df["p_top3"] = preds["y_top3"]
    prob_df["pred_rank"] = preds["y_rank"]
    n_probs = _merge_replace(PROBS_PATH, prob_df, regen_start, KEYS + ["car_no"])
    print(f"  ml_pred_probs.csv: +{len(prob_df):,} 行再生成 → 計 {n_probs:,} 行")

    # picks (ml_baseline.make_picks を共用)
    picks = mlb.make_picks(sub, preds)
    n_picks = _merge_replace(PICKS_PATH, picks, regen_start, KEYS)
    print(f"  ml_picks.csv: +{len(picks):,} レース再生成 → 計 {n_picks:,} 行")

    return {
        "n_new_rows": int(len(prob_df)),
        "n_new_races": int(len(picks)),
        "probs_total": n_probs,
        "picks_total": n_picks,
    }


def update_ev_merged() -> None:
    """ev_prerace_pipeline.py を実行して ev_prerace_merged.csv を更新。"""
    print("\n[5] ev_prerace_pipeline.py 実行 (ev_prerace_merged.csv 更新)")
    r = subprocess.run(
        [sys.executable, str(ROOT / "ev_prerace_pipeline.py")],
        cwd=str(ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"ev_prerace_pipeline.py 失敗 (exit {r.returncode})")


# ─── 5. メイン ────────────────────────────────────────────────────────────

def main() -> None:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description="週次 ML 再学習 + 採用品質ゲート")
    ap.add_argument("--force", action="store_true",
                    help="品質ゲート (絶対床含む) を無視して強制採用")
    ap.add_argument("--oos-weeks", type=int, default=OOS_WEEKS_DEFAULT,
                    help=f"OOS 評価期間の週数 (default {OOS_WEEKS_DEFAULT})")
    args = ap.parse_args()

    t0 = time.time()
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    oos_start = (today - timedelta(weeks=args.oos_weeks)).isoformat()
    train_end = (today - timedelta(weeks=args.oos_weeks, days=1)).isoformat()

    print("=" * 70)
    print("週次 ML 再学習 + 採用品質ゲート (production_no_odds)")
    print(f"  学習: 〜{train_end} / OOS: {oos_start}〜{yesterday} "
          f"({args.oos_weeks} 週) / force={args.force}")
    print("=" * 70)

    print("\n[1] データロード (race_quality in-memory 拡張)")
    data, data_max = load_data_extended()

    print("\n[2] dataset 構築 (ml_baseline.build_dataset 共用)")
    df = mlb.build_dataset(data)
    feat_cols = list(mlb.FEAT_NUM)
    # 実ライン特徴量が読み込まれていれば本投入 (build_dataset が merge 済)
    if "line_real_features" in data:
        feat_cols = feat_cols + list(mlb.LINE_REAL_NUM_FEATURES)
        print(f"  実ライン特徴量を本投入: +{len(mlb.LINE_REAL_NUM_FEATURES)} 列")
    feat_cols = feat_cols + list(mlb.FEAT_CAT)
    oos_end = min(yesterday, data_max)

    print("\n[3] 学習 + OOS 評価")
    models, oos_metrics, train_info = train_and_evaluate(
        df, train_end, oos_start, oos_end, feat_cols
    )

    print("\n[4] 採用ゲート判定")
    old_meta = _load_current_meta()
    verdict, reason = _should_adopt(oos_metrics, old_meta, force=args.force)
    print(f"  verdict = {verdict}: {reason}")

    old_win = (old_meta or {}).get("oos_metrics", {}).get("y_win", {})
    hist_row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "verdict": verdict,
        "reason": reason,
        "train_end": train_end,
        "oos_start": oos_start,
        "oos_end": oos_end,
        "n_train": train_info["n_train"],
        "n_oos": train_info["n_oos"],
        "auc_win": round(oos_metrics["y_win"]["auc"], 5),
        "logloss_win": round(oos_metrics["y_win"]["logloss"], 6),
        "ece_win": round(oos_metrics["y_win"]["ece"], 5),
        "auc_top2": round(oos_metrics["y_top2"]["auc"], 5),
        "auc_top3": round(oos_metrics["y_top3"]["auc"], 5),
        "best_iter_win": train_info["best_iterations"]["y_win"],
        "old_auc_win": round(old_win["auc"], 5) if "auc" in old_win else "",
        "old_logloss_win": round(old_win["logloss"], 6) if "logloss" in old_win else "",
        "old_ece_win": round(old_win["ece"], 5) if "ece" in old_win else "",
    }

    if verdict == "NG":
        print("=" * 70)
        print(f"新モデル採用見送り (NG): {reason}")
        print("旧モデルを維持します。--force で強制採用可。")
        print("=" * 70)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(REJECTED_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "trained_at": datetime.now().isoformat(timespec="seconds"),
                "verdict": verdict,
                "rejected_reason": reason,
                "oos_metrics": oos_metrics,
                "train_info": train_info,
            }, f, ensure_ascii=False, indent=2)
        print(f"  見送りメタ保存: {REJECTED_PATH}")
        hist_row.update({"adopted": 0, "regen_start": "", "regen_end": "",
                         "runtime_sec": round(time.time() - t0, 1)})
        _append_history(hist_row)
        return  # exit 0 (タスクとしては正常)

    if verdict == "WARN":
        print(f"  [WARN] 採用するが要注意: {reason}")

    # 採用: バックアップ → 保存
    meta = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "model_config": "production_no_odds",
        "oos_weeks": args.oos_weeks,
        "oos_start": oos_start,
        "oos_end": oos_end,
        "train_info": train_info,
        "oos_metrics": oos_metrics,
        "n_features": len(feat_cols),
        "feature_columns": feat_cols,
        "quality_gate_verdict": verdict,
        "adoption_reason": reason,
    }
    adopt_models(models, meta)

    # 成果物 hash を確定し meta / MANIFEST に記録 (監査 P1-2: 有効モデルの追跡)
    mhash = _model_hash()
    meta["model_hash"] = mhash
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    _append_manifest(meta, verdict, reason, mhash)

    # probs/picks 差分再生成 (範囲設計は docstring 参照)
    if PROBS_PATH.exists():
        last = str(pd.read_csv(PROBS_PATH, usecols=["race_date"])["race_date"].max())
        regen_start = last  # 最終日は再生成で置換 (取りこぼし防止)
    else:
        regen_start = oos_start  # 初回相当: OOS 期間のみ (in-sample 確率を作らない)
    if regen_start <= train_end:
        # 学習期間に食い込む再生成は in-sample になるため切り上げる
        regen_start = (pd.Timestamp(train_end) + pd.Timedelta(days=1)).date().isoformat()
        print(f"  [注意] regen_start が学習期間内のため {regen_start} に切り上げ")

    print(f"\n  probs/picks 差分再生成: {regen_start} 〜 {oos_end}")
    regen_info = regenerate_outputs(models, df, regen_start, oos_end, feat_cols)

    update_ev_merged()

    hist_row.update({
        "adopted": 1,
        "regen_start": regen_start,
        "regen_end": oos_end,
        "runtime_sec": round(time.time() - t0, 1),
    })
    _append_history(hist_row)

    print("\n" + "=" * 70)
    print(f"完了 (verdict={verdict}, 採用): "
          f"probs +{regen_info.get('n_new_rows', 0):,} 行 / "
          f"picks +{regen_info.get('n_new_races', 0):,} レース / "
          f"runtime {time.time() - t0:.1f}s")
    print("=" * 70)


if __name__ == "__main__":
    main()
