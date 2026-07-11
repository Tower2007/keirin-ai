"""shadow 推論ログ: 当日レースの予測を「その時点」で固定保存 (購入なし)

2026-07-11 監査 P1-4 対応。
これまで予測は週次再学習時の OOS 再生成 (ml_pred_probs.csv) しか無く、
「その時点で実際に使えた予測」を後から監査できなかった。本スクリプトは
毎朝 (lines cron 7:00 の後、prerace daemon 8:00 の前) に現行 weekly_model で
当日全レースの per-car 確率を予測し、以下を付与して固定保存する:

  - predicted_at     : 予測実行時刻 (これ以降の情報は使っていない証明)
  - model_trained_at : 使用モデルの学習時刻 (weekly_model meta より)
  - model_hash       : 4 モデルファイルの SHA-256 先頭 12 桁 (成果物同一性)

設計判断: offset 別 (5/3/2/1/0.5 分前) の EV 候補はここでは保存しない。
production モデルはオッズ非依存で予測は朝時点で確定するため、offset 別に
変わるのは odds 側だけ。本ログと race_odds_prerace.csv は双方が時点記録
(predicted_at / snapshot_dt) を持つので、任意 offset の EV 候補は後結合で
完全に監査再現できる。daemon に ML 依存を持ち込まない (オッズ蓄積の堅牢性優先)。

注意:
  - race_quality フィルタは適用しない (当日の quality は未確定。
    「その時点で出せた予測」をそのまま残すのが shadow ログの目的)
  - 同一日 × 同一 model_hash が既に存在すればスキップ (再実行安全)

使い方:
  python daily_shadow_predict.py                # today
  python daily_shadow_predict.py --date 2026-07-11
  python daily_shadow_predict.py --force        # 既存でも追記し直す
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

# pythonw 起動対応 (daemon と同じ cwd / sys.path 補正)
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import lightgbm as lgb
import pandas as pd

import ml_baseline as mlb
from src.config import DATA_DIR
from src.storage import append_rows

MODEL_DIR = DATA_DIR / "weekly_model"
META_PATH = MODEL_DIR / "ml_weekly_meta.json"
OUT_NAME = "shadow_predictions.csv"
TARGETS = ["y_win", "y_top2", "y_top3", "y_rank"]

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / f"shadow_predict_{date.today().isoformat()}.log",
            encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def model_hash() -> str:
    """4 モデルファイル連結の SHA-256 先頭 12 桁 (成果物同一性の証明)。"""
    h = hashlib.sha256()
    for t in TARGETS:
        p = MODEL_DIR / f"ml_weekly_model_{t}.lgb"
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def preflight_hash_check(meta: dict, mhash: str) -> None:
    """meta 記載の model_hash と実ファイル再計算 hash を照合 (Codex 07-11)。

    不意の手動置換 (例: 7/7 の無記録ロールバック) を fail-fast で検知する。
    不一致なら予測を停止。meta に model_hash が無い旧形式は警告のみ
    (次回 weekly_retrain 採用時から必ず付く)。
    """
    recorded = meta.get("model_hash")
    if recorded is None:
        logger.warning("meta に model_hash 無し (旧形式)。実ファイル hash=%s で続行。",
                       mhash)
        return
    if recorded != mhash:
        logger.error(
            "model_hash 不一致: meta=%s / 実ファイル=%s — モデルファイルが "
            "meta と別物に置換されている。予測を停止。weekly_model/MANIFEST.md を確認せよ。",
            recorded, mhash)
        sys.exit(1)


def code_revision() -> str:
    """現在の Git commit (取得不能なら unknown)。"""
    try:
        import subprocess
        return subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def load_day_dataset(target_date: str) -> pd.DataFrame:
    """当日分のみの dataset を ml_baseline.build_dataset で構築。

    results は未確定なので空 DataFrame を渡す (ターゲット列はダミー 0/NaN になる)。
    """
    def _read_day(name: str) -> pd.DataFrame:
        df = pd.read_csv(DATA_DIR / name, low_memory=False)
        return df[df["race_date"] == target_date].copy()

    stats = _read_day("race_stats.csv")
    if len(stats) == 0:
        return pd.DataFrame()
    entries = _read_day("race_entries.csv")
    meta = _read_day("race_meta.csv")
    empty_results = pd.DataFrame(
        columns=["race_date", "place_code", "race_no", "car_no", "tyaku"])
    data = {"stats": stats, "entries": entries, "meta": meta,
            "results": empty_results}
    return mlb.build_dataset(data)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="対象日 YYYY-MM-DD (default today)")
    ap.add_argument("--force", action="store_true",
                    help="同一日×同一モデルの既存行があっても追記")
    args = ap.parse_args()
    target_date = args.date or date.today().isoformat()

    if not META_PATH.exists():
        logger.error("weekly_model meta が無い: %s", META_PATH)
        sys.exit(1)
    with open(META_PATH, encoding="utf-8") as f:
        meta = json.load(f)
    feat_cols = meta["feature_columns"]
    trained_at = meta.get("trained_at", "")
    mhash = model_hash()
    preflight_hash_check(meta, mhash)
    logger.info("=== shadow predict: date=%s model=%s (%s, %d feats) ===",
                target_date, mhash, trained_at, len(feat_cols))

    df = load_day_dataset(target_date)
    if len(df) == 0:
        logger.info("当日データなし (%s)。lines cron 未完了か開催なし。", target_date)
        return
    missing = [c for c in feat_cols if c not in df.columns]
    if missing:
        logger.error("特徴量不足 (モデル %d 列に対し欠落 %d): %s",
                     len(feat_cols), len(missing), missing[:10])
        sys.exit(1)

    # 再実行安全 (Codex 07-11 指摘で改善): 旧実装は同一日×同一 hash の行が
    # 「1 行でも」あれば日全体を skip し、7:30 時点の出走表が部分取得だと
    # 部分予測が永久に残った。期待 (race, car) 集合との差分で判定し、
    # 不足があれば欠けたレースだけ補完する。
    out_path = DATA_DIR / OUT_NAME
    if out_path.exists() and not args.force:
        existing = pd.read_csv(
            out_path,
            usecols=["race_date", "place_code", "race_no", "car_no",
                     "model_hash"],
            dtype=str)
        done = existing[(existing["race_date"] == target_date)
                        & (existing["model_hash"] == mhash)]
        done_keys = set(map(tuple, done[
            ["race_date", "place_code", "race_no", "car_no"]].astype(str).values))
        expect = df[["race_date", "place_code", "race_no", "car_no"]].astype(str)
        expect_keys = set(map(tuple, expect.values))
        lack = expect_keys - done_keys
        if not lack:
            logger.info("既に完全予測済み (date=%s, model=%s, %d rows)。スキップ。",
                        target_date, mhash, len(expect_keys))
            return
        if done_keys:
            # 欠けたレースだけ補完 (レース単位で絞る)
            lack_races = {(k[0], k[1], k[2]) for k in lack}
            mask = expect.apply(
                lambda r: (r["race_date"], r["place_code"], r["race_no"])
                in lack_races, axis=1)
            df = df[mask.values].copy()
            logger.info("部分予測を検出: 不足 %d レースのみ補完 (既存 %d 行は保持)",
                        len(lack_races), len(done_keys))

    models = {
        t: lgb.Booster(model_file=str(MODEL_DIR / f"ml_weekly_model_{t}.lgb"))
        for t in TARGETS
    }
    predicted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    X = df[feat_cols]
    # 推論入力の hash (Codex 07-11): 後結合監査で「この予測は正確にこの入力から
    # 出た」を固定する。キー順ソート済み CSV バイト列の SHA-256 先頭 12 桁。
    sorted_df = df.sort_values(
        ["race_date", "place_code", "race_no", "car_no"])
    input_hash = hashlib.sha256(
        sorted_df[feat_cols].to_csv(index=False).encode("utf-8")
    ).hexdigest()[:12]
    revision = code_revision()
    preds = {t: m.predict(X, num_iteration=m.best_iteration)
             for t, m in models.items()}

    rows = []
    for i, (_, r) in enumerate(df[["race_date", "place_code", "race_no",
                                   "car_no"]].iterrows()):
        rows.append({
            "race_date": r["race_date"], "place_code": r["place_code"],
            "race_no": r["race_no"], "car_no": r["car_no"],
            "p_win": round(float(preds["y_win"][i]), 6),
            "p_top2": round(float(preds["y_top2"][i]), 6),
            "p_top3": round(float(preds["y_top3"][i]), 6),
            "pred_rank": round(float(preds["y_rank"][i]), 4),
            "predicted_at": predicted_at,
            "model_trained_at": trained_at,
            "model_hash": mhash,
            "n_features": len(feat_cols),
            "prediction_input_hash": input_hash,
            "code_revision": revision,
        })
    n = append_rows(OUT_NAME, rows)
    n_races = df.groupby(["race_date", "place_code", "race_no"]).ngroups
    logger.info("保存: %d 行 (%d races) → %s", n, n_races, OUT_NAME)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("shadow predict crashed")
        raise
