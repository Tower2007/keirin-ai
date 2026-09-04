"""直前オッズ (race_odds_prerace) の月次パーティション読み書きヘルパ (2026-09-04)

背景:
  単一ファイル race_odds_prerace.csv は 800MB / 13.5M 行 (+230MB/月) まで育ち、
  週次の ev_prerace_pipeline が全読み (read 11.6s / RAM 722MB) していた。
  月次分割 + 確定月の dedup キャッシュで、読む量を「必要な月」に限定する。

ファイル配置 (DATA_DIR 直下):
  race_odds_prerace_YYYY-MM.csv   … 月次パーティション (デーモンの追記先)
  race_odds_prerace.csv           … 旧単一ファイル。移行後は通常存在しない。
                                    残っていれば読み手はこれも読む (移行前後の互換)
  cache/odds_prerace_dedup_YYYY-MM.parquet (+ .json)
                                  … 確定月 (当月より前) の dedup 結果キャッシュ

パーティション鍵 = **race_date の年月** (snapshot_dt ではない)。理由:
  1. 読み手は全て race_date で期間指定する (--start/--end、週次 28 日窓)。
     race_date 鍵なら「読むべきファイル」を開かずに決められる。
  2. dedup 鍵 (race_date, place_code, race_no, bet_type, kumi_ban) に race_date を
     含むため、レースの全 snapshot が同一ファイルに閉じる → 月ごとの独立 dedup が
     全体 dedup と厳密に一致し、月単位キャッシュが正確になる。
  3. デーモンは当日レースを当日に撮るので実運用上 snapshot_dt と race_date の
     日付は同じ。差が出るのは --date で過去日を再取得したときだけで、その場合も
     race_date 鍵の方が読み手にとって都合が良い。

dedup の意味論 (ev_prerace_pipeline.load_prerace_dedup と同一、不変):
  odds > 0 の行だけ残し、(race_date, place_code, race_no, bet_type, kumi_ban) 毎に
  snapshot_dt が最新の 1 行を採用する (= 最も締切に近い snapshot)。
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

from .config import DATA_DIR
from .storage import CSV_SCHEMAS, PRERACE_LEGACY_NAME, PRERACE_MONTHLY_RE

LEGACY_NAME = PRERACE_LEGACY_NAME
MONTHLY_RE = PRERACE_MONTHLY_RE
MONTHLY_GLOB = "race_odds_prerace_*.csv"
CACHE_DIRNAME = "cache"
CACHE_STEM = "odds_prerace_dedup_"

KEYS = ["race_date", "place_code", "race_no"]
JOIN_KEYS = KEYS + ["bet_type", "kumi_ban"]
DEDUP_COLS = JOIN_KEYS + ["odds", "snapshot_dt", "target_offset_min"]
_STR_DTYPE = {k: str for k in JOIN_KEYS}

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})")


# ─── 命名 ────────────────────────────────────────────────────────────────

def month_of(race_date: str) -> str:
    """'2026-09-04' → '2026-09'。形式不正は ValueError。"""
    m = _MONTH_RE.match(str(race_date).strip())
    if not m:
        raise ValueError(f"race_date の形式が不正: {race_date!r}")
    return f"{m.group(1)}-{m.group(2)}"


def monthly_name(race_date: str) -> str:
    """race_date に対応する月次パーティションのファイル名。"""
    return f"race_odds_prerace_{month_of(race_date)}.csv"


def month_from_name(name: str) -> str | None:
    """'race_odds_prerace_2026-09.csv' → '2026-09'。月次名でなければ None。"""
    m = MONTHLY_RE.match(name)
    return m.group(1) if m else None


def _month_in_range(month: str, start: str | None, end: str | None) -> bool:
    """月 'YYYY-MM' が [start, end] (YYYY-MM-DD, 閉区間) と交差するか。"""
    if start and month < month_of(start):
        return False
    if end and month > month_of(end):
        return False
    return True


def monthly_files(data_dir: Path = DATA_DIR) -> dict[str, Path]:
    """{ 'YYYY-MM': Path } (存在する月次ファイルのみ)。"""
    out: dict[str, Path] = {}
    for p in Path(data_dir).glob(MONTHLY_GLOB):
        mon = month_from_name(p.name)
        if mon:
            out[mon] = p
    return out


def legacy_file(data_dir: Path = DATA_DIR) -> Path | None:
    """旧単一ファイルが残っていれば Path、無ければ None。"""
    p = Path(data_dir) / LEGACY_NAME
    return p if p.exists() and p.stat().st_size > 0 else None


def list_partition_files(
    data_dir: Path = DATA_DIR,
    start: str | None = None,
    end: str | None = None,
    include_legacy: bool = True,
) -> list[Path]:
    """読むべきファイル一覧 (月次昇順、最後に旧単一ファイル)。

    旧ファイルは全期間を含み得るので期間で除外せず常に含める (行は後で
    race_date でフィルタ)。
    """
    files = [p for mon, p in sorted(monthly_files(data_dir).items())
             if _month_in_range(mon, start, end)]
    if include_legacy:
        legacy = legacy_file(data_dir)
        if legacy is not None:
            files.append(legacy)
    return files


# ─── 読み込み ─────────────────────────────────────────────────────────────

def _filter_dates(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df[df["race_date"] >= start]
    if end:
        df = df[df["race_date"] <= end]
    return df


def _empty(cols: list[str], dtype: dict) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=dtype.get(c, object)) for c in cols})


def read_prerace(
    usecols: list[str] | None = None,
    dtype: dict | None = None,
    start: str | None = None,
    end: str | None = None,
    data_dir: Path = DATA_DIR,
    files: Iterable[Path] | None = None,
) -> pd.DataFrame:
    """必要な月のパーティション (+旧ファイル) を読んで 1 つの DataFrame にする。

    pd.read_csv(DATA_DIR / "race_odds_prerace.csv", usecols=..., dtype=...) の
    置き換え。start/end (race_date, 閉区間) で読むファイルと行を絞る。
    該当ファイルが無ければ usecols 列 (無指定ならスキーマ全列) の空 DataFrame。
    """
    dtype = dtype or {}
    if files is None:
        files = list_partition_files(data_dir, start, end)
    frames: list[pd.DataFrame] = []
    for path in files:
        df = pd.read_csv(path, usecols=usecols, dtype=dtype, low_memory=False)
        if "race_date" in df.columns:
            df = _filter_dates(df, start, end)
        if len(df):
            frames.append(df)
    if not frames:
        return _empty(usecols or CSV_SCHEMAS[LEGACY_NAME], dtype)
    if len(frames) == 1:
        return frames[0].reset_index(drop=True)
    return pd.concat(frames, ignore_index=True)


def iter_partition_rows(
    data_dir: Path = DATA_DIR,
    start: str | None = None,
    end: str | None = None,
) -> Iterator[dict]:
    """csv.DictReader 相当の逐次イテレータ (pandas を使わない小さなスクリプト用)。"""
    for path in list_partition_files(data_dir, start, end):
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rd = row.get("race_date", "")
                if (start and rd < start) or (end and rd > end):
                    continue
                yield row


# ─── dedup (最新 snapshot) + 確定月キャッシュ ─────────────────────────────

def dedup_latest(df: pd.DataFrame) -> pd.DataFrame:
    """(race,bet,kumi) 毎に snapshot_dt 最新の 1 行を残す (odds>0 のみ)。

    ev_prerace_pipeline.load_prerace_dedup (2026-06-06) の意味論そのもの。
    sort は stable にして同時刻タイの結果を決定的にする (旧実装では未定義)。
    """
    df = df.copy()
    df["odds"] = pd.to_numeric(df["odds"], errors="coerce")
    df = df[df["odds"] > 0]
    df = df.sort_values("snapshot_dt", kind="stable").drop_duplicates(
        subset=JOIN_KEYS, keep="last")
    return df.reset_index(drop=True)


def cache_paths(data_dir: Path, month: str) -> tuple[Path, Path]:
    cdir = Path(data_dir) / CACHE_DIRNAME
    return cdir / f"{CACHE_STEM}{month}.parquet", cdir / f"{CACHE_STEM}{month}.json"


def _fingerprint(paths: list[Path]) -> dict[str, list[int]]:
    """キャッシュ妥当性判定用: 元ファイルの (size, mtime_ns)。"""
    fp: dict[str, list[int]] = {}
    for p in paths:
        if p.exists():
            st = p.stat()
            fp[p.name] = [st.st_size, st.st_mtime_ns]
    return fp


def _write_cache(parquet: Path, meta: Path, df: pd.DataFrame,
                 fp: dict, n_raw: int) -> bool:
    try:
        parquet.parent.mkdir(parents=True, exist_ok=True)
        tmp = parquet.with_name(parquet.name + ".tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(parquet)
        meta.write_text(
            json.dumps({"sources": fp, "n_raw": int(n_raw), "n_rows": int(len(df))}),
            encoding="utf-8")
        return True
    except Exception:
        # pyarrow 不在・ディスク不調などはキャッシュ無しで続行 (結果は同じ)
        for p in (parquet, meta, parquet.with_name(parquet.name + ".tmp")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _read_cache(parquet: Path, meta: Path, fp: dict) -> tuple[pd.DataFrame, int] | None:
    if not (parquet.exists() and meta.exists()):
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        if info.get("sources") != fp:
            return None
        df = pd.read_parquet(parquet)
        if list(df.columns) != DEDUP_COLS:
            return None
        return df, int(info.get("n_raw", 0))
    except Exception:
        return None


def load_prerace_dedup(
    start: str | None = None,
    end: str | None = None,
    data_dir: Path = DATA_DIR,
    use_cache: bool = True,
    today: date | None = None,
    verbose: bool = False,
) -> tuple[pd.DataFrame, int]:
    """月次パーティションを dedup して結合。戻り値 (dedup 後 DataFrame, dedup 前の行数)。

    - 確定月 (today の年月より前) は dedup 結果を cache/ に parquet 保存し、
      元ファイルの size/mtime が一致する限り再利用する。
    - 当月 (と未来月) は毎回 CSV から dedup。
    - 旧単一ファイルが残っていれば、その行も race_date の月に振り分けて同じ扱い
      (フィンガープリントに旧ファイルも含めるため、旧ファイルが変化すれば
      該当月は再計算される)。
    - 月ごとに独立に dedup しても全体 dedup と一致する (鍵に race_date を含む)。
    """
    data_dir = Path(data_dir)
    today = today or date.today()
    cur_month = f"{today.year:04d}-{today.month:02d}"

    monthly = monthly_files(data_dir)
    legacy = legacy_file(data_dir)
    legacy_by_month: dict[str, pd.DataFrame] = {}
    if legacy is not None:
        ldf = pd.read_csv(legacy, usecols=DEDUP_COLS, dtype=_STR_DTYPE, low_memory=False)
        if len(ldf):
            for mon, g in ldf.groupby(ldf["race_date"].str.slice(0, 7)):
                legacy_by_month[str(mon)] = g

    months = [m for m in sorted(set(monthly) | set(legacy_by_month))
              if _month_in_range(m, start, end)]

    parts: list[pd.DataFrame] = []
    n_raw_total = 0
    for mon in months:
        sources = [p for p in (monthly.get(mon), legacy if mon in legacy_by_month else None) if p]
        cacheable = use_cache and mon < cur_month
        parquet, meta = cache_paths(data_dir, mon)
        fp = _fingerprint(sources) if cacheable else {}
        hit = _read_cache(parquet, meta, fp) if cacheable else None
        if hit is not None:
            dd, n_raw = hit
            if verbose:
                print(f"    {mon}: cache hit ({len(dd):,} 行)")
        else:
            frames: list[pd.DataFrame] = []
            if mon in monthly:
                mdf = pd.read_csv(monthly[mon], usecols=DEDUP_COLS,
                                  dtype=_STR_DTYPE, low_memory=False)
                if len(mdf):
                    frames.append(mdf)
            if mon in legacy_by_month:
                frames.append(legacy_by_month[mon])
            if not frames:
                continue
            raw = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
            n_raw = int(len(raw))
            dd = dedup_latest(raw)[DEDUP_COLS]
            saved = _write_cache(parquet, meta, dd, fp, n_raw) if cacheable else False
            if verbose:
                print(f"    {mon}: dedup {n_raw:,} → {len(dd):,} 行"
                      + (" (cache 保存)" if saved else ""))
        n_raw_total += n_raw
        parts.append(_filter_dates(dd, start, end))

    if not parts:
        out = _empty(DEDUP_COLS, _STR_DTYPE)
        out["odds"] = pd.to_numeric(out["odds"])
        return out, 0
    out = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    return out.reset_index(drop=True), n_raw_total
