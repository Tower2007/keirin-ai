"""(race_date, place_code, race_no) 単位の完了状態表を生成 (2026-07-11 監査 P1-3)

背景: ingest_day の完了判定は venue-day 単位 (1 行でもあればセクション完了扱い) で、
「正当な中止」と「取得漏れ」を区別できなかった。本スクリプトは既存 CSV から
per-race の完了状態を導出し、race_completion.csv に書き出す。

status 分類:
  complete        : 着順あり + 払戻あり (正常完了)
  canceled        : results 行は存在するが着順が 1 つも無い (中止/不成立。
                    例 2026-06-17 京王閣 R1 = 欠場 1 行のみ。全期間で
                    このパターンは 100% 払戻なしと実証済み → 正当な欠損)
  missing_results : entries はあるが results 行が無い (取得漏れ候補 → 再取得対象)
  missing_payout  : 着順はあるが払戻が無い (取得漏れ候補 → 再取得対象)

venue-day 全体が中止 (meta.is_canceled) の場合は canceled_day を付与。

使い方:
  python build_race_completion.py                # 全期間
  python build_race_completion.py --days 30      # 直近 30 日のみ再計算して全体を置換

出力: DATA_DIR/race_completion.csv
weekly_drift_report.py がこれを読んで「取得漏れ候補」を週次サマリに出す。
per-race 再取得は recover_missing_races.py がこの表を入力に行う。

台帳の鮮度 (2026-07-11 艦隊監査 P1): 結果/払戻の取込直後に refresh() が
ingest_yesterday.py / ingest_day.py / recover_missing_races.py から呼ばれ、
「実データはあるのに missing 扱い」の stale 行が翌週まで残らないようにする。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import pandas as pd

from src.config import DATA_DIR
from src import voids as _voids

KEYS = ["race_date", "place_code", "race_no"]


def build(start: str | None = None) -> pd.DataFrame:
    def _read(name: str, usecols: list[str]) -> pd.DataFrame:
        df = pd.read_csv(DATA_DIR / name, usecols=usecols,
                         dtype={k: str for k in KEYS if k in usecols},
                         low_memory=False)
        if start:
            df = df[df["race_date"] >= start]
        return df

    entries = _read("race_entries.csv", KEYS + ["car_no"])
    results = _read("race_results.csv", KEYS + ["tyaku"])
    payouts = _read("payouts.csv", KEYS)
    meta = _read("race_meta.csv", ["race_date", "place_code", "is_canceled"]) \
        if "is_canceled" in pd.read_csv(DATA_DIR / "race_meta.csv", nrows=0).columns \
        else None

    exp = entries.groupby(KEYS)["car_no"].nunique().rename("n_entries")
    res_rows = results.groupby(KEYS).size().rename("n_result_rows")
    res_tyaku = (results[results["tyaku"].notna()]
                 .groupby(KEYS).size().rename("n_tyaku"))
    pay = payouts.drop_duplicates(KEYS).set_index(KEYS).assign(has_payout=1)["has_payout"]

    comp = pd.concat([exp, res_rows, res_tyaku, pay], axis=1).reset_index()
    for c in ["n_entries", "n_result_rows", "n_tyaku", "has_payout"]:
        comp[c] = comp[c].fillna(0).astype(int)

    def status_of(r) -> str:
        if r["n_result_rows"] > 0 and r["n_tyaku"] == 0:
            return "canceled"
        if r["n_result_rows"] == 0:
            return "missing_results"
        if r["n_tyaku"] > 0 and r["has_payout"] == 0:
            return "missing_payout"
        return "complete"

    comp["status"] = comp.apply(status_of, axis=1)

    if meta is not None:
        m = meta.copy()
        m["is_canceled"] = m["is_canceled"].astype(str).str.lower().isin(["true", "1"])
        comp = comp.merge(m[m["is_canceled"]][["race_date", "place_code"]]
                          .drop_duplicates().assign(canceled_day=1),
                          on=["race_date", "place_code"], how="left")
        comp["canceled_day"] = comp["canceled_day"].fillna(0).astype(int)
        # venue-day 中止の日は missing 系も正当な欠損
        comp.loc[(comp["canceled_day"] == 1)
                 & comp["status"].str.startswith("missing"), "status"] = "canceled"
    else:
        comp["canceled_day"] = 0

    # 正当な欠損の台帳 (void_races.csv: 全返還の不成立 / 日全体の中止) に載るレースは欠測から除外し、
    # status=canceled + void_reason で数える (2026-11-15)。read は DATA_DIR 差し替えに追随させる。
    comp["void_reason"] = ""
    try:
        reasons = {(v["race_date"], v["place_code"], v["race_no"]): v["reason"]
                   for v in _voids.load_voids(DATA_DIR)}
        miss = comp["status"].str.startswith("missing")
        for i in comp.index[miss]:
            rd, pc, rn = str(comp.at[i, "race_date"]), str(comp.at[i, "place_code"]), str(comp.at[i, "race_no"])
            r = reasons.get((rd, pc, rn)) or reasons.get((rd, pc, ""))
            if r:
                comp.at[i, "status"] = "canceled"
                comp.at[i, "void_reason"] = r
    except Exception:
        pass   # 台帳が読めなくても従来どおりの判定で続行
    return comp.sort_values(KEYS).reset_index(drop=True)


def lines_gap_summary(days: int = 14, start: str | None = None,
                      end: str | None = None) -> dict:
    """並び (race_lines.csv) の欠損 R 数 / 率を返す (2026-11-15)。

    race_entries にあるレースのうち race_lines に 1 行も無いものを欠損とする。並びは事後に取れず
    (過去日のページに nInfo が無い)、ミッドナイト開催を中心に場・日単位でまるごと欠けるが、
    台帳 data_gaps.csv には PC 停止日しか載らなかったため、日次/週次で率を見えるようにする。
    期間 = [start, end] (既定: 昨日までの直近 days 日)。当日は取り直し中なので含めない。
    戻り値: races / missing_races / rate / venue_days / missing_venue_days /
            by_band (発走区分別 {"morning","day","night","midnight"}: races, missing)。
    """
    today = date.today()
    end = end or (today - timedelta(days=1)).isoformat()
    start = start or (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    ent = pd.read_csv(DATA_DIR / "race_entries.csv",
                      usecols=KEYS + ["st_time"], dtype=str, low_memory=False)
    ent = ent[(ent["race_date"] >= start) & (ent["race_date"] <= end)]
    ent = ent.drop_duplicates(KEYS)
    lp = DATA_DIR / "race_lines.csv"
    if lp.exists():
        ln = pd.read_csv(lp, usecols=KEYS, dtype=str, low_memory=False)
        ln = ln[(ln["race_date"] >= start) & (ln["race_date"] <= end)].drop_duplicates(KEYS)
        ln = ln.assign(_has=1)
        m = ent.merge(ln, on=KEYS, how="left")
        m["miss"] = m["_has"].isna()
    else:
        m = ent.assign(miss=True)

    def _band(first: str) -> str:
        try:
            h = int(str(first)[:2])
        except ValueError:
            return "day"
        return "morning" if h < 10 else "day" if h < 15 else "night" if h < 20 else "midnight"

    vd = m.groupby(["race_date", "place_code"]).agg(
        n=("miss", "size"), miss=("miss", "sum"), first=("st_time", "min")).reset_index()
    vd["band"] = vd["first"].map(_band)
    by_band = {b: {"races": int(g["n"].sum()), "missing": int(g["miss"].sum())}
               for b, g in vd.groupby("band")}
    races = int(len(m))
    miss = int(m["miss"].sum())
    return {"start": start, "end": end, "races": races, "missing_races": miss,
            "rate": (miss / races) if races else 0.0,
            "venue_days": int(len(vd)), "missing_venue_days": int((vd["miss"] > 0).sum()),
            "by_band": by_band}


def refresh(days: int | None = None) -> pd.DataFrame:
    """race_completion.csv を再計算して書き出す (days=None は全期間)。

    days 指定時は直近 N 日のみ再計算し、既存表の当該期間を置換する。
    stdout には一切出力しない (pythonw / cron 配下からの呼び出し安全)。

    呼び出し元 (2026-07-11 艦隊監査 P1 対応):
      - ingest_yesterday.py  : 日次 results/payouts 取込直後 (台帳を実態に整合)
      - ingest_day.py main() : 手動 ingest / per-race 自己治癒の直後
      - recover_missing_races.py : 救済 backfill の直後
      - scripts/cron_weekly_retrain.bat : 週次 (本 CLI 経由、従来どおり)
    """
    out_path = DATA_DIR / "race_completion.csv"
    if days:
        start = (date.today() - timedelta(days=days)).isoformat()
        new = build(start)
        if out_path.exists():
            old = pd.read_csv(out_path, dtype={k: str for k in KEYS})
            old = old[old["race_date"] < start]
            if "void_reason" not in old.columns:
                old["void_reason"] = ""
            new = pd.concat([old, new], ignore_index=True)
    else:
        new = build(None)
    new.to_csv(out_path, index=False)
    return new


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="直近 N 日だけ再計算 (既存表の当該期間を置換)")
    args = ap.parse_args()

    new = refresh(args.days)
    print(f"wrote {DATA_DIR / 'race_completion.csv'} ({len(new):,} races)")
    try:
        g = lines_gap_summary(days=14)
        print(f"並び欠損 ({g['start']}〜{g['end']}): {g['missing_races']} / {g['races']} R "
              f"({g['rate'] * 100:.1f}%)  場・日 {g['missing_venue_days']} / {g['venue_days']}")
    except Exception as e:
        print(f"並び欠損の集計に失敗: {e}")
    print("\n=== status counts ===")
    print(new["status"].value_counts().to_string())
    # 取得漏れ候補 (直近 14 日、canceled は正当欠損なので除外)
    # 当日は results が構造的に未取得 (翌朝 5:00 cron) なので対象外
    recent_cut = (date.today() - timedelta(days=14)).isoformat()
    today_iso = date.today().isoformat()
    sus = new[(new["race_date"] >= recent_cut)
              & (new["race_date"] < today_iso)
              & new["status"].str.startswith("missing")]
    print(f"\n=== 直近14日の取得漏れ候補: {len(sus)} 件 ===")
    if len(sus):
        print(sus[KEYS + ["n_entries", "n_result_rows", "n_tyaku",
                          "has_payout", "status"]].to_string(index=False))


if __name__ == "__main__":
    main()
