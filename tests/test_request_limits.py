"""T6.4 — Content-Length/413/400 守护（反复修补 ECONNRESET 的高危边界）+ drain 上限。"""
import io
import socket
import threading

import pytest

import common
import local_proxy
from local_proxy import SmartProxy, ThreadedHTTPServer


@pytest.fixture
def server(monkeypatch):
    # 调小体积上限便于测 413（do_POST 用的是 local_proxy 模块内的绑定）
    monkeypatch.setattr(local_proxy, "MAX_REQUEST_BODY_BYTES", 1024)
    srv = ThreadedHTTPServer(("127.0.0.1", 0), SmartProxy)  # 端口 0 → 系统分配
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


def _raw_post(port, headers, body=b""):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    req = ("POST /v1/messages HTTP/1.0\r\nHost: x\r\nConnection: close\r\n"
           + headers + "\r\n\r\n").encode() + body
    s.sendall(req)
    s.settimeout(5)
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def _status(data):
    return data.split(b"\r\n", 1)[0] if data else b""


def test_malformed_content_length_400(server):
    data = _raw_post(server, "Content-Length: abc\r\nContent-Type: application/json")
    assert b"400" in _status(data)  # 干净 400，非崩线程/RST


def test_negative_content_length_400(server):
    data = _raw_post(server, "Content-Length: -5\r\nContent-Type: application/json")
    assert b"400" in _status(data)


def test_oversized_413_not_reset(server):
    body = b'{"x":"' + b"A" * 4096 + b'"}'  # > 1024 上限
    data = _raw_post(server, f"Content-Length: {len(body)}\r\nContent-Type: application/json", body)
    # 收到了完整 413 响应 = 连接未被 RST（曾经的 ECONNRESET 回归点）
    assert b"413" in _status(data)


def test_drain_caps_at_limit():
    # _drain_request_body 至多读 DRAIN_CAP 字节，不被迫陪读声明的全部
    p = SmartProxy.__new__(SmartProxy)
    p.rfile = io.BytesIO(b"Z" * 10000)
    p.DRAIN_CAP = 100  # 覆盖类属性
    p._drain_request_body(10000)
    assert p.rfile.tell() == 100  # 只读了 cap，未读完 10000


def test_drain_reads_full_when_under_cap():
    p = SmartProxy.__new__(SmartProxy)
    p.rfile = io.BytesIO(b"Z" * 500)
    p.DRAIN_CAP = 64 * 1024 * 1024
    p._drain_request_body(500)
    assert p.rfile.tell() == 500  # 合法超限请求被读完 → 可得干净 413


def test_chunked_request_rejected_400(server):
    data = _raw_post(server, "Transfer-Encoding: chunked\r\nContent-Type: application/json")
    assert b"400" in _status(data)  # 不支持 chunked，显式 400 而非静默丢正文
