"""
分层约束：app 组装层位于 ui / core / data / utils 之上，各层不得反向导入 app 包
（根目录的 app_config、config 不属于 app 包）；业务代码不得绕过组合根自建 Database()。
"""
import os
import re

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYERS = ("ui", "core", "data", "utils")

# 匹配 `from app import ...` / `from app.xxx import ...` / `import app` / `import app.xxx`，
# 不匹配 app_config
_APP_IMPORT = re.compile(r"^\s*(from\s+app(\.|\s)|import\s+app(\.|\s|$))", re.MULTILINE)


def _sources(*layers):
    for layer in layers:
        for root, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, layer)):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8") as f:
                        yield os.path.relpath(path, PROJECT_ROOT), f.read()


@pytest.mark.parametrize("layer", LAYERS)
def test_layer_does_not_import_app_package(layer):
    offenders = [path for path, text in _sources(layer) if _APP_IMPORT.search(text)]
    assert offenders == []


def test_ui_and_core_never_construct_default_database():
    offenders = [path for path, text in _sources("ui", "core") if "Database()" in text]
    assert offenders == []


def test_pattern_sanity():
    assert _APP_IMPORT.search("from app.paths import AppPaths")
    assert _APP_IMPORT.search("import app.shutdown as m")
    assert _APP_IMPORT.search("from app import bootstrap")
    assert not _APP_IMPORT.search("from app_config import cfg")
    assert not _APP_IMPORT.search("import app_config")
