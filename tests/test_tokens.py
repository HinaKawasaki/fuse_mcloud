"""token 解析、脱敏与 0600 落盘。"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest

from mcloud_fuse import tokens
from mcloud_fuse.client.web139 import Web139AuthError, Web139Token


def basic_token(account: str = "13800138000", delta_ms: int = 30 * 24 * 3600 * 1000) -> str:
    expiry = int(time.time() * 1000) + delta_ms
    payload = f"v1|signature|opaque|{expiry}"
    raw = f"pc:{account}:{payload}".encode()
    return base64.b64encode(raw).decode()


def test_extract_from_authorization_header():
    basic = basic_token()
    assert tokens.extract_basic(f"authorization: Basic {basic}") == basic


def test_extract_from_curl():
    basic = basic_token()
    raw = (
        "curl 'https://yun.139.com/hcy/file/list' \\\n"
        "  -H 'accept: application/json' \\\n"
        f"  -H 'authorization: Basic {basic}' \\\n"
        "  --data-raw '{}'"
    )
    assert tokens.extract_basic(raw) == basic


def test_extract_from_bare_value():
    basic = basic_token()
    assert tokens.extract_basic(f"  {basic}\n") == basic
    assert tokens.extract_basic(f"Basic {basic}") == basic


def test_cookie_is_rejected_with_actionable_message():
    with pytest.raises(ValueError, match="Cookie"):
        tokens.extract_basic("skey=abc; auth_token=def; ud_id=ghi")


def test_empty_input_is_rejected():
    with pytest.raises(ValueError, match="输入为空"):
        tokens.extract_basic("   ")


def test_mask_account():
    assert tokens.mask_account("13800138000") == "138****8000"
    assert tokens.mask_account("ab") == "ab"


def test_describe_hides_account_and_reports_expiry():
    token = Web139Token.parse(basic_token())
    text = tokens.describe(token)
    assert "138****8000" in text
    assert "13800138000" not in text
    assert "到期" in text


def test_save_and_load_private(tmp_path: Path):
    path = tmp_path / "web_token"
    result = tokens.save_basic(basic_token(), path=path, force=True)
    assert result.wrote is True
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = tokens.load_saved(path)
    assert loaded is not None
    assert loaded.account == "13800138000"


def test_expired_token_is_rejected(tmp_path: Path):
    path = tmp_path / "web_token"
    with pytest.raises(Web139AuthError, match="过期"):
        tokens.save_basic(basic_token(delta_ms=-1000), path=path, force=True)
    assert not path.exists()


def test_later_existing_token_is_kept(tmp_path: Path):
    path = tmp_path / "web_token"
    later = basic_token(delta_ms=60 * 24 * 3600 * 1000)
    earlier = basic_token(delta_ms=10 * 24 * 3600 * 1000)
    tokens.save_basic(later, path=path, force=True)
    original = path.read_text(encoding="utf-8")

    result = tokens.save_basic(earlier, path=path)
    assert result.wrote is False
    assert path.read_text(encoding="utf-8") == original


def test_force_replaces_existing(tmp_path: Path):
    path = tmp_path / "web_token"
    tokens.save_basic(basic_token(delta_ms=60 * 24 * 3600 * 1000), path=path, force=True)
    result = tokens.save_basic(
        basic_token(delta_ms=10 * 24 * 3600 * 1000), path=path, force=True
    )
    assert result.wrote is True
    loaded = tokens.load_saved(path)
    assert loaded is not None


def test_load_missing_returns_none(tmp_path: Path):
    assert tokens.load_saved(tmp_path / "missing") is None


def test_token_summary_never_raises(tmp_path: Path):
    assert "未导入" in tokens.token_summary(tmp_path / "missing")
