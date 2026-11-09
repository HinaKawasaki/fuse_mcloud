"""用户配置的读写、权限与环境变量优先级。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mcloud_fuse import userconfig
from mcloud_fuse.userconfig import ConfigError, UserConfig


def make_config(tmp_path: Path, **changes) -> UserConfig:
    values = {
        "mountpoint": str(tmp_path / "mnt" / "cloud"),
        "cache_dir": str(tmp_path / "cache"),
        "root": "/",
        "rm_mode": "delete",
        "conflict": "copy",
        "install_service": True,
    }
    values.update(changes)
    return UserConfig(**values)


def test_roundtrip_and_private_permissions(tmp_path: Path):
    path = tmp_path / "config" / "config.json"
    config = make_config(tmp_path, rm_mode="trash", install_service=False)

    userconfig.save(config, path)
    loaded = userconfig.load(path)

    assert loaded == config
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["rm_mode"] == "trash"


def test_missing_file_returns_none(tmp_path: Path):
    assert userconfig.load(tmp_path / "nope.json") is None


def test_invalid_json_raises(tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON"):
        userconfig.load(path)


def test_invalid_enum_raises():
    with pytest.raises(ConfigError, match="rm_mode"):
        UserConfig(mountpoint="/tmp/x", cache_dir="/tmp/c", rm_mode="shred")
    with pytest.raises(ConfigError, match="conflict"):
        UserConfig(mountpoint="/tmp/x", cache_dir="/tmp/c", conflict="overwrite")


def test_root_mountpoint_is_rejected():
    with pytest.raises(ConfigError, match="挂载点"):
        UserConfig(mountpoint="/", cache_dir="/tmp/c")


def test_paths_are_expanded_and_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = UserConfig(mountpoint="~/mnt/cloud", cache_dir="~/cache", root="  /dir  ")
    assert config.mountpoint == str(tmp_path / "mnt" / "cloud")
    assert config.cache_dir == str(tmp_path / "cache")
    assert config.root == "/dir"


def test_defaults_honor_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_MOUNTPOINT", str(tmp_path / "env-mnt"))
    monkeypatch.setenv("MCLOUD_CACHE_DIR", str(tmp_path / "env-cache"))
    config = UserConfig()
    assert config.mountpoint == str(tmp_path / "env-mnt")
    assert config.cache_dir == str(tmp_path / "env-cache")


def test_existing_environment_wins(tmp_path: Path):
    config = make_config(tmp_path, cache_dir=str(tmp_path / "from-file"))
    environ = {"MCLOUD_CACHE_DIR": "/explicit", "MCLOUD_RM_MODE": "trash"}
    config.apply_env(environ)
    assert environ["MCLOUD_CACHE_DIR"] == "/explicit"
    assert environ["MCLOUD_RM_MODE"] == "trash"
    assert environ["MCLOUD_CONFLICT"] == "copy"


def test_non_bool_install_service_is_rejected(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mountpoint": str(tmp_path / "mnt"), "install_service": "false"}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="install_service"):
        userconfig.load(path)


def test_unknown_fields_are_ignored(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "mountpoint": str(tmp_path / "mnt"),
                "cache_dir": str(tmp_path / "cache"),
                "future_field": "ignored",
            }
        ),
        encoding="utf-8",
    )
    loaded = userconfig.load(path)
    assert loaded is not None
    assert loaded.mountpoint == str(tmp_path / "mnt")


def test_config_paths_follow_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("MCLOUD_CONFIG_FILE", raising=False)
    assert userconfig.config_path() == tmp_path / "cfg" / "config.json"
    assert userconfig.default_token_path() == tmp_path / "cfg" / "web_token"

    monkeypatch.setenv("MCLOUD_CONFIG_FILE", str(tmp_path / "one.json"))
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(tmp_path / "token"))
    assert userconfig.config_path() == tmp_path / "one.json"
    assert userconfig.default_token_path() == tmp_path / "token"


def test_with_changes_revalidates(tmp_path: Path):
    config = make_config(tmp_path)
    changed = config.with_changes(root="/abc", rm_mode="trash")
    assert changed.root == "/abc"
    assert changed.rm_mode == "trash"
    with pytest.raises(ConfigError):
        config.with_changes(conflict="nope")


def test_apply_saved_env_ignores_broken_config(tmp_path: Path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("MCLOUD_CONFIG_FILE", str(path))
    monkeypatch.delenv("MCLOUD_CACHE_DIR", raising=False)
    assert userconfig.apply_saved_env() is None
    assert "MCLOUD_CACHE_DIR" not in os.environ
