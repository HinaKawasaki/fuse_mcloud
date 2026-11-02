"""systemd 用户服务单元生成与状态判断（不调用 systemctl）。"""

from __future__ import annotations

from pathlib import Path

from mcloud_fuse import service
from mcloud_fuse.userconfig import UserConfig


def make_config(tmp_path: Path, **changes) -> UserConfig:
    values = {
        "mountpoint": str(tmp_path / "mnt"),
        "cache_dir": str(tmp_path / "cache"),
        "root": "/",
        "install_service": True,
    }
    values.update(changes)
    return UserConfig(**values)


def test_render_unit_contains_paths_and_module_entry(tmp_path: Path):
    config = make_config(tmp_path)
    config_file = tmp_path / "config.json"
    token_file = tmp_path / "web_token"
    unit = service.render_unit(
        config,
        python="/usr/bin/python3",
        config_file=config_file,
        token_file=token_file,
    )

    assert "ExecStart=/usr/bin/python3 -m mcloud_fuse mount" in unit
    assert str(config_file) in unit
    assert str(token_file) in unit
    assert "ExecStop=-" in unit
    assert str(config.mountpoint) in unit
    assert "Type=notify" in unit
    assert "NotifyAccess=main" in unit
    assert "TimeoutStartSec=60" in unit
    assert "TimeoutStopSec=60" in unit
    assert "WantedBy=default.target" in unit
    assert "Basic " not in unit, "单元里不应出现 token 明文"


def test_render_unit_quotes_paths_with_spaces(tmp_path: Path):
    config = make_config(tmp_path, mountpoint=str(tmp_path / "my cloud"))
    unit = service.render_unit(
        config,
        python="/usr/bin/python3",
        config_file=tmp_path / "config.json",
        token_file=tmp_path / "web_token",
    )
    assert f'"{config.mountpoint}"' in unit


def test_percent_is_escaped(tmp_path: Path):
    config = make_config(tmp_path, mountpoint=str(tmp_path / "100%"))
    unit = service.render_unit(
        config,
        python="/usr/bin/python3",
        config_file=tmp_path / "config.json",
        token_file=tmp_path / "web_token",
    )
    assert "100%%" in unit


def test_write_unit_permissions(tmp_path: Path):
    config = make_config(tmp_path)
    path = tmp_path / "systemd" / "mcloud-fuse.service"
    written = service.write_unit(config, path)
    assert written == path
    assert path.stat().st_mode & 0o777 == 0o644
    assert "mcloud-fuse" in path.read_text(encoding="utf-8")


def test_status_text_without_unit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "unit_dir", lambda: tmp_path / "systemd")
    assert service.status_text() == "未安装"


def test_install_fails_cleanly_without_systemctl(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "available", lambda: False)
    result = service.install(make_config(tmp_path))
    assert result.ok is False
    assert "systemctl" in result.detail


def test_stop_without_systemctl(monkeypatch):
    monkeypatch.setattr(service, "available", lambda: False)
    assert service.stop().ok is False


def test_status_text_installed_but_stopped(tmp_path: Path, monkeypatch):
    unit = tmp_path / "mcloud-fuse.service"
    unit.write_text("x", encoding="utf-8")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(service, "is_active", lambda: False)
    assert "未运行" in service.status_text()


def test_install_success(tmp_path: Path, monkeypatch):
    import types

    config = make_config(tmp_path)
    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(service, "write_unit", lambda cfg: tmp_path / "unit")
    monkeypatch.setattr(service, "is_active", lambda: True)
    monkeypatch.setattr(
        service,
        "_run",
        lambda args, timeout=30: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(service.os.path, "ismount", lambda path: True)
    result = service.install(config)
    assert result.ok is True
    assert "已安装并启动" in result.detail


def test_install_reports_daemon_reload_failure(tmp_path: Path, monkeypatch):
    import types

    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(service, "write_unit", lambda cfg: tmp_path / "unit")
    monkeypatch.setattr(
        service,
        "_run",
        lambda args, timeout=30: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    result = service.install(make_config(tmp_path))
    assert result.ok is False
    assert "daemon-reload" in result.detail


def test_uninstall_removes_unit(tmp_path: Path, monkeypatch):
    import types

    unit = tmp_path / "mcloud-fuse.service"
    unit.write_text("x", encoding="utf-8")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(
        service,
        "_run",
        lambda args, timeout=30: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    result = service.uninstall()
    assert result.ok is True
    assert not unit.exists()


def test_stop_success(monkeypatch):
    import types

    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(
        service,
        "_run",
        lambda args, timeout=30: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert service.stop().ok is True
