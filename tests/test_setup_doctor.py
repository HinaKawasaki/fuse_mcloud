"""setup 校验/执行与 doctor 的离线小测试（不访问网络、不依赖 token）。"""

from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path

import pytest

from mcloud_fuse import doctor, tokens, userconfig
from mcloud_fuse import setup as setup_mod
from mcloud_fuse.client.web139 import Web139AuthError, Web139Token
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
    assert setup_mod.validate_root("/Photos") is None
    assert setup_mod.validate_root("/Photos/2026/") is None
    assert setup_mod.validate_root("a/b") is not None
    assert setup_mod.validate_root("/Photos//2026") is not None
    assert setup_mod.validate_root("/Photos/../etc") is not None


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


def test_refresh_existing_token_saves_new_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(tmp_path / "web_token"))
    old = Web139Token.parse(basic_token())
    new_expiry = int(time.time() * 1000) + 60 * 24 * 3600 * 1000
    new = Web139Token("pc", "13800138000", f"v1|sig|opaque|{new_expiry}")

    class FakeClient:
        def __init__(self, auth):
            self.auth = auth

        def refresh_token(self, *, force: bool = False):
            assert force is True, "向导里的「立即续期」必须强制请求"
            return new

    monkeypatch.setattr(setup_mod, "Web139Client", FakeClient)

    refreshed = setup_mod.refresh_existing_token(old)

    assert refreshed.expires_at_ms == new_expiry
    saved = tokens.load_saved(tmp_path / "web_token")
    assert saved is not None
    assert saved.expires_at_ms == new_expiry
    assert (tmp_path / "web_token").stat().st_mode & 0o777 == 0o600


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


def test_run_line_refresh_token_updates_existing_token(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "config.json"
    token_file = tmp_path / "web_token"
    monkeypatch.setenv("MCLOUD_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("MCLOUD_WEB_TOKEN_FILE", str(token_file))

    old = Web139Token.parse(basic_token())
    new_expiry = int(time.time() * 1000) + 60 * 24 * 3600 * 1000
    new = Web139Token("pc", "13800138000", f"v1|sig|opaque|{new_expiry}")
    monkeypatch.setattr(setup_mod, "refresh_existing_token", lambda token: new)

    plan = setup_mod.SetupPlan(
        config=make_config(tmp_path),
        existing_token=old,
        verify_api=False,
    )
    args = setup_mod._parse_args(
        ["--yes", "--no-service", "--no-check-api", "--refresh-token"]
    )

    assert setup_mod.run_line(plan, args) == 0
    saved = tokens.load_saved(token_file)
    assert saved is not None
    assert saved.expires_at_ms == new_expiry


class FakeScreen:
    """只实现 step_token 用到的几个页面方法。"""

    def __init__(self, choice: int, pasted: str = ""):
        self.choice = choice
        self.pasted = pasted
        self.messages = []
        self.menu_kwargs = {}

    def menu(self, **kwargs):
        self.menu_kwargs = kwargs
        return self.choice

    def progress(self, **kwargs):
        return None

    def message(self, **kwargs):
        self.messages.append(kwargs)

    def read_pasted(self, instructions):
        return self.pasted


def test_wizard_auto_refresh_success(tmp_path: Path, monkeypatch):
    old = Web139Token.parse(basic_token())
    new_expiry = int(time.time() * 1000) + 60 * 24 * 3600 * 1000
    new = Web139Token("pc", "13800138000", f"v1|sig|opaque|{new_expiry}")
    plan = setup_mod.SetupPlan(
        config=make_config(tmp_path),
        existing_token=old,
        verify_api=False,
    )
    monkeypatch.setattr(setup_mod, "refresh_existing_token", lambda token: new)
    screen = FakeScreen(choice=1)  # 有效 token：使用 / 自动续期 / 重新粘贴

    setup_mod._Wizard(screen, plan, force_token=False).step_token()

    assert plan.existing_token == new
    assert plan.new_token_basic == new.to_basic()
    assert screen.messages[-1]["title"] == "续期成功"


def test_wizard_auto_refresh_failure_falls_back_to_paste(tmp_path: Path, monkeypatch):
    old = Web139Token.parse(basic_token())
    plan = setup_mod.SetupPlan(
        config=make_config(tmp_path),
        existing_token=old,
        verify_api=False,
    )

    def boom(token):
        raise Web139AuthError("token 续期被拒绝：需要设备指纹")

    monkeypatch.setattr(setup_mod, "refresh_existing_token", boom)
    screen = FakeScreen(choice=1, pasted="")  # 续期失败后粘贴空内容 = 沿用旧 token

    setup_mod._Wizard(screen, plan, force_token=False).step_token()

    assert screen.messages[0]["title"] == "自动续期失败"
    assert "设备指纹" in screen.messages[0]["lines"][0]
    assert plan.new_token_basic is None


def test_wizard_expired_token_defaults_to_auto_refresh(tmp_path: Path, monkeypatch):
    expired = int(time.time() * 1000) - 1000
    old = Web139Token("pc", "13800138000", f"v1|sig|opaque|{expired}")
    new_expiry = int(time.time() * 1000) + 60 * 24 * 3600 * 1000
    new = Web139Token("pc", "13800138000", f"v1|sig|opaque|{new_expiry}")
    plan = setup_mod.SetupPlan(
        config=make_config(tmp_path),
        existing_token=old,
        verify_api=False,
    )
    monkeypatch.setattr(setup_mod, "refresh_existing_token", lambda token: new)
    screen = FakeScreen(choice=0)  # 已过期：尝试自动续期 / 重新粘贴

    setup_mod._Wizard(screen, plan, force_token=False).step_token()

    assert [item[0] for item in screen.menu_kwargs["items"]] == [
        "尝试自动续期（无需粘贴）",
        "重新粘贴令牌",
    ]
    assert screen.menu_kwargs["selected"] == 0
    assert plan.existing_token == new


# ------------------------------------------------------- TUI 第 2/3 步（离线）


class _ScriptedScreen:
    """按顺序回放菜单选择；message 只记录不阻塞。"""

    def __init__(self, choices=()):
        self.choices = list(choices)
        self.messages = []

    def menu(self, **kwargs):
        assert self.choices, "界面等待了一个没有排队的菜单选择"
        return self.choices.pop(0)

    def message(self, **kwargs):
        self.messages.append(kwargs)

    def progress(self, **kwargs):
        return None


def make_wizard(tmp_path: Path, screen, *, mountpoint=None, allow_other=False):
    config = make_config(
        tmp_path,
        mountpoint=str(mountpoint or (tmp_path / "mnt")),
        allow_other=allow_other,
    )
    return setup_mod._Wizard(screen, setup_mod.SetupPlan(config=config), False)


def _conf(tmp_path: Path, text: str = "user_allow_other\n") -> Path:
    path = tmp_path / "fuse.conf"
    path.write_text(text, encoding="utf-8")
    return path


def test_wizard_share_off_is_default(tmp_path: Path):
    screen = _ScriptedScreen([0])
    wizard = make_wizard(tmp_path, screen)
    wizard.step_share()
    assert wizard.plan.config.allow_other is False
    assert screen.choices == []


def test_wizard_share_on_when_prerequisites_ready(tmp_path, monkeypatch, world_dir):
    conf = _conf(tmp_path)
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(conf))
    screen = _ScriptedScreen([1])
    wizard = make_wizard(tmp_path, screen, mountpoint=world_dir / "cloud")
    wizard.step_share()
    assert wizard.plan.config.allow_other is True
    assert screen.choices == []


def test_wizard_share_can_back_out_without_fuse_conf(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(tmp_path / "missing.conf"))
    screen = _ScriptedScreen([1, 2])  # 选共享（允许 1），再选「算了，仅自己」
    wizard = make_wizard(tmp_path, screen)
    wizard.step_share()
    assert wizard.plan.config.allow_other is False


def test_wizard_share_warns_when_home_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(_conf(tmp_path)))
    # tmp_path 是 0700：父目录不可穿越，向导应给出提示页并允许改回「仅自己」。
    screen = _ScriptedScreen([1, 1])
    wizard = make_wizard(tmp_path, screen, mountpoint=tmp_path / "cloud")
    wizard.step_share()
    assert wizard.plan.config.allow_other is False
    assert screen.choices == []


def test_wizard_apply_mountpoint_creates_directory(tmp_path: Path):
    screen = _ScriptedScreen([])
    wizard = make_wizard(tmp_path, screen)
    target = tmp_path / "a" / "b" / "cloud"
    assert wizard._apply_mountpoint(str(target)) is True
    assert target.is_dir()
    assert wizard.plan.config.mountpoint == str(target)


def test_wizard_apply_mountpoint_reports_blocked_path(tmp_path: Path):
    screen = _ScriptedScreen([])
    wizard = make_wizard(tmp_path, screen)
    assert wizard._apply_mountpoint("relative/path") is False
    assert screen.messages and "绝对路径" in screen.messages[-1]["lines"][0]


def test_wizard_review_shows_share_state(tmp_path: Path):
    from mcloud_fuse import tui

    class _ReviewScreen(_ScriptedScreen):
        def __init__(self):
            super().__init__([1])  # review 的菜单直接选「重新配置」
            self.lines = []

        def message(self, **kwargs):
            self.lines.extend(kwargs.get("lines", []))

        def menu(self, **kwargs):
            if self.choices:
                return self.choices.pop(0)
            raise tui.Back()

    screen = _ReviewScreen()
    wizard = make_wizard(tmp_path, screen, allow_other=True)
    action = wizard.review()
    assert action == "edit"
    assert any("共享访问" in line and "allow_other" in line for line in screen.lines)


def test_ensure_share_line_mode_degrades_without_fuse_conf(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(tmp_path / "missing.conf"))
    assert setup_mod.ensure_share(str(tmp_path / "mnt"), True, assume_yes=False) is False
    captured = capsys.readouterr()
    assert "user_allow_other" in captured.err


def test_ensure_share_line_mode_keeps_off_when_disabled(tmp_path):
    assert setup_mod.ensure_share(str(tmp_path / "mnt"), False, assume_yes=True) is False


def test_doctor_share_check_off(tmp_path: Path):
    checks = doctor._check_share(False, tmp_path)
    assert len(checks) == 1 and checks[0].status == doctor.OK


def test_doctor_share_check_flags_missing_conf(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(tmp_path / "missing.conf"))
    checks = doctor._check_share(True, tmp_path)
    assert any(check.status == doctor.FAIL for check in checks)
    assert any("user_allow_other" in check.detail for check in checks)


def test_doctor_share_check_ok_with_conf(tmp_path, monkeypatch, world_dir):
    monkeypatch.setenv("MCLOUD_FUSE_CONF", str(_conf(tmp_path)))
    mount = world_dir / "cloud"
    mount.mkdir()
    checks = doctor._check_share(True, mount)
    assert [check.status for check in checks if check.status == doctor.FAIL] == []


def test_run_line_saves_config_without_network(tmp_path, monkeypatch, capsys):
    """行模式 --yes：不提问、不联网，也要把配置写对（CI/脚本的入口）。"""
    import argparse

    future = int(time.time() * 1000) + 30 * 86400_000
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("MCLOUD_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("MCLOUD_DELETE_FAILURES_FILE", str(tmp_path / "failures.json"))

    cfg = UserConfig(
        mountpoint=str(tmp_path / "mnt"),
        cache_dir=str(tmp_path / "cache"),
        install_service=False,
    )
    plan = setup_mod.SetupPlan(
        config=cfg,
        existing_token=Web139Token("pc", "13800138000", f"v1|sig|opaque|{future}"),
        verify_api=False,
    )
    args = argparse.Namespace(
        refresh_token=False,
        force_token=False,
        token_stdin=False,
        yes=True,
        no_service=True,
    )

    assert setup_mod.run_line(plan, args) == 0
    saved = userconfig.load(config_path)
    assert saved is not None
    assert saved.mountpoint == str(tmp_path / "mnt")
    assert saved.cache_dir == str(tmp_path / "cache")
    assert "写入配置" in capsys.readouterr().out
