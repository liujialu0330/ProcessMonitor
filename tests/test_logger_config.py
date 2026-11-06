"""日志滚动配置：5MB x 5"""
import logging
import logging.handlers
import os

import pytest

import utils.logger as logger_module


def test_rotation_constants():
    assert logger_module.LOG_MAX_BYTES == 5 * 1024 * 1024
    assert logger_module.LOG_BACKUP_COUNT == 5


def test_setup_logging_installs_rotating_handler_with_new_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, 'get_log_dir', lambda: str(tmp_path / "logs"))
    monkeypatch.setattr(logger_module, '_configured', False)
    root = logging.getLogger()
    before = list(root.handlers)
    old_level = root.level
    try:
        log_dir = logger_module.setup_logging()
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        handler = added[0]
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 5
        assert os.path.dirname(handler.baseFilename) == os.path.abspath(log_dir)
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(old_level)


def _remove_added_handlers(root, before, old_level):
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(old_level)


def test_setup_logging_follows_data_dir_env(tmp_path, monkeypatch):
    """设置 PROCESS_MONITOR_DATA_DIR 后，未传目录时日志也写进该数据目录之下"""
    data_dir = tmp_path / "portable_data"
    monkeypatch.setenv("PROCESS_MONITOR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(logger_module, '_configured', False)
    root = logging.getLogger()
    before = list(root.handlers)
    old_level = root.level
    try:
        log_dir = logger_module.setup_logging()
        assert os.path.commonpath([os.path.abspath(log_dir), str(data_dir)]) == str(data_dir)
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert os.path.dirname(added[0].baseFilename) == os.path.abspath(log_dir)
        assert os.path.isdir(log_dir)
    finally:
        _remove_added_handlers(root, before, old_level)


def test_setup_logging_uses_explicit_dir(tmp_path, monkeypatch):
    """入口显式传入的目录优先"""
    monkeypatch.setattr(logger_module, '_configured', False)
    root = logging.getLogger()
    before = list(root.handlers)
    old_level = root.level
    target = str(tmp_path / "explicit_logs")
    try:
        assert logger_module.setup_logging(target) == target
        added = [h for h in root.handlers if h not in before]
        assert os.path.dirname(added[0].baseFilename) == os.path.abspath(target)
    finally:
        _remove_added_handlers(root, before, old_level)


def test_default_log_dir_is_sibling_of_data_dir(tmp_path, monkeypatch):
    """未设置环境变量时保持既有布局：logs 与 data 同级"""
    monkeypatch.delenv("PROCESS_MONITOR_DATA_DIR", raising=False)
    fake_data = str(tmp_path / "app" / "data")
    monkeypatch.setattr(logger_module.config, 'get_data_dir', lambda: fake_data)
    assert logger_module.get_log_dir() == str(tmp_path / "app" / "logs")
