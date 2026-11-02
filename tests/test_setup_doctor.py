"""setup 校验/执行与 doctor 的离线小测试（不访问网络、不依赖 token）。"""

from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path

import pytest

from mcloud_fuse import doctor
from mcloud_fuse import setup as setup_mod
from mcloud_fuse.client.web139 import Web139AuthError
from mcloud_fuse.userconfig import UserConfig


def make_config(tmp_path: Path, **changes) -> UserConfig:
    values = {
        "mountpoint": str(tmp_path / "mnt"),
        "cache_dir": str(tmp_path / "cache"),
        "root": "/",
        "install_service": False,
    }
    values.update(changes)
    return UserConfig(**values)


def test_validate_mountpoint_creates_and_accepts(tmp_path: Path):
    target = tmp_path / "cloud"
    assert setup_mod.validate_mountpoint(str(target)) is None
    assert target.is_dir()


def test_validate_mountpoint_rejects_root_and_relative():
    assert setup_mod.validate_mountpoint("/") is not None
    assert setup_mod.validate_mountpoint("mnt/cloud") is not None
    assert setup_mod.validate_mountpoint("") is not None


def test_validate_mountpoint_rejects_existing_mount(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(setup_mod.os.path, "ismount", lambda path: True)
    assert "挂载点" in (setup_mod.validate_mountpoint(str(tmp_path)) or "")


def test_validate_cache_rejects_recursive_paths(tmp_path: Path):
    mount = tmp_path / "mnt"
    inside = mount / "cache"
    assert "互相包含" in (setup_mod.validate_cache(str(inside), str(mount)) or "")
    assert "互相包含" in (setup_mod.validate_cache(str(tmp_path), str(mount / "sub")) or "")


def test_validate_cache_creates_dir(tmp_path: Path):
    target = tmp_path / "cache"
    assert setup_mod.validate_cache(str(target), str(tmp_path / "mnt")) is None
    assert target.is_dir()


def test_validate_root():
    assert setup_mod.validate_root("/") is None
    assert setup_mod.validate_root("abc123") is None
    assert setup_mod.validate_root("a/b") is not None


def test_apply_args_overrides_config(tmp_path: Path):
    base = make_config(tmp_path)
    args = setup_mod._parse_args(
        ["--mountpoint", str(tmp_path / "other"), "--root", "abc", "--no-service"]
    )
    changed = setup_mod._apply_args(base, args)
    assert changed.mountpoint == str(tmp_path / "other")
    assert changed.root == "abc"
    assert changed.install_service is False


def test_execute_offline_writes_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_CONFIG_FILE", str(tmp_path / "config.json"))
    plan = setup_mod.SetupPlan(
        config=make_config(tmp_path),
        verify_api=False,
    )
    seen = []
    results = setup_mod.execute(plan, lambda name, index, total: seen.append(name))
    assert [result.name for result in results] == ["写入配置"]
    assert results[0].ok is True
    assert seen == ["写入配置"]
    assert (tmp_path / "config.json").exists()


def test_run_line_without_token_returns_error(tmp_path: Path):
    plan = setup_mod.SetupPlan(config=make_config(tmp_path), verify_api=False)
    args = setup_mod._parse_args(["--yes", "--no-service", "--no-check-api"])
    assert setup_mod.run_line(plan, args) == 1


def test_doctor_cache_ok(tmp_path: Path):
    check = doctor._check_cache(tmp_path)
    assert check.status == doctor.OK


def test_doctor_mountpoint_states(tmp_path: Path):
    missing = tmp_path / "missing"
    assert doctor._check_mountpoint(missing).status == doctor.WARN
    assert doctor._check_mountpoint(tmp_path).status == doctor.WARN


def test_doctor_python_ok():
    assert doctor._check_python().status == doctor.OK


def basic_token() -> str:
    expiry = int(time.time() * 1000) + 30 * 24 * 3600 * 1000
    return base64.b64encode(f"pc:13800138000:v1|sig|opaque|{expiry}".encode()).decode()


def test_parse_token_rejects_expired():
    expiry = int(time.time() * 1000) - 1000
    raw = base64.b64encode(f"pc:13800138000:v1|sig|opaque|{expiry}".encode()).decode()
    with pytest.raises(Web139AuthError, match="过期"):
        setup_mod.parse_token(raw)


def test_run_line_imports_token_and_writes_config(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "config.json"
    token_file = tmp_path / "web_token"
    monkeypatch.setenv("MCLOUD_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(sys, "stdin", io.StringIO(basic_token()))

    plan = setup_mod.SetupPlan(config=make_config(tmp_path), verify_api=False)
    args = setup_mod._parse_args(
        ["--yes", "--no-service", "--no-check-api", "--token-stdin"]
    )
    assert setup_mod.run_line(plan, args) == 0
    assert config_file.exists()
    assert token_file.exists()
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_load_existing_token_returns_none_when_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(tmp_path / "missing"))
    assert setup_mod.load_existing_token() is None
