"""Feature Importance 分析 (Phase 6 6a-2/6a-3 の深掘り)

LightGBM の gain importance を抽出し、どの特徴がどのターゲット予測に
効いているかを可視化する。特に odds / line 特徴量がどう使われているかを焦点に。

ml_baseline.py で学習済モデルが保存されないため、ここで再学習する
(with_odds_line 構成、約 5-7 分)。

出力: D:\\keirin-ai-data\\feature_importance_report.md
       D:\\keirin-ai-data\\feature_importance.csv (生データ、後で再分析用)

使い方:
  python analyze_feature_importance.py
"""

from __future__ import annotations

import sys
import time

import pandas as pd

import ml_baseline
from src.config import DATA_DIR as DATA

sys.stdout.reconfigure(encoding="utf-8")


def collect_importance(model, target: str) -> pd.DataFrame:
    """LightGBM Booster から特徴量重要度を取得。"""
    imp_gain = model.feature_importance(importance_type="gain")
    imp_split = model.feature_importance(importance_type="split")
    names = model.feature_name()
    df = pd.DataFrame({
        "target": target,
        "feature": names,
        "gain": imp_gain,
        "split": imp_split,
    })
    total_gain = df["gain"].sum()
    df["gain_pct"] = (df["gain"] / total_gain * 100) if total_gain else 0
    return df.sort_values("gain", ascending=False).reset_index(drop=True)


def main() -> None:
    t0 = time.time()
    print("[1/3] Loading data with odds + line features...")
    data = ml_baseline.load_data(use_odds=True, use_line=True)
    df = ml_baseline.build_dataset(data)
    train, test = ml_baseline.split_train_test(df)

    feat_cols = (
        list(ml_baseline.FEAT_NUM)
        + ml_baseline.ODDS_NUM_FEATURES
        + ml_baseline.LINE_NUM_FEATURES
        + ml_baseline.FEAT_CAT
    )
    print(f"  total features: {len(feat_cols)}")

    print("\n[2/3] Training 4 models...")
    importance_dfs: dict[str, pd.DataFrame] = {}
    for target in ["y_win", "y_top2", "y_top3"]:
        print(f"  >>> {target}")
        model, _, _ = ml_baseline.train_one(
            train, test, target, ml_baseline.LGB_PARAMS_CLF, feat_cols
        )
        importance_dfs[target] = collect_importance(model, target)
    print("  >>> y_rank")
    model, _, _ = ml_baseline.train_one(
        train, test, "y_rank", ml_baseline.LGB_PARAMS_REG, feat_cols
    )
    importance_dfs["y_rank"] = collect_importance(model, "y_rank")

    print(f"\n[3/3] Generating report... (elapsed {time.time()-t0:.0f}s)")

    # 生データ CSV
    all_imp = pd.concat(importance_dfs.values(), ignore_index=True)
    all_imp.to_csv(DATA / "feature_importance.csv", index=False)

    # Markdown レポート
    out = DATA / "feature_importance_report.md"
    lines: list[str] = []
    lines.append("# Feature Importance Report (with_odds_line)")
    lines.append("")
    lines.append("LightGBM の gain importance で各特徴の寄与度を確認。")
    lines.append("`gain_pct` = そのターゲット予測における gain 合計の何 % を占めるか。")
    lines.append("")

    # ターゲット別 top 15
    for target, df_imp in importance_dfs.items():
        lines.append(f"## {target} - top 15 (by gain)")
        lines.append("")
        top = df_imp.head(15)[["feature", "gain", "gain_pct"]].copy()
        top["gain"] = top["gain"].astype(int)
        top["gain_pct"] = top["gain_pct"].round(2)
        lines.append(top.to_markdown(index=False))
        lines.append("")

    # クロスターゲット集計
    lines.append("## クロスターゲット集計 (4 ターゲット合計 gain%)")
    lines.append("")
    cross = pd.DataFrame({"feature": importance_dfs["y_win"]["feature"]})
    for target, df_imp in importance_dfs.items():
        cross = cross.merge(
            df_imp[["feature", "gain_pct"]].rename(
                columns={"gain_pct": f"{target}_pct"}
            ),
            on="feature", how="outer",
        )
    pct_cols = [f"{t}_pct" for t in importance_dfs]
    for c in pct_cols:
        cross[c] = cross[c].fillna(0)
    cross["total_pct"] = cross[pct_cols].sum(axis=1)
    cross = cross.sort_values("total_pct", ascending=False).reset_index(drop=True).head(25)
    for c in pct_cols + ["total_pct"]:
        cross[c] = cross[c].round(2)
    lines.append(cross.to_markdown(index=False))
    lines.append("")

    # オッズ特徴のフォーカス
    lines.append("## オッズ特徴量の使われ方")
    lines.append("")
    odds_df = pd.DataFrame({"feature": ml_baseline.ODDS_NUM_FEATURES})
    for target, df_imp in importance_dfs.items():
        d = df_imp.set_index("feature")["gain_pct"].to_dict()
        odds_df[f"{target}_pct"] = odds_df["feature"].map(d).fillna(0).round(2)
    odds_df["total_pct"] = odds_df[pct_cols].sum(axis=1).round(2)
    odds_df = odds_df.sort_values("total_pct", ascending=False)
    lines.append(odds_df.to_markdown(index=False))
    lines.append("")

    # ライン特徴のフォーカス
    lines.append("## 弱ライン特徴量の使われ方")
    lines.append("")
    line_df = pd.DataFrame({"feature": ml_baseline.LINE_NUM_FEATURES})
    for target, df_imp in importance_dfs.items():
        d = df_imp.set_index("feature")["gain_pct"].to_dict()
        line_df[f"{target}_pct"] = line_df["feature"].map(d).fillna(0).round(2)
    line_df["total_pct"] = line_df[pct_cols].sum(axis=1).round(2)
    line_df = line_df.sort_values("total_pct", ascending=False)
    lines.append(line_df.to_markdown(index=False))
    lines.append("")

    lines.append("## 補足")
    lines.append("- gain: そのスプリットで減った損失の合計")
    lines.append("- y_win: 1着予測 / y_top2: 連対 / y_top3: 3着内 / y_rank: 順位回帰")
    lines.append("- y_rank は MAE 最適化なので importance スケールが他と少し異なる")
    lines.append("- 低 gain_pct (1% 未満) = 使われていない or 他特徴で代替可能")

    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n=== 完了 ({time.time()-t0:.0f}s) ===")
    print(f"Report: {out}")
    print(f"CSV:    {DATA / 'feature_importance.csv'}")


if __name__ == "__main__":
    main()
