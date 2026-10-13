"""按需读的 HTTP 层测试：用一个本地 HTTP 服务器覆盖 206/200/416/异常分支。

这一层是最容易「看起来对、实际写错位置」的地方（Range 被忽略、Content-Range 对不上、
写盘偏移错位），离线把它测实比只依赖真实云盘更可靠。
"""

import http.server
import sys
import threading
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent  # 仓库根（mcloud_fuse/ 所在目录）
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcloud_fuse.client.wrapper import DirectClientWrapper, MCloudAPIException  # noqa: E402

PAYLOAD = bytes(range(256)) * 8  # 2048 字节


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    mode = "range"

    def log_message(self, *args):  # 静默，避免测试输出噪音
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 要求的命名
        request_range = self.headers.get("Range")

        if self.mode == "error":
            self.send_response(500)
            self.end_headers()
            return

        if self.mode == "short-full-body":
            # 回 200 但只给一半内容：调用方必须发现并报错，而不是当成完整文件
            body = PAYLOAD[: len(PAYLOAD) // 2]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.mode == "size-mismatch":
            # 206 分片内容正确，但总长与元数据不符（例如云端文件被改过）
            body = PAYLOAD[0:16]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-15/{len(PAYLOAD) + 1}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.mode == "wrong-range":
            # 服务端回了 206，但 Content-Range 起点与请求不符 → 必须报错，不能猜偏移
            body = PAYLOAD[100:201]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 100-200/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.mode == "ignore" or not request_range:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return

        start_text, end_text = request_range.split("=", 1)[1].split("-", 1)
        start, end = int(start_text), int(end_text)
        if start >= len(PAYLOAD):
            self.send_response(416)
            self.end_headers()
            return

        end = min(end, len(PAYLOAD) - 1)
        body = PAYLOAD[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def range_url():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/file"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_mode():
    _RangeHandler.mode = "range"
    yield
    _RangeHandler.mode = "range"


def test_range_get_writes_at_offset(range_url, tmp_path):
    target = tmp_path / "part.bin"
    target.write_bytes(b"\x00" * len(PAYLOAD))

    written = DirectClientWrapper._range_get(str(range_url), str(target), 1000, 16)

    assert written == 16
    assert target.read_bytes()[1000:1016] == PAYLOAD[1000:1016]
    assert target.read_bytes()[:1000] == b"\x00" * 1000, "不应破坏 offset 之前的内容"


def test_range_get_returns_zero_past_eof(range_url, tmp_path):
    target = tmp_path / "part.bin"
    target.write_bytes(b"\x00" * 8)
    assert DirectClientWrapper._range_get(str(range_url), str(target), 99999, 8) == 0


def test_range_get_detects_ignored_range(range_url, tmp_path):
    """服务端回 200（忽略 Range）时必须返回 None，并把整文件写在 0 位置。"""
    _RangeHandler.mode = "ignore"
    target = tmp_path / "part.bin"
    target.write_bytes(b"\x00" * len(PAYLOAD))

    result = DirectClientWrapper._range_get(
        str(range_url), str(target), 512, 16, len(PAYLOAD)
    )

    assert result is None
    assert target.read_bytes() == PAYLOAD


def test_range_get_rejects_short_full_body(range_url, tmp_path):
    """200 但内容不足元数据大小：必须报错，绝不能留下被截断的「完整文件」。"""
    _RangeHandler.mode = "short-full-body"
    target = tmp_path / "part.bin"

    with pytest.raises(MCloudAPIException, match="不完整"):
        DirectClientWrapper._range_get(
            str(range_url), str(target), 0, 16, len(PAYLOAD)
        )


def test_range_get_rejects_size_mismatch(range_url, tmp_path):
    """Content-Range 的总长与元数据不符（云端文件被改过）时应报错。"""
    _RangeHandler.mode = "size-mismatch"
    target = tmp_path / "part.bin"
    target.write_bytes(b"\x00" * len(PAYLOAD))

    with pytest.raises(MCloudAPIException, match="大小"):
        DirectClientWrapper._range_get(
            str(range_url), str(target), 0, 16, len(PAYLOAD)
        )


def test_range_get_rejects_mismatched_content_range(range_url, tmp_path):
    """206 但 Content-Range 起点对不上 → 报错，绝不写到猜测的偏移。"""
    _RangeHandler.mode = "wrong-range"
    target = tmp_path / "part.bin"
    target.write_bytes(b"\x00" * len(PAYLOAD))

    with pytest.raises(MCloudAPIException, match="Content-Range"):
        DirectClientWrapper._range_get(str(range_url), str(target), 512, 16)


def test_range_get_raises_on_server_error(range_url, tmp_path):
    _RangeHandler.mode = "error"
    target = tmp_path / "part.bin"
    target.write_bytes(b"\x00" * 16)
    with pytest.raises(MCloudAPIException):
        DirectClientWrapper._range_get(str(range_url), str(target), 0, 16)
