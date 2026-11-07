"""
build/gen_version_info.py 用例：版本号 → Windows 文件版本 4 元组的映射，
以及生成的 version_info.txt 保留原始版本号文本
"""
import functools
import importlib.util
import os

import pytest

from core.update_checker import compare_version

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    """build\\ 不是包，按文件路径加载打包脚本"""
    path = os.path.join(PROJECT_ROOT, 'build', 'gen_version_info.py')
    spec = importlib.util.spec_from_file_location('gen_version_info_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_module()


@pytest.mark.parametrize("version, expected", [
    ("2.0.0", (2, 0, 0, 100)),
    ("1.4.1", (1, 4, 1, 100)),
    ("2.0.0-beta.1", (2, 0, 0, 1)),
    ("2.0.0-beta.2", (2, 0, 0, 2)),
    ("2.0.0-beta.49", (2, 0, 0, 49)),
    ("2.0.0-rc.1", (2, 0, 0, 51)),
    ("2.0.0-rc.0", (2, 0, 0, 50)),
    ("2.0.0-rc.49", (2, 0, 0, 99)),
    ("2.0.0-alpha", (2, 0, 0, 0)),       # 其他预发布标签按 0
    ("2.0.0-beta", (2, 0, 0, 0)),        # 不带序号的 beta 也属"其他"
    ("2.0.0-dev.3", (2, 0, 0, 0)),
    ("2.0.0-RC.1", (2, 0, 0, 0)),        # 只认小写 beta/rc
    ("2.0.0+build.7", (2, 0, 0, 100)),   # 构建元数据忽略
    ("2.0.0-rc.2+build.7", (2, 0, 0, 52)),
    # 不符合 X.Y.Z[-预发布] 的输入沿用旧规则：前 4 段、非数字按 0、不足补 0
    ("1.2", (1, 2, 0, 0)),
    ("1", (1, 0, 0, 0)),
    ("1.2.3.4", (1, 2, 3, 4)),
    ("1.2.3.4.5", (1, 2, 3, 4)),
    ("1.x.3", (1, 0, 3, 0)),
    ("v2.0.0", (0, 0, 0, 0)),
])
def test_to_version_tuple_mapping(version, expected):
    assert gen._to_version_tuple(version) == expected


def test_file_version_order_matches_compare_version():
    """同一 X.Y.Z 下文件版本号的顺序必须与 compare_version 的 SemVer 顺序一致"""
    chain = ["1.4.1", "2.0.0-beta.1", "2.0.0-beta.2", "2.0.0-beta.10",
             "2.0.0-rc.1", "2.0.0-rc.2", "2.0.0", "2.0.1-beta.1", "2.0.1"]
    assert sorted(chain, key=functools.cmp_to_key(compare_version)) == chain
    tuples = [gen._to_version_tuple(v) for v in chain]
    assert tuples == sorted(tuples)
    assert len(set(tuples)) == len(tuples)


@pytest.mark.parametrize("version", ["2.0.0-beta.50", "2.0.0-rc.50", "2.0.0-rc.999"])
def test_prerelease_number_too_large_is_rejected(version):
    """N ≥ 50 会撞到下一档（beta.50 == rc.0，rc.50 == 正式版），必须拒绝而不是静默生成"""
    with pytest.raises(ValueError, match="预发布序号"):
        gen._to_version_tuple(version)


@pytest.mark.parametrize("version", ["65536.0.0", "1.65536.0", "1.0.70000", "1.2.3.65536"])
def test_field_over_65535_is_rejected(version):
    """Windows 版本资源每段 16 位，越界必须报错"""
    with pytest.raises(ValueError, match="65535"):
        gen._to_version_tuple(version)


def test_field_max_65535_is_accepted():
    assert gen._to_version_tuple("65535.65535.65535") == (65535, 65535, 65535, 100)


def test_generate_keeps_original_version_text(tmp_path):
    """FileVersion/ProductVersion 字符串保留原始版本号（含 -beta.1），4 元组按映射规则"""
    out = tmp_path / 'version_info.txt'
    assert gen.generate(str(out), version="2.0.0-beta.1") == str(out)
    text = out.read_text(encoding='utf-8')
    assert "filevers=(2, 0, 0, 1)" in text
    assert "prodvers=(2, 0, 0, 1)" in text
    assert "StringStruct(u'FileVersion', u'2.0.0-beta.1')" in text
    assert "StringStruct(u'ProductVersion', u'2.0.0-beta.1')" in text


def test_generate_defaults_to_app_version(tmp_path):
    import config
    out = tmp_path / 'version_info.txt'
    gen.generate(str(out))
    text = out.read_text(encoding='utf-8')
    assert f"filevers={gen._to_version_tuple(config.APP_VERSION)}" in text
    assert f"StringStruct(u'FileVersion', u'{config.APP_VERSION}')" in text
