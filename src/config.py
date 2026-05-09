"""プロジェクト全体の設定値。

DATA_DIR: CSV/ログ保存先。.env の DATA_DIR で上書き可能。
別 PC との共有や Google Drive (例: G:\\マイドライブ\\keirin-ai\\data) を使う場合に活用。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent


def _path_env(key: str, default: Path) -> Path:
    """環境変数 key が定義されていればその Path を返す。無ければ default。"""
    val = os.getenv(key)
    if val:
        return Path(val).expanduser()
    return default


DATA_DIR: Path = _path_env("DATA_DIR", ROOT_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
