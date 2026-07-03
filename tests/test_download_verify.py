"""
UpdateDownloader 下载完整性校验用例
mock urllib.request.urlopen，避免真实网络请求；直接调用 run()（同线程执行，QThread
子类的 run() 是普通 Python 方法，不经 start() 也能同步跑完，便于断言）
"""
import os

import pytest

import core.update_checker as update_checker_module
from core.update_checker import UpdateDownloader


class _FakeResponse:
    """模拟 urllib 的响应对象：支持 with 语句、headers.get、分块 read()"""

    def __init__(self, content: bytes, content_length):
        self._chunks = [content[i:i + 1024] for i in range(0, len(content), 1024)] or [b'']
        self._idx = 0
        self.headers = {'Content-Length': str(content_length)} if content_length is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _size):
        if self._idx >= len(self._chunks):
            return b''
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk


@pytest.fixture
def downloader(tmp_path, monkeypatch, qapp):
    """把下载落地目录重定向到 tmp_path，避免污染系统临时目录"""
    monkeypatch.setattr(update_checker_module.tempfile, 'gettempdir', lambda: str(tmp_path))
    return lambda url, filename, asset_size=0: UpdateDownloader(url, filename, asset_size)


def test_download_length_mismatch_deletes_file_and_emits_error(downloader, tmp_path, monkeypatch):
    """Content-Length 与实际下载字节数不符：删除已下载文件、emit 错误信号，不 emit 完成信号"""
    content = b'x' * 100
    monkeypatch.setattr(
        update_checker_module.urllib.request, 'urlopen',
        lambda *a, **k: _FakeResponse(content, content_length=200)  # 声称200，实际只有100
    )

    d = downloader("https://example.com/pkg.exe", "pkg.exe")
    errors = []
    finished = []
    d.error_occurred.connect(lambda msg: errors.append(msg))
    d.download_finished.connect(lambda path: finished.append(path))

    d.run()

    assert len(errors) == 1
    assert "不完整" in errors[0]
    assert finished == []
    assert not os.path.exists(str(tmp_path / "pkg.exe"))


def test_download_length_match_emits_finished(downloader, tmp_path, monkeypatch):
    """Content-Length 与实际下载字节数一致：正常 emit download_finished，文件保留"""
    content = b'y' * 100
    monkeypatch.setattr(
        update_checker_module.urllib.request, 'urlopen',
        lambda *a, **k: _FakeResponse(content, content_length=100)
    )

    d = downloader("https://example.com/pkg.exe", "pkg.exe")
    errors = []
    finished = []
    d.error_occurred.connect(lambda msg: errors.append(msg))
    d.download_finished.connect(lambda path: finished.append(path))

    d.run()

    assert errors == []
    assert len(finished) == 1
    assert os.path.exists(finished[0])
    with open(finished[0], 'rb') as f:
        assert f.read() == content


def test_download_missing_content_length_falls_back_to_asset_size_mismatch(downloader, tmp_path, monkeypatch):
    """响应头缺 Content-Length（total=0）时退回 asset_size 做校验：不匹配则判失败"""
    content = b'z' * 50
    monkeypatch.setattr(
        update_checker_module.urllib.request, 'urlopen',
        lambda *a, **k: _FakeResponse(content, content_length=None)
    )

    d = downloader("https://example.com/pkg.exe", "pkg.exe", asset_size=999)
    errors = []
    finished = []
    d.error_occurred.connect(lambda msg: errors.append(msg))
    d.download_finished.connect(lambda path: finished.append(path))

    d.run()

    assert len(errors) == 1
    assert finished == []


def test_download_no_length_and_no_asset_size_skips_verification(downloader, tmp_path, monkeypatch):
    """Content-Length 与 asset_size 都拿不到（total<=0 且 asset_size<=0）：无法校验，视为通过"""
    content = b'w' * 30
    monkeypatch.setattr(
        update_checker_module.urllib.request, 'urlopen',
        lambda *a, **k: _FakeResponse(content, content_length=None)
    )

    d = downloader("https://example.com/pkg.exe", "pkg.exe", asset_size=0)
    errors = []
    finished = []
    d.error_occurred.connect(lambda msg: errors.append(msg))
    d.download_finished.connect(lambda path: finished.append(path))

    d.run()

    assert errors == []
    assert len(finished) == 1
