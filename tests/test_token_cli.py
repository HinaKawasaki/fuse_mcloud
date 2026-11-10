"""``mcloud-fuse token`` 的离线测试（续期请求全部由假客户端接管）。"""

from __future__ import annotations

import base64
import time

from mcloud_fuse import token_cli, tokens
from mcloud_fuse.client.web139 import Web139AuthError, Web139Token


def basic_token(delta_ms: int = 30 * 24 * 3600 * 1000) -> str:
    expiry = int(time.time() * 1000) + delta_ms
    payload = f"v1|sig|opaque|{expiry}"
    return base64.b64encode(f"pc:13800138000:{payload}".encode()).decode()


def save_token(tmp_path, monkeypatch, delta_ms: int = 30 * 24 * 3600 * 1000):
    path = tmp_path / "web_token"
    monkeypatch.delenv("MCLOUD_WEB_TOKEN", raising=False)
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(path))
    tokens.save_basic(basic_token(delta_ms), path=path, force=True)
    return path


def test_status_masks_account(tmp_path, monkeypatch, capsys):
    save_token(tmp_path, monkeypatch)

    assert token_cli.main(["status"]) == 0

    output = capsys.readouterr().out
    assert "138****8000" in output
    assert "13800138000" not in output
    assert "到期" in output


def test_status_without_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MCLOUD_WEB_TOKEN", raising=False)
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(tmp_path / "missing"))
    assert token_cli.main(["status"]) == 0
    assert "未导入" in capsys.readouterr().out


def test_refresh_saves_new_token(tmp_path, monkeypatch, capsys):
    save_token(tmp_path, monkeypatch)
    new_expiry = int(time.time() * 1000) + 60 * 24 * 3600 * 1000
    new = Web139Token("pc", "13800138000", f"v1|sig|opaque|{new_expiry}")

    class FakeClient:
        def __init__(self, auth):
            self.auth = auth

        def refresh_token(self, *, force: bool = False):
            assert force is True
            return new

    monkeypatch.setattr(token_cli, "Web139Client", FakeClient)

    assert token_cli.main(["refresh"]) == 0
    output = capsys.readouterr().out
    assert "138****8000" in output
    assert "13800138000" not in output

    saved = tokens.load_saved(tmp_path / "web_token")
    assert saved is not None
    assert saved.expires_at_ms == new_expiry
    assert (tmp_path / "web_token").stat().st_mode & 0o777 == 0o600


def test_refresh_failure_returns_one_and_actionable_hint(tmp_path, monkeypatch, capsys):
    save_token(tmp_path, monkeypatch)

    class FakeClient:
        def __init__(self, auth):
            pass

        def refresh_token(self, *, force: bool = False):
            raise Web139AuthError("token 续期被拒绝：expired")

    monkeypatch.setattr(token_cli, "Web139Client", FakeClient)

    assert token_cli.main(["refresh"]) == 1
    captured = capsys.readouterr()
    assert "自动续期失败" in captured.err
    assert "mcloud-fuse setup" in captured.err


def test_refresh_without_token_returns_one(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MCLOUD_WEB_TOKEN", raising=False)
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(tmp_path / "missing"))
    assert token_cli.main(["refresh"]) == 1
    assert "未导入" in capsys.readouterr().err


def test_unknown_command_returns_usage(capsys):
    assert token_cli.main(["what"]) == 2
    assert "status|refresh" in capsys.readouterr().err
