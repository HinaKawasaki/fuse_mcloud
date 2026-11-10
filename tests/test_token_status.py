"""token 状态行与后台续期（不访问网络、不依赖 systemd）。"""

from __future__ import annotations

import base64
import time

import pytest

from mcloud_fuse import token_status
from mcloud_fuse.client.web139 import Web139Token


def make_token(delta_ms: int = 30 * 24 * 3600 * 1000, account: str = "13800138000") -> Web139Token:
    expiry = int(time.time() * 1000) + delta_ms
    payload = f"v1|signature|opaque|{expiry}"
    raw = f"pc:{account}:{payload}".encode()
    return Web139Token.parse(base64.b64encode(raw).decode())


def token_with_expiry(expiry_ms: int, account: str = "13800138000") -> Web139Token:
    return Web139Token("pc", account, f"v1|signature|opaque|{expiry_ms}")


def test_summarize_without_token():
    assert token_status.summarize(None) == "token 未导入"


def test_summarize_masks_account_and_reports_days():
    text = token_status.summarize(make_token())
    assert "138****8000" in text
    assert "13800138000" not in text
    assert "剩余" in text
    assert "到期" in text


def test_summarize_expired():
    text = token_status.summarize(make_token(delta_ms=-3600 * 1000))
    assert "已过期" in text
    assert "138****8000" in text


def test_summarize_without_expiry():
    token = Web139Token("pc", "13800138000", "v1|signature|opaque")
    assert "未携带过期时间" in token_status.summarize(token)


def test_summarize_warns_when_less_than_a_week():
    text = token_status.summarize(make_token(delta_ms=2 * 24 * 3600 * 1000))
    assert "请尽快续期" in text


def test_status_text_includes_mountpoint_and_token():
    text = token_status.status_text("/home/me/mnt/cloud", make_token())
    assert text.startswith("mounted on /home/me/mnt/cloud | token 138****8000")
    assert "13800138000" not in text


def test_interval_env_parsing(monkeypatch):
    monkeypatch.delenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", raising=False)
    assert token_status.interval_seconds() == token_status.DEFAULT_INTERVAL_SEC

    monkeypatch.setenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", "0")
    assert token_status.interval_seconds() == 0

    monkeypatch.setenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", "6")
    assert token_status.interval_seconds() == token_status.MIN_INTERVAL_SEC

    monkeypatch.setenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", "7200")
    assert token_status.interval_seconds() == 7200

    monkeypatch.setenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", "不是数字")
    assert token_status.interval_seconds() == token_status.DEFAULT_INTERVAL_SEC


def test_auto_refresh_env(monkeypatch):
    monkeypatch.delenv("MCLOUD_TOKEN_AUTO_REFRESH", raising=False)
    assert token_status.auto_refresh_enabled() is True
    monkeypatch.setenv("MCLOUD_TOKEN_AUTO_REFRESH", "0")
    assert token_status.auto_refresh_enabled() is False
    monkeypatch.setenv("MCLOUD_TOKEN_AUTO_REFRESH", "off")
    assert token_status.auto_refresh_enabled() is False


def test_cadence_text_reflects_env(monkeypatch):
    monkeypatch.delenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("MCLOUD_TOKEN_AUTO_REFRESH", raising=False)
    assert "每 12 小时" in token_status.cadence_text()
    assert "自动续期" in token_status.cadence_text()

    monkeypatch.setenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", "0")
    assert "不做后台续期" in token_status.cadence_text()

    monkeypatch.setenv("MCLOUD_TOKEN_STATUS_INTERVAL_SEC", "21600")
    monkeypatch.setenv("MCLOUD_TOKEN_AUTO_REFRESH", "0")
    assert "只报告不续期" in token_status.cadence_text()


def test_report_refreshes_and_sends_status(monkeypatch):
    sent = []
    monkeypatch.setattr(token_status.sdnotify, "status", lambda text: sent.append(text) or True)
    token = make_token()
    calls = []

    reporter = token_status.TokenStatusReporter(
        "/home/me/mnt/cloud",
        token=lambda: token,
        refresh=lambda: calls.append(1),
        interval=0,
    )
    text = reporter.report()

    assert calls == [1]
    assert sent == [text]
    assert "138****8000" in text
    assert "13800138000" not in text


def test_report_survives_refresh_failure(monkeypatch):
    sent = []
    monkeypatch.setattr(token_status.sdnotify, "status", lambda text: sent.append(text) or True)

    def boom():
        raise RuntimeError("续期被拒绝")

    reporter = token_status.TokenStatusReporter(
        "/mnt/cloud",
        token=lambda: make_token(),
        refresh=boom,
        interval=0,
    )
    text = reporter.report()

    assert sent == [text]
    assert "token 138****8000" in text


def test_report_survives_token_provider_failure(monkeypatch):
    sent = []
    monkeypatch.setattr(token_status.sdnotify, "status", lambda text: sent.append(text) or True)

    def boom():
        raise OSError("token 文件读不了")

    reporter = token_status.TokenStatusReporter(
        "/mnt/cloud",
        token=boom,
        interval=0,
    )
    text = reporter.report(refresh=False)

    assert "token 不可用" in text
    assert sent == [text]


def test_start_with_zero_interval_only_reports_once(monkeypatch):
    sent = []
    monkeypatch.setattr(token_status.sdnotify, "status", lambda text: sent.append(text) or True)
    calls = []

    reporter = token_status.TokenStatusReporter(
        "/mnt/cloud",
        token=lambda: make_token(),
        refresh=lambda: calls.append(1),
        interval=0,
    )
    reporter.start()
    reporter.stop()

    assert len(sent) == 1
    assert calls == [], "interval=0 时不做后台续期"


def test_next_delay_shrinks_near_expiry():
    now = 1_000_000.0
    token = token_with_expiry(int(now * 1000) + 2 * 3600 * 1000)
    reporter = token_status.TokenStatusReporter(
        "/mnt/cloud",
        token=lambda: token,
        interval=12 * 3600,
        now=lambda: now,
    )
    assert reporter._next_delay() == pytest.approx(3600.0)


def test_next_delay_keeps_default_when_far_from_expiry():
    now = 1_000_000.0
    token = token_with_expiry(int(now * 1000) + 30 * 24 * 3600 * 1000)
    reporter = token_status.TokenStatusReporter(
        "/mnt/cloud",
        token=lambda: token,
        interval=12 * 3600,
        now=lambda: now,
    )
    assert reporter._next_delay() == 12 * 3600
