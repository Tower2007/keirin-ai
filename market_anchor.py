"""市場基準の 1 着確率 (計器) — 二車単の直前オッズ + べき乗補正 α (2026-11-12)
=======================================================================
背景 (統合監視 精度検証 2026-11-12 Keirin C/E):
  朝の official shadow (オッズ非依存モデル) を、直前の二車単 (ST2, dedup 後 = 締切に最も近い
  snapshot、通常 T-0.5) から出した市場の 1 着確率と比べると、前向き 7/13〜11/11 の 8,746 R で
    1 着 二値 logloss: モデル単体 0.3256 / 市場 0.2992 / 結合 0.2993 (モデル係数 0.119→0.006)
    レース単位 1 着対数尤度: モデル 1.486 / 市場 1.352、市場を q^α (α≈1.1) で補正すると -0.0024/R
  モデルは市場に足す情報を持たない (7/3 の market_blend_eval と同じ結論の前向き再確認)。

本モジュール (計器のみ。予測の本線 = 朝のオッズ非依存モデルは止めない・差し替えない):
  - 市場 1 着確率: ST2 の 1/odds をレース内で合計 1 に正規化し、1 着側で周辺化。出走全車に
    市場確率が付かないレースは使わない。
  - α: 「前月までのデータ」でレース単位 1 着対数尤度を最大化するべき乗 (p ∝ q^α)。月ごとに
    当てはめて履歴を残し、翌月で評価する (前向き)。
  - 結合: 条件付きロジット (p ∝ q^a · m^b、m = モデル p_win) の係数 b を同じく前月までで当てはめ、
    レース単位ブートストラップで b の 95%CI を出す (0 と区別できるか)。
  - 履歴: DATA_DIR/market_anchor_history.json (月ごと。既存月は書き換えない)。
  - 週次メール §10 (shadow_eval) に 2 行。

pythonw 起動でも落ちないよう print は使わない。ネットワーク不要。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

KEYS = ["race_date", "place_code", "race_no"]
CAR_KEYS = KEYS + ["car_no"]
HISTORY_NAME = "market_anchor_history.json"
START_MONTH = "2026-08"       # 前向き評価の最初の適用月 (official shadow は 7/11〜)
MIN_TRAIN_RACES = 500
N_BOOT = 100


# ---------------------------------------------------------------- 市場確率
def market_win_from_st2(odds: pd.DataFrame) -> pd.DataFrame:
    """dedup 済み直前オッズ (bet_type=ST2) → {KEYS, car_no, mkt_win}。"""
    o = odds[odds["bet_type"] == "ST2"].copy()
    o["odds"] = pd.to_numeric(o["odds"], errors="coerce")
    o = o[o["odds"] > 0]
    if o.empty:
        return pd.DataFrame(columns=CAR_KEYS + ["mkt_win"])
    o["car_no"] = pd.to_numeric(o["kumi_ban"].astype(str).str.split("-").str[0], errors="coerce")
    o = o.dropna(subset=["car_no"])
    o["q"] = 1.0 / o["odds"]
    o["q"] = o["q"] / o.groupby(KEYS)["q"].transform("sum")
    w = o.groupby(KEYS + ["car_no"])["q"].sum().rename("mkt_win").reset_index()
    for c in ("place_code", "race_no", "car_no"):
        w[c] = pd.to_numeric(w[c], errors="coerce").astype("Int64")
    w["race_date"] = w["race_date"].astype(str)
    return w


def build_frame(shadow_off: pd.DataFrame, results: pd.DataFrame, odds: pd.DataFrame,
                complete_races: set | None = None) -> pd.DataFrame:
    """official shadow × 着順 × 市場。1 着がちょうど 1 車・全車に市場確率があるレースのみ。"""
    sh = shadow_off[CAR_KEYS + ["p_win"]].copy()
    res = results[CAR_KEYS + ["tyaku"]].copy()
    for df in (sh, res):
        for c in ("place_code", "race_no", "car_no"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        df["race_date"] = df["race_date"].astype(str)
    d = sh.merge(res.drop_duplicates(CAR_KEYS), on=CAR_KEYS, how="inner")
    d = d.dropna(subset=["place_code", "race_no", "car_no"])
    if complete_races is not None:
        cr = {(str(a), int(b), int(c)) for a, b, c in complete_races
              if str(b).strip().isdigit() and str(c).strip().isdigit()}
        rk = list(zip(d["race_date"], d["place_code"].astype(int), d["race_no"].astype(int)))
        d = d[[k in cr for k in rk]]
    d["win"] = (pd.to_numeric(d["tyaku"], errors="coerce") == 1).astype(int)
    d["p_win"] = pd.to_numeric(d["p_win"], errors="coerce")
    d = d[d.groupby(KEYS)["win"].transform("sum") == 1]
    d = d.merge(market_win_from_st2(odds), on=CAR_KEYS, how="left")
    d = d[d.groupby(KEYS)["mkt_win"].transform(lambda s: s.notna().all())]
    d = d[d["p_win"].notna() & (d["p_win"] > 0)].copy()
    d["race_id"] = (d["race_date"] + "_" + d["place_code"].astype(int).astype(str) + "_"
                    + d["race_no"].astype(int).astype(str))
    d["month"] = d["race_date"].str[:7]
    return d.reset_index(drop=True)


# ---------------------------------------------------------------- 条件付きロジット
class _Races:
    """レース単位の条件付きロジット計算用 (race_id で整列した配列 + reduceat の区切り)。"""

    def __init__(self, d: pd.DataFrame | None = None):
        if d is None:
            return
        d = d.sort_values(["race_id", "car_no"]).reset_index(drop=True)
        codes, _ = pd.factorize(d["race_id"])
        self.starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]]) if len(d) else np.array([], int)
        self.lens = np.diff(np.r_[self.starts, len(d)]) if len(d) else np.array([], int)
        self.win = d["win"].to_numpy(int)
        self.lq = np.log(np.clip(d["mkt_win"].to_numpy(float), 1e-9, 1))
        self.lm = np.log(np.clip(d["p_win"].to_numpy(float), 1e-9, 1))

    def subset(self, race_idx: np.ndarray) -> "_Races":
        obj = _Races()
        rows = np.concatenate([np.arange(self.starts[i], self.starts[i] + self.lens[i])
                               for i in race_idx])
        obj.lens = self.lens[race_idx]
        obj.starts = np.r_[0, np.cumsum(obj.lens)[:-1]]
        obj.win, obj.lq, obj.lm = self.win[rows], self.lq[rows], self.lm[rows]
        return obj

    def nll_grad(self, a: float, b: float) -> tuple[float, np.ndarray]:
        """平均 -log p(勝者) と (a, b) 勾配。"""
        z = a * self.lq + b * self.lm
        zmax = np.repeat(np.maximum.reduceat(z, self.starts), self.lens)
        e = np.exp(z - zmax)
        p = e / np.repeat(np.add.reduceat(e, self.starts), self.lens)
        n = len(self.lens)
        w = self.win == 1
        nll = float(-np.sum(np.log(np.clip(p[w], 1e-300, 1))) / n)
        ga = -(np.sum(self.lq[w]) - np.sum(p * self.lq)) / n
        gb = -(np.sum(self.lm[w]) - np.sum(p * self.lm)) / n
        return nll, np.array([ga, gb])


def _binary_ll(y, p) -> float:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _fit(r: _Races, with_model: bool) -> tuple[float, float]:
    from scipy.optimize import minimize
    if with_model:
        res = minimize(lambda v: r.nll_grad(v[0], v[1]), x0=[1.0, 0.0], jac=True,
                       method="L-BFGS-B")
        return float(res.x[0]), float(res.x[1])

    def f(v):
        nll, g = r.nll_grad(v[0], 0.0)
        return nll, g[:1]
    res = minimize(f, x0=[1.0], jac=True, method="L-BFGS-B", bounds=[(0.3, 3.0)])
    return float(res.x[0]), 0.0


def fit_alpha(d: pd.DataFrame) -> float:
    """市場だけのべき乗補正 α (p ∝ q^α)。"""
    return _fit(_Races(d), with_model=False)[0]


def fit_blend(d: pd.DataFrame) -> tuple[float, float]:
    """条件付きロジット p ∝ q^a · m^b の (a, b)。"""
    return _fit(_Races(d), with_model=True)


def bootstrap_b(d: pd.DataFrame, n_boot: int = N_BOOT, seed: int = 0) -> tuple[float, float] | None:
    """レース単位ブートストラップで b の 95%CI。"""
    r = _Races(d)
    n = len(r.lens)
    if n == 0:
        return None
    rng = np.random.default_rng(seed)
    bs = [_fit(r.subset(rng.integers(0, n, n)), with_model=True)[1] for _ in range(n_boot)]
    return float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


# ---------------------------------------------------------------- 月次履歴
def _months(a: str, b: str) -> list[str]:
    y, m = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def load_history(data_dir: Path) -> list[dict]:
    p = Path(data_dir) / HISTORY_NAME
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("history", [])
    except Exception:
        return []


def save_history(data_dir: Path, history: list[dict]) -> Path:
    p = Path(data_dir) / HISTORY_NAME
    doc = {"note": ("計器のみ: 市場 (ST2 直前) の 1 着確率のべき乗補正 α と、モデルとの結合係数 b。"
                    "apply_month の値は apply_month 1 日より前のデータだけで当てはめた (前向き)。"),
           "history": sorted(history, key=lambda e: e["apply_month"])}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def refit_missing_months(d: pd.DataFrame, history: list[dict], today: date,
                         n_boot: int = N_BOOT) -> tuple[list[dict], list[str]]:
    """START_MONTH〜今月のうち履歴に無い月を、その月 1 日より前のデータで当てはめて足す。"""
    have = {e["apply_month"] for e in history}
    added = []
    for mo in _months(START_MONTH, f"{today:%Y-%m}"):
        if mo in have:
            continue
        tr = d[d["race_date"] < f"{mo}-01"]
        n_r = int(tr["race_id"].nunique()) if len(tr) else 0
        if n_r < MIN_TRAIN_RACES:
            continue
        a, b = fit_blend(tr)
        ci = bootstrap_b(tr, n_boot=n_boot)
        history.append({
            "apply_month": mo, "fitted_on": today.isoformat(), "alpha": fit_alpha(tr),
            "blend_a": a, "blend_b": b,
            "blend_b_ci_low": ci[0] if ci else None, "blend_b_ci_high": ci[1] if ci else None,
            "train_races": n_r, "train_date_min": str(tr["race_date"].min()),
            "train_date_max": str(tr["race_date"].max()),
        })
        added.append(mo)
    return history, added


def evaluate_forward(d: pd.DataFrame, history: list[dict]) -> dict:
    """各月をその月の係数 (前月までで当てはめ) で評価し合算する。"""
    by_month = {e["apply_month"]: e for e in history}
    parts = []
    for mo, g in d.groupby("month"):
        e = by_month.get(mo)
        if e is None:
            continue
        r = _Races(g)
        parts.append({
            "month": mo, "n_races": int(len(r.lens)), "n_rows": int(len(g)),
            "rl_model": r.nll_grad(0.0, 1.0)[0], "rl_mkt": r.nll_grad(1.0, 0.0)[0],
            "rl_mkt_alpha": r.nll_grad(e["alpha"], 0.0)[0],
            "rl_blend": r.nll_grad(e["blend_a"], e["blend_b"])[0],
            "bl_model": _binary_ll(g["win"], g["p_win"]),
            "bl_mkt": _binary_ll(g["win"], g["mkt_win"]),
        })
    if not parts:
        return {"n_races": 0}
    n = sum(p["n_races"] for p in parts)
    nr = sum(p["n_rows"] for p in parts)
    out: dict = {"n_races": n, "months": [p["month"] for p in parts], "by_month": parts}
    for k in ("rl_model", "rl_mkt", "rl_mkt_alpha", "rl_blend"):
        out[k] = sum(p[k] * p["n_races"] for p in parts) / n
    for k in ("bl_model", "bl_mkt"):
        out[k] = sum(p[k] * p["n_rows"] for p in parts) / nr
    return out


def run(data_dir: Path, shadow_off: pd.DataFrame, results: pd.DataFrame,
        complete_races: set | None, today: date | None = None,
        write: bool = True) -> dict:
    """週次から呼ぶ: データ構築 → 未作成月の当てはめ (履歴へ追記) → 前向き評価。"""
    from src.odds_prerace import load_prerace_dedup
    today = today or date.today()
    start = str(shadow_off["race_date"].min()) if len(shadow_off) else None
    odds, _ = load_prerace_dedup(start=start, data_dir=Path(data_dir), today=today)
    d = build_frame(shadow_off, results, odds, complete_races)
    hist = load_history(data_dir)
    hist, added = refit_missing_months(d, hist, today)
    if added and write:
        save_history(data_dir, hist)
    ev = evaluate_forward(d, hist)
    ev["latest"] = max(hist, key=lambda e: e["apply_month"]) if hist else None
    ev["added"] = added
    return ev


def lines(res: dict | None) -> list[str]:
    """週次メール用の行 (md/html 共通のプレーン文)。"""
    if not res:
        return []
    if not res.get("n_races"):
        return ["市場基準の確率 (計器): 前向き評価できる月がまだありません (前月までの学習 "
                f"{MIN_TRAIN_RACES} R 以上が必要)"]
    out = [(f"市場基準の確率 (計器、前向き {res['n_races']:,} R / {res['months'][0]}〜{res['months'][-1]}): "
            f"1 着のレース単位対数損失 モデル単体 {res['rl_model']:.4f} ・ 市場 {res['rl_mkt']:.4f} ・ "
            f"補正後市場 {res['rl_mkt_alpha']:.4f} ・ 結合 {res['rl_blend']:.4f} (低いほど良い。"
            f"二値 logloss はモデル {res['bl_model']:.4f} / 市場 {res['bl_mkt']:.4f})")]
    e = res.get("latest") or {}
    if e:
        lo, hi = e.get("blend_b_ci_low"), e.get("blend_b_ci_high")
        sep = ("0 と区別できる" if lo is not None and (lo > 0 or hi < 0)
               else "0 と区別できない (モデルの上積みの証拠なし)")
        ci = f"[{lo:+.3f}, {hi:+.3f}]" if lo is not None else "[算出不可]"
        out.append(f"結合時のモデル係数 ({e['apply_month']} 適用、〜{e['train_date_max']} で当てはめ): "
                   f"b = {e['blend_b']:+.3f} 95%CI {ci} = {sep} / 市場の補正 α = {e['alpha']:.3f}")
    return out
