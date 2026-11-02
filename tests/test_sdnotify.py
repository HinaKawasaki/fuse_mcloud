"""sd_notify 的最小协议测试（不依赖真实 systemd）。"""

from __future__ import annotations

import socket
from pathlib import Path

from mcloud_fuse import sdnotify


def test_no_socket_is_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.ready() is False
    assert sdnotify.status("hi") is False
    assert sdnotify.extend_timeout(10) is False


def test_send_ready_status_and_extend(tmp_path: Path, monkeypatch):
    path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    try:
        assert sdnotify.ready() is True
        assert server.recv(4096) == b"READY=1"

        assert sdnotify.status("mounted") is True
        assert server.recv(4096) == b"STATUS=mounted"

        assert sdnotify.extend_timeout(12.5) is True
        assert server.recv(4096) == b"EXTEND_TIMEOUT_USEC=12500000"
    finally:
        server.close()


def test_status_strips_newlines(tmp_path: Path, monkeypatch):
    path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    try:
        sdnotify.status("a\nb")
        assert server.recv(4096) == b"STATUS=a b"
    finally:
        server.close()
