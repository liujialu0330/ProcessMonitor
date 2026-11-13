"""utils.version：版本比较的唯一实现；core 原样导出，data 层不再依赖 core"""
import os
import subprocess
import sys

import pytest

import config
from utils import version


def test_core_reexports_the_same_function():
    from core import update_checker
    assert update_checker.compare_version is version.compare_version


def test_meta_uses_utils_version_not_core():
    """数据层的只读判定不导入 core（data 不依赖 core）"""
    import data.db.meta as meta
    with open(meta.__file__, encoding='utf-8') as f:
        source = f.read()
    assert 'core.update_checker' not in source
    assert 'from utils.version import' in source


def test_importing_meta_does_not_load_core():
    """在干净的解释器里导入 data.db.meta 并调用 reader_blocked，core 包都不会被加载"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys\n"
        "from data.db import meta\n"
        "info = meta.MetaInfo(min_reader_version='9.0.0')\n"
        "assert meta.reader_blocked(info, '2.0.0')\n"
        "print(sorted(m for m in sys.modules if m == 'core' or m.startswith('core.')))\n"
    )
    out = subprocess.run([sys.executable, '-c', code], cwd=root, capture_output=True,
                         text=True, encoding='utf-8', timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == '[]'


@pytest.mark.parametrize('a, b, expected', [
    ('2.0.0-beta.1', '2.0.0', -1),
    ('2.0.0-beta.1', '1.4.1', 1),
    ('2.0.0-beta.2', '2.0.0-beta.10', -1),
    ('2.0.0-rc.1', '2.0.0-beta.9', 1),
    ('v2.0.0', '2.0.0+build.7', 0),
    ('', None, 0),
])
def test_compare_version(a, b, expected):
    assert version.compare_version(a, b) == expected


@pytest.mark.parametrize('text, release, pre', [
    ('2.0.0-beta.1', '2.0.0', True),
    ('v2.0.0-rc.2+abc', '2.0.0', True),
    ('2.0.0', '2.0.0', False),
    ('1.4.1+build', '1.4.1', False),
])
def test_release_part_and_prerelease(text, release, pre):
    assert version.release_part(text) == release
    assert version.is_prerelease(text) is pre


# ========== 当前版本号（2.0.0-beta.1）在各读取方的表现 ==========

def test_app_version_is_beta_prerelease():
    assert config.APP_VERSION == "2.0.0-beta.1"
    assert version.is_prerelease(config.APP_VERSION)


def test_beta_reads_and_writes_v5_db():
    """库的最低读取版本是 2.0.0：同一发布线的预发布版不能被判为只读"""
    from data.db import meta
    info = meta.MetaInfo(schema_version=5, min_reader_version=meta.MIN_READER_VERSION)
    assert meta.reader_blocked(info, config.APP_VERSION) is None


def test_update_check_with_beta_current_version():
    """beta 用户能收到正式版与更新的 beta，不会把旧正式版当成更新"""
    from core.update_checker import parse_release_info

    def release(tag):
        return {'tag_name': tag, 'body': '', 'html_url': 'https://github.com/x/y',
                'assets': []}

    assert parse_release_info(release('v2.0.0'))['has_update'] is True
    assert parse_release_info(release('v2.0.0-beta.2'))['has_update'] is True
    assert parse_release_info(release('v2.0.0-beta.1'))['has_update'] is False
    assert parse_release_info(release('v1.4.1'))['has_update'] is False


def test_meta_records_beta_as_writer(tmp_path):
    from data.database import Database
    from data.db import meta
    path = str(tmp_path / 'monitor.db')
    Database(path).close()
    info = meta.read_meta_file(path)
    assert info.created_by_version == config.APP_VERSION
    assert info.min_reader_version == meta.MIN_READER_VERSION


def test_setup_iss_requires_version_from_build_script():
    """安装脚本不再有写死的兜底版本号：没传 /DMyAppVersion 就编译失败"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'build', 'setup.iss'), encoding='utf-8') as f:
        text = f.read()
    block = text[text.index('#ifndef MyAppVersion'):text.index('#endif')]
    assert '#error' in block
    assert '#define MyAppVersion' not in block
