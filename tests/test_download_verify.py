"""
UpdateDownloader 下载校验用例（长度校验、SHA-256 校验、URL 白名单、缺失摘要拒绝）
mock core.update_checker._open_download，不访问真实网络；直接调用 run()（同线程执行，
QThread 子类的 run() 是普通 Python 方法，不经 start() 也能同步跑完，便于断言）
"""
import hashlib
import os

import pytest

import core.update_checker as update_checker_module
from core.update_checker import (
    MSG_CANNOT_VERIFY, UpdateDownloader, is_allowed_download_url, parse_sha256_digest,
)

GOOD_URL = "https://github.com/liujialu0330/ProcessMonitor/releases/download/v1.5.0/pkg.exe"


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


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@pytest.fixture
def work_dirs(tmp_path, monkeypatch):
    """把 mkdtemp 重定向到 tmp_path 下并记录每次创建的目录，便于断言清理情况"""
    created = []
    real_mkdtemp = update_checker_module.tempfile.mkdtemp

    def _mkdtemp(*args, **kwargs):
        kwargs['dir'] = str(tmp_path)
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(update_checker_module.tempfile, 'mkdtemp', _mkdtemp)
    return created


@pytest.fixture
def run_download(qapp, monkeypatch, work_dirs):
    """返回 run(content, content_length, digest, url, asset_size) -> (downloader, errors, finished)"""
    def _run(content, content_length, digest, url=GOOD_URL, asset_size=0):
        monkeypatch.setattr(
            update_checker_module, '_open_download',
            lambda *a, **k: _FakeResponse(content, content_length))
        d = UpdateDownloader(url, "pkg.exe", asset_size, digest=digest)
        errors, finished = [], []
        d.error_occurred.connect(lambda msg: errors.append(msg))
        d.download_finished.connect(lambda path: finished.append(path))
        d.run()
        return d, errors, finished
    return _run


# ========== 长度校验 ==========

def test_download_length_mismatch_deletes_file_and_emits_error(run_download, work_dirs):
    content = b'x' * 100
    _, errors, finished = run_download(content, 200, _digest(content))  # 声称200，实际只有100

    assert len(errors) == 1 and "不完整" in errors[0]
    assert finished == []
    assert not os.path.exists(work_dirs[0]), "失败后应删除整个临时目录"


def test_download_missing_content_length_falls_back_to_asset_size_mismatch(run_download):
    content = b'z' * 50
    _, errors, finished = run_download(content, None, _digest(content), asset_size=999)

    assert len(errors) == 1 and "不完整" in errors[0]
    assert finished == []


# ========== SHA-256 校验 ==========

def test_download_success_with_matching_digest(run_download, work_dirs):
    content = b'y' * 5000
    _, errors, finished = run_download(content, 5000, _digest(content))

    assert errors == []
    assert len(finished) == 1
    assert os.path.dirname(finished[0]) == work_dirs[0]   # 落在 mkdtemp 的唯一目录内
    assert os.path.basename(finished[0]) == "pkg.exe"
    with open(finished[0], 'rb') as f:
        assert f.read() == content


def test_download_without_length_info_still_verified_by_digest(run_download):
    """Content-Length 与 asset_size 都缺失时跳过长度检查，由 SHA-256 兜底"""
    content = b'w' * 30
    _, errors, finished = run_download(content, None, _digest(content), asset_size=0)

    assert errors == [] and len(finished) == 1


def test_download_digest_mismatch_deletes_file(run_download, work_dirs):
    content = b'a' * 100
    _, errors, finished = run_download(content, 100, _digest(b'tampered'))

    assert len(errors) == 1 and "校验失败" in errors[0]
    assert finished == []
    assert not os.path.exists(work_dirs[0])


def test_digest_comparison_is_case_insensitive(run_download):
    content = b'b' * 64
    digest = "sha256:" + hashlib.sha256(content).hexdigest().upper()
    _, errors, finished = run_download(content, 64, digest)

    assert errors == [] and len(finished) == 1


@pytest.mark.parametrize("digest", [None, "", "sha256:", "sha256:abc", "md5:" + "a" * 32,
                                    "sha256:" + "g" * 64])
def test_missing_or_invalid_digest_refuses_without_download(qapp, monkeypatch, work_dirs, digest):
    """缺少/无效摘要：不下载、不建临时目录，经 error_occurred 提示手动下载"""
    opened = []
    monkeypatch.setattr(update_checker_module, '_open_download',
                        lambda *a, **k: opened.append(1))
    d = UpdateDownloader(GOOD_URL, "pkg.exe", 100, digest=digest)
    errors, finished = [], []
    d.error_occurred.connect(errors.append)
    d.download_finished.connect(finished.append)

    d.run()

    assert errors == [MSG_CANNOT_VERIFY]
    assert "手动下载" in MSG_CANNOT_VERIFY
    assert finished == [] and opened == [] and work_dirs == []


def test_cancel_deletes_temp_dir(qapp, monkeypatch, work_dirs):
    content = b'c' * 5000
    monkeypatch.setattr(update_checker_module, '_open_download',
                        lambda *a, **k: _FakeResponse(content, 5000))
    d = UpdateDownloader(GOOD_URL, "pkg.exe", digest=_digest(content))
    errors, finished = [], []
    d.error_occurred.connect(errors.append)
    d.download_finished.connect(finished.append)
    d.cancel()

    d.run()

    assert errors == [] and finished == []
    assert not os.path.exists(work_dirs[0])


def test_unique_work_dir_per_download(run_download, work_dirs):
    content = b'd' * 10
    run_download(content, 10, _digest(content))
    run_download(content, 10, _digest(content))
    assert len(set(work_dirs)) == 2


# ========== URL 白名单 ==========

@pytest.mark.parametrize("url", [
    "https://github.com/a/b/releases/download/v1/x.exe",
    "https://objects.githubusercontent.com/github-production/x",
    "https://release-assets.githubusercontent.com/github-production-release-asset/x",
    "https://sub.objects.githubusercontent.com/x",
    "https://GitHub.com/x",
    "https://github.com:443/x",
])
def test_allowed_download_urls(url):
    assert is_allowed_download_url(url) is True


@pytest.mark.parametrize("url", [
    "http://github.com/x",                          # 非 https
    "ftp://github.com/x",
    "https://evilgithub.com/x",                     # 后缀拼接
    "https://github.com.evil.com/x",                # 前缀伪装
    "https://notobjects.githubusercontent.com.evil/x",
    "https://githubusercontent.com/x",              # 不在白名单的父域
    "https://raw.githubusercontent.com/x",          # 同属 githubusercontent 但未列入
    "https://github.com@evil.com/x",                # userinfo 欺骗
    "https://evil.com/github.com",
    "https://github.com:8443/x",                    # 非默认端口
    "https://example.com/pkg.exe",
    "", None, "not a url", "https:///x",
])
def test_rejected_download_urls(url):
    assert is_allowed_download_url(url) is False


def test_downloader_rejects_untrusted_url_before_network(qapp, monkeypatch, work_dirs):
    opened = []
    monkeypatch.setattr(update_checker_module, '_open_download',
                        lambda *a, **k: opened.append(1))
    d = UpdateDownloader("https://example.com/pkg.exe", "pkg.exe", digest=_digest(b'x'))
    errors = []
    d.error_occurred.connect(errors.append)

    d.run()

    assert len(errors) == 1 and "不受信任" in errors[0]
    assert opened == [] and work_dirs == []


def test_redirect_handler_blocks_untrusted_target():
    import urllib.error
    import urllib.request
    handler = update_checker_module._SafeRedirectHandler()
    req = urllib.request.Request(GOOD_URL)
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.com/x.exe")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://objects.githubusercontent.com/x")
    ok = handler.redirect_request(
        req, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/x")
    assert ok is not None


# ========== digest 解析 ==========

def test_parse_sha256_digest():
    hex64 = "ab" * 32
    assert parse_sha256_digest("sha256:" + hex64) == hex64
    assert parse_sha256_digest("  sha256:" + hex64.upper() + " ") == hex64
    for bad in (None, "", "sha256:", "sha1:" + "a" * 40, hex64, "sha256:" + "a" * 63):
        assert parse_sha256_digest(bad) is None


# ========== 基类信号（BackgroundTask） ==========

def test_downloader_generic_signals_mirror_named_ones(qapp, monkeypatch, work_dirs):
    """成功时 result 与 download_finished 同值、progress 与 download_progress 同步；
    失败时 failed 与 error_occurred 同文案"""
    content = b'g' * 3000
    monkeypatch.setattr(update_checker_module, '_open_download',
                        lambda *a, **k: _FakeResponse(content, 3000))
    d = UpdateDownloader(GOOD_URL, "pkg.exe", digest=_digest(content))
    finished, result, named_progress, progress = [], [], [], []
    d.download_finished.connect(finished.append)
    d.result.connect(result.append)
    d.download_progress.connect(named_progress.append)
    d.progress.connect(progress.append)

    d.run()

    assert len(finished) == 1 and result == finished
    assert progress == named_progress and progress[-1] == 100

    d2 = UpdateDownloader(GOOD_URL, "pkg.exe", digest=None)
    errors, failed = [], []
    d2.error_occurred.connect(errors.append)
    d2.failed.connect(failed.append)
    d2.run()
    assert errors == failed == [MSG_CANNOT_VERIFY]


def test_cancel_during_download_emits_nothing(qapp, monkeypatch, work_dirs):
    """下载途中取消：删除残留目录，既不发完成也不发失败"""
    content = b'h' * 10240
    monkeypatch.setattr(update_checker_module, '_open_download',
                        lambda *a, **k: _FakeResponse(content, 10240))
    d = UpdateDownloader(GOOD_URL, "pkg.exe", digest=_digest(content))
    signals = []
    d.download_progress.connect(lambda p: d.cancel())   # 第一块写完即取消
    d.error_occurred.connect(signals.append)
    d.failed.connect(signals.append)
    d.download_finished.connect(signals.append)
    d.result.connect(signals.append)

    d.run()

    assert signals == []
    assert not os.path.exists(work_dirs[0])
