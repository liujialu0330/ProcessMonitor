"""utils.version：版本比较的唯一实现；core 原样导出，data 层不再依赖 core"""
import os
import subprocess
import sys
from pathlib import Path

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


# ========== 发布版本与预发布版本在各读取方的表现 ==========

def test_app_version_matches_changelog():
    changelog = Path(__file__).resolve().parent.parent / 'CHANGELOG.md'
    section = next(line[4:] for line in changelog.read_text(encoding='utf-8').splitlines()
                   if line.startswith('### '))
    if section.startswith('未发布'):
        assert version.is_prerelease(config.APP_VERSION)
    else:
        assert section.split()[0] == f'v{config.APP_VERSION}'


@pytest.mark.parametrize('current_version', ['2.0.0-beta.1', '2.0.0'])
def test_release_line_reads_and_writes_v5_db(current_version):
    """库的最低读取版本是 2.0.0：同一发布线的预发布版不能被判为只读"""
    from data.db import meta
    info = meta.MetaInfo(schema_version=5, min_reader_version=meta.MIN_READER_VERSION)
    assert meta.reader_blocked(info, current_version) is None


@pytest.mark.parametrize('current_version, ga_update, beta_update', [
    ('2.0.0-beta.1', True, True),
    ('2.0.0', False, False),
])
def test_update_check_respects_release_stage(monkeypatch, current_version,
                                            ga_update, beta_update):
    """beta 能升级到正式版；正式版不会提示安装同版本或更早的 beta。"""
    from core.update_checker import parse_release_info
    monkeypatch.setattr(config, 'APP_VERSION', current_version)

    def release(tag):
        return {'tag_name': tag, 'body': '', 'html_url': 'https://github.com/x/y',
                'assets': []}

    assert parse_release_info(release('v2.0.0'))['has_update'] is ga_update
    assert parse_release_info(release('v2.0.0-beta.2'))['has_update'] is beta_update
    assert parse_release_info(release('v2.0.0-beta.1'))['has_update'] is False
    assert parse_release_info(release('v1.4.1'))['has_update'] is False


def test_meta_records_current_version_as_writer(tmp_path):
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
