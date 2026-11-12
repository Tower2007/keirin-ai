"""shadow 前向き評価 + 凍結ゲートB 判定 + 打ち切り条件 (2026-11-12)

背景: shadow_predictions.csv は毎朝書かれるだけで読む処理が無く、CLAUDE.md の凍結ゲートB
(戦略ロック) を評価するコードも無かった (ループが閉じていない)。本モジュールは週次ドリフト
メールから呼ばれ、以下を出す。購入判断ではなく「観測として次へ進める/やめる」ための計器。

1. shadow 前向き成績 (週別 + 累計): 対数損失 (p_win/p_top2/p_top3) と、make_picks と同じ
   買い目規則の的中率・ROI (100 円 / レース)。確定払戻で清算 = 執行ウェッジ込みの実現値。
2. 凍結ゲートB: 事前登録した戦略 (POLICIES) × 取得時点 (offset) ごとに、
   選択レース >= 200 かつ 4 週以上、全戦略×取得時点の比較に BH-FDR 補正 (q < 0.10)、
   ROI > 0 を要求する。ゲートA を満たした (offset, 券種) だけが候補 (満たさない組は参考値)。
3. 打ち切り条件 (SUNSET_*): 期限と件数に到達したら週次メールに「判定の時期」を出す。

official 行の選択規則 (OFFICIAL_DEDUP_RULE): run_type=official の行だけを評価に使う。
同一 (race_date, place_code, race_no, car_no) が複数ある場合は predicted_at が最新の行を
採用する (rerun / legacy_unverifiable は事前時点性を主張できないため評価に使わない)。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

KEYS = ["race_date", "place_code", "race_no"]
CAR_KEYS = KEYS + ["car_no"]

OFFICIAL_DEDUP_RULE = ("run_type=official のみ。同一 (race_date,place_code,race_no,car_no) が"
                       "複数なら predicted_at が最新の行を採用")

# ─── 事前登録 (戦略ロック。変更するときは CHANGELOG に理由を残す) ─────────────
GATE_B_LOCK_DATE = "2026-11-13"          # この日以降のレースだけを OOS として数える
POLICY_BETS = ["SH2", "ST2"]             # Harville 組合せ確率で EV を作れる券種
POLICY_EV_THRESHOLDS = [1.00, 1.10, 1.20]  # 比較する閾値群 (全部 FDR の母集団に入れる)
POLICY_OFFSETS = [5, 3, 2, 1, 0.5]
GATE_B_MIN_RACES = 200                   # 戦略×取得時点ごとの独立な選択レース数
GATE_B_MIN_WEEKS = 4                     # 選択レースが存在する ISO 週の数
GATE_B_FDR_Q = 0.10

# ─── 打ち切り条件 (期限と件数) ──────────────────────────────────────────────
SUNSET_GATE_A_WEEKS = 12                 # LOCK から 12 週で ゲートA 合格が 0 なら取得運用の見直し
SUNSET_FINAL_WEEKS = 26                  # LOCK から 26 週で最終判定 (合格候補が無ければ打ち切り推奨)
SUNSET_RACES_PER_POLICY = 1000           # 全戦略で選択 1000 レースに届いても未合格なら打ち切り推奨


def _lock_date() -> date:
    return date.fromisoformat(GATE_B_LOCK_DATE)


def monday_of(d: str) -> str:
    x = date.fromisoformat(d)
    return (x - timedelta(days=x.weekday())).isoformat()


# ─── official 行の選択 ─────────────────────────────────────────────────────

def select_official(shadow: pd.DataFrame) -> pd.DataFrame:
    """OFFICIAL_DEDUP_RULE に従って評価対象の shadow 行を返す。"""
    if shadow is None or len(shadow) == 0:
        return pd.DataFrame(columns=list(shadow.columns) if shadow is not None else [])
    df = shadow[shadow["run_type"].astype(str) == "official"].copy()
    df = df.sort_values("predicted_at", kind="stable")
    df = df.drop_duplicates(subset=CAR_KEYS, keep="last")
    return df.reset_index(drop=True)


def _norm_keys(df: pd.DataFrame, cols=CAR_KEYS) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = df[c].astype(str)
    return df


# ─── 1. 前向き成績 ─────────────────────────────────────────────────────────

def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p.astype(float), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def build_picks(sh: pd.DataFrame) -> pd.DataFrame:
    """ml_baseline.make_picks と同じ規則で券種別の買い目を作る (同点は車番昇順で決定的)。

    SH2: p_top2 上位 2 車 (小=大) / ST2: p_win 1 位 -> p_top2 最上位 (1 位を除く)
    RH3: p_top3 上位 3 車 (昇順) / RT3: p_win 上位 3 車 (順序付き)
    """
    d = sh.copy()
    d["car_i"] = pd.to_numeric(d["car_no"], errors="coerce")
    for c in ["p_win", "p_top2", "p_top3"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    rows = []
    for key, g in d.groupby(KEYS, sort=False):
        g = g.dropna(subset=["car_i"])
        if len(g) < 3:
            continue
        w = g.sort_values(["p_win", "car_i"], ascending=[False, True])["car_i"].astype(int).tolist()
        t2 = g.sort_values(["p_top2", "car_i"], ascending=[False, True])["car_i"].astype(int).tolist()
        t3 = g.sort_values(["p_top3", "car_i"], ascending=[False, True])["car_i"].astype(int).tolist()
        first = w[0]
        second = [c for c in t2 if c != first][0]
        rows.append({
            "race_date": key[0], "place_code": key[1], "race_no": key[2],
            "SH2": "=".join(map(str, sorted(t2[:2]))),
            "ST2": f"{first}-{second}",
            "RH3": "=".join(map(str, sorted(t3[:3]))),
            "RT3": "-".join(map(str, w[:3])),
        })
    return pd.DataFrame(rows)


def _pay_map(payouts: pd.DataFrame) -> dict:
    p = payouts.copy()
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    p = p[p["payout"] > 0]
    p = p.drop_duplicates(subset=KEYS + ["bet_type", "kumi_ban"])
    return {(r.race_date, r.place_code, r.race_no, r.bet_type, r.kumi_ban): float(r.payout)
            for r in p.itertuples(index=False)}


def forward_metrics(shadow_off: pd.DataFrame, results: pd.DataFrame,
                    complete_races: set, payouts: pd.DataFrame) -> dict:
    """official shadow を確定結果・払戻で評価する。

    complete_races: race_completion で status=complete (着順・払戻が揃った) の (date,place,race)。
    返り値: {"weeks": [週別 dict], "total": dict}
    """
    sh = _norm_keys(shadow_off)
    res = _norm_keys(results, CAR_KEYS)
    sh = sh.merge(res[CAR_KEYS + ["tyaku"]], on=CAR_KEYS, how="left")
    sh["race_key"] = list(zip(sh["race_date"], sh["place_code"], sh["race_no"]))
    sh = sh[sh["race_key"].isin(complete_races)].copy()
    if len(sh) == 0:
        return {"weeks": [], "total": {"n_races": 0}}
    ty = pd.to_numeric(sh["tyaku"], errors="coerce")
    sh["y_win"] = (ty == 1).astype(int)
    sh["y_top2"] = ty.between(1, 2).astype(int)
    sh["y_top3"] = ty.between(1, 3).astype(int)
    pm = _pay_map(_norm_keys(payouts, KEYS + ["kumi_ban"]))
    picks = build_picks(sh)
    sh["week"] = sh["race_date"].map(monday_of)
    picks["week"] = picks["race_date"].map(monday_of)

    def summarize(s: pd.DataFrame, pk: pd.DataFrame) -> dict:
        out = {"n_races": int(pk.shape[0])}
        for t in ("win", "top2", "top3"):
            y = s[f"y_{t}"].to_numpy()
            p = pd.to_numeric(s[f"p_{t}"], errors="coerce").to_numpy()
            out[f"ll_{t}"] = _logloss(y, p) if len(y) else float("nan")
            out[f"ll_{t}_base"] = _logloss(y, np.full(len(y), y.mean())) if len(y) else float("nan")
        for bt in ("SH2", "ST2", "RH3", "RT3"):
            pays = [pm.get((r.race_date, r.place_code, r.race_no, bt, getattr(r, bt)), 0.0)
                    for r in pk.itertuples(index=False)]
            n = len(pays)
            out[f"{bt}_hit"] = (sum(1 for x in pays if x > 0) / n) if n else float("nan")
            out[f"{bt}_roi"] = (sum(pays) / (n * 100.0)) if n else float("nan")
        return out

    weeks = []
    for wk in sorted(sh["week"].unique()):
        r = summarize(sh[sh["week"] == wk], picks[picks["week"] == wk])
        r["week"] = wk
        weeks.append(r)
    total = summarize(sh, picks)
    return {"weeks": weeks, "total": total}


# ─── 2. 凍結ゲートB ───────────────────────────────────────────────────────

def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg の補正後 p 値 (q 値)。入力順を保つ。"""
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    q = [1.0] * n
    prev = 1.0
    for rank in range(n, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * n / rank)
        q[i] = min(1.0, prev)
    return q


def _t_pvalue_greater(x: np.ndarray) -> float:
    """H0: mean(x) <= 0 に対する片側 t 検定の p 値 (n<2 や分散 0 は 1.0)。"""
    n = len(x)
    if n < 2:
        return 1.0
    sd = float(np.std(x, ddof=1))
    if sd == 0:
        return 0.0 if float(np.mean(x)) > 0 else 1.0
    t = float(np.mean(x)) / (sd / math.sqrt(n))
    try:
        from scipy.stats import t as tdist
        return float(tdist.sf(t, n - 1))
    except Exception:  # scipy 不在時は正規近似
        return 0.5 * math.erfc(t / math.sqrt(2))


def harville_combos(probs: pd.DataFrame) -> pd.DataFrame:
    """per-car p_win -> SH2/ST2 の組合せ確率 (ev_prerace_pipeline.build_combo_probs と同じ式)。"""
    rows = []
    for (rd, pc, rn), g in probs.groupby(KEYS, sort=False):
        cars = pd.to_numeric(g["car_no"], errors="coerce").astype(int).tolist()
        pw = pd.to_numeric(g["p_win"], errors="coerce").to_numpy(dtype=float)
        s = pw.sum()
        if s <= 0 or len(cars) < 2:
            continue
        q = dict(zip(cars, pw / s))
        for i in cars:
            for j in cars:
                if i != j and 1 - q[i] > 0:
                    rows.append((rd, pc, rn, "ST2", f"{i}-{j}", q[i] * q[j] / (1 - q[i])))
        for a in range(len(cars)):
            for b in range(a + 1, len(cars)):
                i, j = cars[a], cars[b]
                p = 0.0
                if 1 - q[i] > 0:
                    p += q[i] * q[j] / (1 - q[i])
                if 1 - q[j] > 0:
                    p += q[j] * q[i] / (1 - q[j])
                lo, hi = sorted([i, j])
                rows.append((rd, pc, rn, "SH2", f"{lo}={hi}", p))
    return pd.DataFrame(rows, columns=KEYS + ["bet_type", "kumi_ban", "p_combo"])


def evaluate_policies(shadow_off: pd.DataFrame, odds: pd.DataFrame,
                      complete_races: set, payouts: pd.DataFrame,
                      gate_a: dict | None, lock_date: str = GATE_B_LOCK_DATE) -> list[dict]:
    """事前登録した (券種 × EV 閾値 × 取得時点) の全組を評価する。

    odds: race_odds_prerace の (KEYS, bet_type, kumi_ban, odds, snapshot_dt, target_offset_min)。
    EV = Harville 組合せ確率 × その取得時点のオッズ。EV >= 閾値の組合せを 100 円ずつ購入と見なし、
    確定払戻で清算する (= 約定ウェッジ込みの実現値)。検定はレース単位の損益に対する片側 t 検定、
    全組に BH-FDR。ゲートA 合格の (offset, 券種) 以外は verdict='A未通過' (参考値のみ)。
    """
    sh = _norm_keys(shadow_off)
    sh = sh[sh["race_date"] >= lock_date]
    sh = sh[[(a, b, c) in complete_races for a, b, c in zip(sh["race_date"], sh["place_code"], sh["race_no"])]]
    combos = harville_combos(sh) if len(sh) else pd.DataFrame(columns=KEYS + ["bet_type", "kumi_ban", "p_combo"])
    combos = _norm_keys(combos, KEYS + ["kumi_ban"])
    pm = _pay_map(_norm_keys(payouts, KEYS + ["kumi_ban"]))

    od = _norm_keys(odds, KEYS + ["kumi_ban"]) if len(odds) else odds
    if len(od):
        od = od.copy()
        od["odds"] = pd.to_numeric(od["odds"], errors="coerce")
        od["off"] = pd.to_numeric(od["target_offset_min"], errors="coerce")
        od = od[(od["odds"] > 0) & od["bet_type"].isin(POLICY_BETS) & (od["race_date"] >= lock_date)]
        od = od.sort_values("snapshot_dt", kind="stable").drop_duplicates(
            subset=KEYS + ["bet_type", "kumi_ban", "off"], keep="last")

    rows: list[dict] = []
    for off in POLICY_OFFSETS:
        for bt in POLICY_BETS:
            if len(od):
                m = combos[combos["bet_type"] == bt].merge(
                    od[(od["off"] == float(off)) & (od["bet_type"] == bt)][KEYS + ["kumi_ban", "odds"]],
                    on=KEYS + ["kumi_ban"], how="inner")
                m["ev"] = m["p_combo"] * m["odds"]
            else:
                m = pd.DataFrame(columns=KEYS + ["kumi_ban", "odds", "ev"])
            a_pass = bool(gate_a and gate_a.get("offsets", {}).get(float(off), {})
                          .get("bet_verdicts", {}).get(bt)) if gate_a else False
            for thr in POLICY_EV_THRESHOLDS:
                sel = m[m["ev"] >= thr].copy() if len(m) else m
                n_bets = int(len(sel))
                if n_bets:
                    sel["pay"] = [pm.get((r.race_date, r.place_code, r.race_no, bt, r.kumi_ban), 0.0)
                                  for r in sel.itertuples(index=False)]
                    per_race = sel.groupby(KEYS).agg(pay=("pay", "sum"), n=("pay", "size")).reset_index()
                    per_race["net"] = per_race["pay"] - 100.0 * per_race["n"]
                    per_race["week"] = per_race["race_date"].map(monday_of)
                    n_races = int(len(per_race))
                    weeks = int(per_race["week"].nunique())
                    cost = 100.0 * n_bets
                    roi = float(per_race["pay"].sum() / cost - 1.0)
                    p = _t_pvalue_greater(per_race["net"].to_numpy(dtype=float))
                else:
                    n_races = weeks = 0
                    roi = float("nan")
                    p = 1.0
                rows.append({"policy_id": f"{bt}_ev{thr:.2f}", "bet": bt, "ev_thr": thr, "offset": off,
                             "n_races": n_races, "n_bets": n_bets, "weeks": weeks, "roi": roi,
                             "p": p, "a_pass": a_pass})
    qs = bh_fdr([r["p"] for r in rows])
    for r, q in zip(rows, qs):
        r["q"] = q
        if not r["a_pass"]:
            r["verdict"] = "A未通過 (参考値)"
        elif r["n_races"] < GATE_B_MIN_RACES or r["weeks"] < GATE_B_MIN_WEEKS:
            r["verdict"] = "蓄積中"
        elif r["roi"] > 0 and q < GATE_B_FDR_Q:
            r["verdict"] = "合格候補"
        else:
            r["verdict"] = "不合格"
    return rows


def sunset_status(policies: list[dict], gate_a_pass: int, today: date) -> dict:
    """打ち切り条件 (期限と件数) の到達状況。到達していれば messages に「判定の時期」を出す。"""
    lock = _lock_date()
    gate_a_deadline = lock + timedelta(weeks=SUNSET_GATE_A_WEEKS)
    final_deadline = lock + timedelta(weeks=SUNSET_FINAL_WEEKS)
    msgs: list[str] = []
    any_pass = any(p["verdict"] == "合格候補" for p in policies)
    if any_pass:
        msgs.append("🟢 ゲートB 合格候補あり: 戦略ロックの判定の時期 (購入開始は別判断)")
    if today >= gate_a_deadline and gate_a_pass == 0:
        msgs.append(f"🟠 判定の時期: ゲートA が {SUNSET_GATE_A_WEEKS} 週 ({gate_a_deadline}) 経過しても"
                    "合格 0 → 取得運用 (PC 稼働・S4U 化) の見直しか観測打ち切りを判断")
    cand = [p for p in policies if p["a_pass"]]
    if (not any_pass and policies
            and all(p["n_races"] >= SUNSET_RACES_PER_POLICY for p in policies)):
        msgs.append(f"🔴 判定の時期: 全戦略で選択 {SUNSET_RACES_PER_POLICY} レースに到達しても合格なし"
                    " → 打ち切り推奨 (件数条件)")
    if today >= final_deadline and not any_pass:
        msgs.append(f"🔴 判定の時期: 期限 {final_deadline} ({SUNSET_FINAL_WEEKS} 週) に到達、合格候補なし"
                    " → 打ち切り推奨 (期限条件)")
    return {"lock_date": lock.isoformat(), "gate_a_deadline": gate_a_deadline.isoformat(),
            "final_deadline": final_deadline.isoformat(), "messages": msgs,
            "n_a_pass_policies": len(cand)}


# ─── 3. 実データでの評価 (週次メールから呼ぶ) ───────────────────────────────

def evaluate(data_dir, gate_a: dict | None, today: date | None = None) -> dict:
    """D: のファイルを読んで forward_metrics / policies / sunset をまとめて返す。"""
    from pathlib import Path
    from src.odds_prerace import read_prerace
    today = today or date.today()
    d = Path(data_dir)
    sh = pd.read_csv(d / "shadow_predictions.csv", dtype=str, low_memory=False)
    off = select_official(sh)
    comp = pd.read_csv(d / "race_completion.csv", dtype=str, usecols=KEYS + ["status"])
    complete = set(zip(*[comp.loc[comp["status"] == "complete", k] for k in KEYS]))
    res = pd.read_csv(d / "race_results.csv", dtype=str, usecols=CAR_KEYS + ["tyaku"], low_memory=False)
    res = res[res["race_date"] >= (off["race_date"].min() if len(off) else "9999")]
    pay = pd.read_csv(d / "payouts.csv", dtype=str, low_memory=False,
                      usecols=KEYS + ["bet_type", "kumi_ban", "payout"])
    pay = pay[pay["race_date"] >= (off["race_date"].min() if len(off) else "9999")]
    fm = forward_metrics(off, res, complete, pay)
    jk = KEYS + ["bet_type", "kumi_ban"]
    odds = read_prerace(usecols=jk + ["odds", "snapshot_dt", "target_offset_min"],
                        dtype={k: str for k in jk}, start=GATE_B_LOCK_DATE, data_dir=d)
    pol = evaluate_policies(off, odds, complete, pay, gate_a)
    n_pass = 0
    if gate_a:
        n_pass = sum(1 for v in gate_a["offsets"].values()
                     for b in v["bet_verdicts"].values() if b)
    return {"forward": fm, "policies": pol, "sunset": sunset_status(pol, n_pass, today),
            "n_official_rows": int(len(off)),
            "n_official_days": int(off["race_date"].nunique()) if len(off) else 0}


# ─── 表示 (md / html) ──────────────────────────────────────────────────────

def _pct(x) -> str:
    return "—" if x is None or x != x else f"{x * 100:.1f}%"


def md_lines(res: dict | None, err: str = "") -> list[str]:
    L = ["## 10. shadow 前向き評価と凍結ゲートB", ""]
    if res is None:
        L += [f"⚠️ 評価に失敗しました: {err}", ""]
        return L
    fm, pol, sun = res["forward"], res["policies"], res["sunset"]
    L.append(f"対象: 朝の official 予測のみ (rerun / legacy は除外)。{OFFICIAL_DEDUP_RULE}。"
             f"official {res['n_official_days']} 日 / {res['n_official_rows']:,} 行。"
             "清算は確定払戻 (執行ウェッジ込み)、100 円/レース。")
    L.append("")
    t = fm["total"]
    if t.get("n_races", 0) == 0:
        L += ["(評価できる確定済みレースなし)", ""]
    else:
        L.append("| 週 (月曜) | R数 | 対数損失 勝/連対2/3着内 (基準比) | 二車複 的中/ROI | 二車単 的中/ROI | 三連複 的中/ROI | 三連単 的中/ROI |")
        L.append("|---|---:|---|---|---|---|---|")
        for r in fm["weeks"][-8:] + [dict(t, week="累計")]:
            ll = " / ".join(f"{r[f'll_{k}']:.4f}({r[f'll_{k}'] - r[f'll_{k}_base']:+.4f})"
                            for k in ("win", "top2", "top3"))
            cells = " | ".join(f"{_pct(r[f'{b}_hit'])} / {_pct(r[f'{b}_roi'])}"
                               for b in ("SH2", "ST2", "RH3", "RT3"))
            L.append(f"| {r['week']} | {r['n_races']} | {ll} | {cells} |")
        L.append("")
        L.append("対数損失のカッコ内は「定数予測 (的中率の平均)」との差 (負 = 予測に情報あり)。"
                 "ROI 100% が損益分岐 (控除率 約25%)。")
    L.append("")
    L.append(f"### ゲートB (戦略ロック {sun['lock_date']} 以降の OOS のみ)")
    L.append("")
    L.append(f"条件: 戦略×取得時点ごとに選択レース >= {GATE_B_MIN_RACES} かつ {GATE_B_MIN_WEEKS} 週以上、"
             f"全 {len(pol)} 組に BH-FDR (q < {GATE_B_FDR_Q})、ROI > 0。ゲートA 合格の組だけが判定対象。")
    L.append("")
    shown = [p for p in pol if p["n_races"]]
    if not shown:
        L.append(f"全 {len(pol)} 組とも選択レース 0 件 (ロック日以降の確定レースが未蓄積、または EV 条件を満たす買い目なし)。")
    else:
        L.append("| 戦略 | 取得時点 | 選択R | 週 | ROI | q | 判定 |")
        L.append("|---|---:|---:|---:|---:|---:|---|")
    for p in shown:
        roi = "—" if p["roi"] != p["roi"] else f"{p['roi'] * 100:+.1f}%"
        L.append(f"| {p['policy_id']} | {p['offset']} | {p['n_races']} | {p['weeks']} | {roi} | "
                 f"{p['q']:.3f} | {p['verdict']} |")
    L.append("")
    L.append(f"打ち切り条件: ゲートA 期限 {sun['gate_a_deadline']} / 最終判定 {sun['final_deadline']} / "
             f"全戦略で選択 {SUNSET_RACES_PER_POLICY} レース。")
    for m in sun["messages"]:
        L.append(f"- **{m}**")
    if not sun["messages"]:
        L.append("- 判定の時期には未到達 (蓄積中)。")
    L.append("")
    return L


def html_lines(res: dict | None, err: str, tbl: str, th: str, th_r: str, td) -> list[str]:
    p = ['<h3 style="color:#444; margin:18px 0 8px 0;">10. shadow 前向き評価と凍結ゲートB</h3>']
    if res is None:
        p.append(f'<p style="color:#d32f2f;">⚠️ 評価に失敗しました: {err}</p>')
        return p
    fm, pol, sun = res["forward"], res["policies"], res["sunset"]
    t = fm["total"]
    p.append(f'<p style="color:#666; font-size:12px;">official 予測のみ ({res["n_official_days"]} 日 / '
             f'{res["n_official_rows"]:,} 行)。確定払戻で清算 (執行ウェッジ込み)、100円/レース。</p>')
    if t.get("n_races", 0):
        p.append(f'<table {tbl}><tr><th {th}>週</th><th {th_r}>R数</th>'
                 f'<th {th}>対数損失 勝/連対2/3着内</th>'
                 + "".join(f'<th {th}>{n} 的中/ROI</th>' for n in ("二車複", "二車単", "三連複", "三連単"))
                 + '</tr>')
        for r in fm["weeks"][-8:] + [dict(t, week="累計")]:
            ll = " / ".join(f"{r[f'll_{k}']:.4f}" for k in ("win", "top2", "top3"))
            p.append("<tr>" + td(r["week"]) + td(str(r["n_races"]), right=True) + td(ll)
                     + "".join(td(f"{_pct(r[f'{b}_hit'])} / {_pct(r[f'{b}_roi'])}")
                               for b in ("SH2", "ST2", "RH3", "RT3")) + "</tr>")
        p.append('</table>')
    else:
        p.append('<p style="color:#999;">評価できる確定済みレースなし</p>')
    n_cand = sum(1 for x in pol if x["verdict"] == "合格候補")
    n_a = sum(1 for x in pol if x["a_pass"])
    p.append(f'<p><b>ゲートB</b> (ロック {sun["lock_date"]} 以降): 合格候補 <b>{n_cand}</b> / '
             f'判定対象 (ゲートA合格) {n_a} / 全 {len(pol)} 組。'
             f'条件: 選択R>={GATE_B_MIN_RACES}・{GATE_B_MIN_WEEKS}週以上・FDR q&lt;{GATE_B_FDR_Q}・ROI&gt;0。</p>')
    best = sorted([x for x in pol if x["n_races"]], key=lambda x: x["q"])[:5]
    if best:
        p.append(f'<table {tbl}><tr><th {th}>戦略</th><th {th_r}>取得時点</th><th {th_r}>選択R</th>'
                 f'<th {th_r}>週</th><th {th_r}>ROI</th><th {th_r}>q</th><th {th}>判定</th></tr>')
        for x in best:
            p.append("<tr>" + td(x["policy_id"]) + td(str(x["offset"]), right=True)
                     + td(str(x["n_races"]), right=True) + td(str(x["weeks"]), right=True)
                     + td("—" if x["roi"] != x["roi"] else f"{x['roi'] * 100:+.1f}%", right=True)
                     + td(f"{x['q']:.3f}", right=True) + td(x["verdict"]) + "</tr>")
        p.append('</table>')
    for m in sun["messages"]:
        p.append(f'<p style="color:#c62828;"><b>{m}</b></p>')
    if not sun["messages"]:
        p.append(f'<p style="color:#666; font-size:12px;">判定の時期には未到達。打ち切り条件: '
                 f'ゲートA 期限 {sun["gate_a_deadline"]} / 最終判定 {sun["final_deadline"]} / '
                 f'全戦略で選択 {SUNSET_RACES_PER_POLICY} レース。</p>')
    return p
