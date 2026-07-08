"""T6.6 — _stream_response_to_client：流式透传断开收尾 + 必 close + preview cap。"""
from local_proxy import SmartProxy


class FakeResp:
    def __init__(self, chunks, raise_at=None):
        self._chunks = chunks
        self._raise_at = raise_at
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for i, c in enumerate(self._chunks):
            if self._raise_at is not None and i == self._raise_at:
                raise OSError("client gone")
            yield c

    def close(self):
        self.closed = True


class FakeWfile:
    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data.extend(b)

    def flush(self):
        pass


def _proxy():
    p = SmartProxy.__new__(SmartProxy)  # 不走 __init__，避免起真实连接
    p.wfile = FakeWfile()
    return p


def test_all_chunks_written_and_closed():
    p = _proxy()
    r = FakeResp([b"a", b"bb", b"ccc"])
    p._stream_response_to_client(r, debug=False, full=False)
    assert bytes(p.wfile.data) == b"abbccc"
    assert r.closed is True  # finally 必 close


def test_client_disconnect_quiet_and_closed():
    p = _proxy()
    r = FakeResp([b"a", b"b", b"c"], raise_at=1)
    p._stream_response_to_client(r, debug=False, full=False)  # OSError 被吞，不外抛
    assert bytes(p.wfile.data) == b"a"  # 第 1 块前写出，之后中断
    assert r.closed is True


def test_full_body_written_regardless_of_preview_cap():
    p = _proxy()
    r = FakeResp([b"x" * 5000])  # 超过 cap(4096) 的单块
    p._stream_response_to_client(r, debug=True, full=False)
    assert len(p.wfile.data) == 5000  # 写客户端不受 preview cap 影响
    assert r.closed is True
