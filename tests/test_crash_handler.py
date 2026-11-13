"""
utils/crash_handler 的 crash.log 轮转：超过阈值时改名为 crash.log.1（覆盖旧的 .1）并重新
以追加方式打开；不超过阈值不动；轮转失败不影响钩子安装。

faulthandler.enable 换成记录调用的替身：真的启用会把 pytest 自己的 faulthandler 输出
改指向测试目录里的文件，事后 disable 又会关掉 pytest 的那份。
"""
import faulthandler
import os
import sys

import pytest

import utils.crash_handler as crash_handler


@pytest.fixture
def install(monkeypatch, tmp_path):
    """返回一个调用 install_excepthook(tmp_path) 的函数；结束时关闭 crash.log 并还原全局状态"""
    enabled = []
    monkeypatch.setattr(faulthandler, 'enable', lambda file=None, **kw: enabled.append(file))
    monkeypatch.setattr(sys, 'excepthook', sys.excepthook)
    monkeypatch.setattr(crash_handler, '_crash_log_file', None)
    monkeypatch.setattr(crash_handler, '_log_dir', None)
    monkeypatch.setattr(crash_handler, 'CRASH_LOG_MAX_BYTES', 100)

    def _install():
        crash_handler.install_excepthook(str(tmp_path))
        return enabled

    yield _install
    if crash_handler._crash_log_file is not None:
        crash_handler._crash_log_file.close()


def _write(path, size):
    path.write_bytes(b"x" * size)


def test_default_threshold_is_one_megabyte():
    assert crash_handler.CRASH_LOG_MAX_BYTES == 1024 * 1024


def test_oversized_log_is_rotated_and_new_empty_file_opened(install, tmp_path):
    crash_log = tmp_path / "crash.log"
    _write(crash_log, 101)

    enabled = install()

    assert (tmp_path / "crash.log.1").read_bytes() == b"x" * 101
    assert crash_log.exists() and crash_log.stat().st_size == 0
    assert len(enabled) == 1
    assert os.path.normcase(enabled[0].name) == os.path.normcase(str(crash_log))
    assert enabled[0].mode == 'a'
    assert sys.excepthook is crash_handler._global_excepthook


def test_log_within_threshold_is_left_alone(install, tmp_path):
    crash_log = tmp_path / "crash.log"
    _write(crash_log, 100)    # 恰好等于阈值：不轮转

    install()

    assert not (tmp_path / "crash.log.1").exists()
    assert crash_log.read_bytes() == b"x" * 100


def test_missing_log_is_created_without_rotation(install, tmp_path):
    install()

    assert (tmp_path / "crash.log").exists()
    assert not (tmp_path / "crash.log.1").exists()


def test_existing_backup_is_overwritten(install, tmp_path):
    crash_log, backup = tmp_path / "crash.log", tmp_path / "crash.log.1"
    _write(crash_log, 150)
    backup.write_bytes(b"old backup")

    install()

    assert backup.read_bytes() == b"x" * 150
    assert crash_log.stat().st_size == 0


def test_rotation_failure_still_installs(install, tmp_path, monkeypatch):
    """改名失败（如另一个实例正占用 crash.log）只记日志，仍以追加方式打开原文件并安装钩子"""
    crash_log = tmp_path / "crash.log"
    _write(crash_log, 200)

    def _deny(src, dst):
        raise PermissionError("文件被占用")

    monkeypatch.setattr(crash_handler.os, 'replace', _deny)

    enabled = install()

    assert not (tmp_path / "crash.log.1").exists()
    assert crash_log.read_bytes() == b"x" * 200
    assert len(enabled) == 1
    assert sys.excepthook is crash_handler._global_excepthook
