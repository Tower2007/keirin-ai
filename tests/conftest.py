"""テスト中は実メール送信を禁止する (2026-11-12)。

通知関数を足したコードのテストで差し替え忘れがあっても本物のメールが出ないよう、
送信の実体 (gmail_notify.send_email と smtplib の SMTP/SMTP_SSL) を autouse で
「呼ばれたら例外」にする。送信を検証したいテストは monkeypatch で自前のモックに
差し替えること (fixture より後に適用されるので上書きできる)。
"""

import smtplib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _forbidden(*args, **kwargs):
    raise RuntimeError("テスト中にメール送信が呼ばれた (モックに差し替えること)")


@pytest.fixture(autouse=True)
def _block_real_mail(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _forbidden)
    try:
        import gmail_notify
        monkeypatch.setattr(gmail_notify, "send_email", _forbidden)
    except Exception:
        pass
    yield
