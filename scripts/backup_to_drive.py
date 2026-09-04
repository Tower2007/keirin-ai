"""keirin-ai データを ZIP 化して Google Drive へ転送

PowerShell の Compress-Archive は 2 GB を超えると失敗するため Python で実装。
zipfile に allowZip64=True を渡して大容量に対応。

使い方:
  python scripts/backup_to_drive.py
  python scripts/backup_to_drive.py --data-dir D:\\keirin-ai-data ^
                                    --drive-dir "G:\\マイドライブ\\keirin-ai-backups"
  python scripts/backup_to_drive.py --no-upload  # ZIP は作るが Drive へ送らない
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path


def format_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} TB"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=r"D:\keirin-ai-data",
                    help="ZIP 化するデータディレクトリ")
    ap.add_argument("--drive-dir",
                    default=r"G:\マイドライブ\keirin-ai-backups",
                    help="ZIP の転送先 (Drive フォルダ)")
    ap.add_argument("--no-upload", action="store_true",
                    help="ZIP は作るが Drive へ送らない (検証用)")
    ap.add_argument("--keep", type=int, default=3,
                    help="Drive に残す世代数 (古い ZIP は削除)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        sys.exit(f"data-dir not found: {data_dir}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"keirin-ai-data_{timestamp}.zip"
    tmp_zip = Path.home() / "AppData" / "Local" / "Temp" / zip_name
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)

    # 対象ファイル一覧 (.lock は除外)。
    # race_odds_prerace.csv.bak-<日時> (月次パーティション移行前の旧単一ファイル、800MB) は
    # 中身が race_odds_prerace_YYYY-MM.csv と同一なので ZIP には入れない (2026-09-04)。
    # cache/ (dedup キャッシュ) はサブディレクトリなので元々対象外。
    files = [f for f in data_dir.iterdir()
             if f.is_file() and f.suffix != ".lock"
             and ".csv.bak-" not in f.name]
    total_size = sum(f.stat().st_size for f in files)

    print(f"1/3 ZIP 作成中: {tmp_zip}")
    print(f"   source: {data_dir}")
    print(f"   files: {len(files)} / total: {format_size(total_size)}")

    t0 = time.time()
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED,
                         allowZip64=True, compresslevel=6) as zf:
        for i, f in enumerate(files, 1):
            t_file = time.time()
            zf.write(f, arcname=f.name)
            sz = f.stat().st_size
            print(f"   [{i}/{len(files)}] {f.name:30s} "
                  f"{format_size(sz):>10s}  "
                  f"({time.time() - t_file:.1f}s)")

    elapsed = time.time() - t0
    zip_size = tmp_zip.stat().st_size
    ratio = zip_size / total_size * 100
    print(f"\n   完成: {format_size(zip_size)} "
          f"(圧縮率 {ratio:.1f}%, {elapsed:.1f}秒)")

    # Drive 転送
    if args.no_upload:
        print(f"\n2/3 --no-upload 指定のため Drive へのコピーはスキップ")
        print(f"ZIP は {tmp_zip} にあります")
        return

    drive_dir = Path(args.drive_dir)
    drive_dir.mkdir(parents=True, exist_ok=True)
    dest = drive_dir / zip_name

    print(f"\n2/3 Drive へコピー: {dest}")
    t0 = time.time()
    shutil.copy2(tmp_zip, dest)
    print(f"   完了 ({time.time() - t0:.1f}秒)")

    # 古いバックアップ削除
    print(f"\n3/3 古いバックアップ整理 (最新 {args.keep} 個だけ残す)")
    backups = sorted(
        drive_dir.glob("keirin-ai-data_*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[args.keep:]:
        old.unlink()
        print(f"   削除: {old.name}")

    # 一時 ZIP 削除
    tmp_zip.unlink()

    print(f"\n=== バックアップ完了 ===")
    print(f"PC2 での復元手順:")
    print(f"  1. ブラウザで Google Drive を開き {zip_name} をダウンロード")
    print(f"     (Drive Stream で扱うと事故るのでブラウザ DL 推奨)")
    print(f"  2. D:\\keirin-ai-data\\ に展開")
    print(f"  3. .env で DATA_DIR=D:\\keirin-ai-data を設定")


if __name__ == "__main__":
    main()
