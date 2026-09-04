"""race_odds_prerace.csv (旧単一ファイル) を月次パーティションへ一度だけ分割する移行スクリプト

  race_odds_prerace.csv  →  race_odds_prerace_YYYY-MM.csv (race_date の年月ごと)

設計 (2026-09-04):
- 逐次ストリーム (1 行ずつ) なので RAM は行数に依存しない (800MB / 13.5M 行でも数十 MB)。
- 元ファイルは削除せず `race_odds_prerace.csv.bak-YYYYMMDD-HHMMSS` にリネームして残す。
  リネームは src/storage._file_lock (デーモンと同じ .lock) を取ってから行うので、
  デーモンが追記中でも行が壊れない。リネーム後にデーモン (旧コード) が追記すると
  新しい race_odds_prerace.csv が再生成されるが、読み手は旧ファイルも読むので
  データは失われない。本スクリプトを再実行すればその残りも月次へ取り込まれる。
- 冪等: 旧ファイルが無ければ何もしない (exit 0)。月次ファイルが既にあれば
  ヘッダ抜きで追記する。書き込みは `.part` に出してから最後にまとめて反映するので、
  途中で落ちても本番ファイルは中途半端にならない (残った .part は次回起動時に破棄)。
- 検証: 元ファイルのデータ行数 == 各月へ書いた行数の合計、かつ各月次ファイルの
  行数増分 == 書いた行数、を確認してから `<bak>.migrated.json` を書く。不一致なら exit 1。
- race_date が YYYY-MM-DD でない行 (壊れた行) は race_odds_prerace_invalid-<ts>.csv へ退避。
- 事前にディスク空き容量が元ファイルサイズの 2 倍未満なら実行せず exit 2。

使い方:
  python scripts/partition_odds_prerace.py --dry-run     # 計画と月別行数だけ表示 (無書き込み)
  python scripts/partition_odds_prerace.py               # 実行
  python scripts/partition_odds_prerace.py --data-dir D:\\keirin-ai-data
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DATA_DIR  # noqa: E402
from src.storage import CSV_SCHEMAS, PRERACE_LEGACY_NAME, _file_lock  # noqa: E402

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-\d{2},")
HEADER = ",".join(CSV_SCHEMAS[PRERACE_LEGACY_NAME])
PROGRESS_EVERY = 1_000_000


def _count_data_lines(path: Path) -> int:
    """ヘッダを除いた行数 (空行は数えない)。"""
    if not path.exists():
        return 0
    n = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        first = f.readline()
        if not first:
            return 0
        for line in f:
            if line.strip():
                n += 1
    return n


def _scan(src: Path, sinks: dict[str, object] | None, log) -> tuple[dict[str, int], int]:
    """src を 1 行ずつ読み、月別行数 (と sinks があれば書き込み) を返す。

    戻り値: ({month: rows}, invalid_rows)。sinks は {month: writable} (None なら数えるだけ)。
    行はバイト同一で転記する (csv 再パースしない。schema に自由文字列列は無い)。
    """
    counts: dict[str, int] = {}
    invalid = 0
    t0 = time.monotonic()
    with open(src, "r", encoding="utf-8", newline="") as f:
        header = f.readline().rstrip("\r\n")
        if header != HEADER:
            raise SystemExit(f"ヘッダがスキーマと不一致:\n  got: {header}\n  exp: {HEADER}")
        total = 0
        for line in f:
            if not line.strip():
                continue
            m = DATE_RE.match(line)
            key = f"{m.group(1)}-{m.group(2)}" if m else "invalid"
            if key == "invalid":
                invalid += 1
            counts[key] = counts.get(key, 0) + 1
            if sinks is not None:
                sink = sinks.get(key)
                if sink is None:
                    sink = sinks["__open__"](key)
                    sinks[key] = sink
                sink.write(line if line.endswith("\n") else line + "\r\n")
            total += 1
            if total % PROGRESS_EVERY == 0:
                log(f"    ... {total:,} 行 ({time.monotonic() - t0:.0f}s)")
    counts.pop("invalid", None)
    return counts, invalid


def partition(data_dir: Path, dry_run: bool = False, log=print) -> dict:
    """移行本体。戻り値は summary dict (テストからも呼ぶ)。"""
    data_dir = Path(data_dir)
    src = data_dir / PRERACE_LEGACY_NAME
    summary: dict = {"data_dir": str(data_dir), "dry_run": dry_run}

    # 前回の中断で残った .part は破棄 (本番ファイルには未反映なので安全)
    for stale in data_dir.glob("race_odds_prerace_*.csv.part"):
        log(f"  前回残骸 .part を削除: {stale.name}")
        stale.unlink()

    if not src.exists() or src.stat().st_size == 0:
        log(f"  {src.name} は無い/空 → 移行済み。何もしない。")
        summary.update({"status": "nothing_to_do"})
        return summary

    size = src.stat().st_size
    free = shutil.disk_usage(data_dir).free
    log(f"  元ファイル: {src} ({size / 1024**2:,.1f} MB) / 空き {free / 1024**3:,.1f} GB")
    if free < size * 2:
        log(f"  [中止] 空き容量が元ファイルの 2 倍 ({size * 2 / 1024**3:,.1f} GB) 未満")
        summary.update({"status": "no_space", "size": size, "free": free})
        return summary

    if dry_run:
        log("  [dry-run] 月別行数を数えるだけ (書き込み無し)")
        counts, invalid = _scan(src, None, log)
        summary.update({"status": "dry_run", "months": counts, "invalid": invalid,
                        "n_src_rows": sum(counts.values()) + invalid})
        for mon, n in sorted(counts.items()):
            tgt = data_dir / f"race_odds_prerace_{mon}.csv"
            log(f"    {mon}: {n:,} 行 → {tgt.name}"
                + (f" (既存 {_count_data_lines(tgt):,} 行へ追記)" if tgt.exists() else " (新規)"))
        if invalid:
            log(f"    invalid: {invalid:,} 行 (race_date 不正) → race_odds_prerace_invalid-*.csv")
        log(f"    合計 {summary['n_src_rows']:,} 行")
        return summary

    # 1) 旧ファイルをロック下でリネーム (デーモンの追記と衝突しない)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = data_dir / f"{PRERACE_LEGACY_NAME}.bak-{stamp}"
    seq = 0
    while bak.exists():  # 同一秒内の再実行 (テスト等) で衝突しないように
        seq += 1
        stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{seq}"
        bak = data_dir / f"{PRERACE_LEGACY_NAME}.bak-{stamp}"
    with _file_lock(src):
        for attempt in range(10):
            try:
                src.rename(bak)
                break
            except PermissionError:
                time.sleep(0.5)  # 別プロセスが一瞬開いている可能性 → リトライ
        else:
            raise SystemExit(f"リネームできない (他プロセスが掴んでいる): {src}")
    log(f"  リネーム: {src.name} → {bak.name}")

    # 2) .bak を月別 .part へストリーム転記
    parts: dict[str, Path] = {}
    handles: dict[str, object] = {}

    def _open(key: str):
        name = (f"race_odds_prerace_{key}.csv.part" if key != "invalid"
                else f"race_odds_prerace_invalid-{stamp}.csv.part")
        p = data_dir / name
        h = open(p, "w", encoding="utf-8", newline="")
        h.write(HEADER + "\r\n")
        parts[key] = p
        return h

    handles["__open__"] = _open
    try:
        counts, invalid = _scan(bak, handles, log)
    finally:
        for k, h in list(handles.items()):
            if k != "__open__":
                h.close()
    n_src = sum(counts.values()) + invalid
    log(f"  転記: {n_src:,} 行 → {len(counts)} か月" + (f" + invalid {invalid:,}" if invalid else ""))

    # 3) .part を本番ファイルへ反映 (既存月次はヘッダ抜き追記、無ければリネーム)
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    for key, part in parts.items():
        if key == "invalid":
            final = data_dir / part.name[:-len(".part")]
            part.replace(final)
            log(f"    invalid → {final.name} ({invalid:,} 行)")
            continue
        target = data_dir / f"race_odds_prerace_{key}.csv"
        with _file_lock(target):
            if target.exists() and target.stat().st_size > 0:
                before[key] = _count_data_lines(target)
                with open(part, "r", encoding="utf-8", newline="") as pf, \
                        open(target, "a", encoding="utf-8", newline="") as tf:
                    pf.readline()  # ヘッダを捨てる
                    shutil.copyfileobj(pf, tf, 1024 * 1024)
                part.unlink()
            else:
                before[key] = 0
                part.replace(target)
            after[key] = _count_data_lines(target)
        log(f"    {key}: {before[key]:,} → {after[key]:,} 行 (+{counts[key]:,}) {target.name}")

    # 4) 検証
    ok = all(after[k] - before[k] == counts[k] for k in counts)
    summary.update({
        "status": "done" if ok else "count_mismatch",
        "bak": str(bak), "n_src_rows": n_src, "invalid": invalid,
        "months": counts, "before": before, "after": after,
        "n_written": sum(counts.values()),
    })
    if ok:
        marker = Path(str(bak) + ".migrated.json")
        marker.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  検証 OK: 元 {n_src:,} 行 = 月次 {summary['n_written']:,} 行 + invalid {invalid:,}")
        log(f"  完了マーカー: {marker.name}")
    else:
        log("  [NG] 行数不一致。.bak は残してある。月次ファイルを確認すること")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="計画表示のみ (書き込み無し)")
    ap.add_argument("--data-dir", default=None, help=f"既定 {DATA_DIR}")
    args = ap.parse_args()
    data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR
    print(f"=== race_odds_prerace 月次パーティション移行 {'(dry-run)' if args.dry_run else ''} ===")
    t0 = time.monotonic()
    s = partition(data_dir, dry_run=args.dry_run)
    print(f"=== {s['status']} ({time.monotonic() - t0:.1f}s) ===")
    if s["status"] == "no_space":
        return 2
    return 0 if s["status"] in ("done", "nothing_to_do", "dry_run") else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
