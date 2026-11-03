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
