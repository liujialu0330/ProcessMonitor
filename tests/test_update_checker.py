"""
core/update_checker.py 纯函数用例
覆盖 compare_version 全边界与 parse_release_info（mock JSON dict）
"""
import config
from core.update_checker import compare_version, parse_release_info


# ========== compare_version ==========

def test_compare_version_equal():
    assert compare_version("1.0.6", "1.0.6") == 0


def test_compare_version_v_prefix():
    """容忍 v/V 前缀"""
    assert compare_version("v1.2.3", "1.2.3") == 0
    assert compare_version("V1.2.3", "v1.2.3") == 0
    assert compare_version("v1.1.0", "1.0.6") == 1


def test_compare_version_greater_and_less():
    assert compare_version("1.0.7", "1.0.6") == 1
    assert compare_version("0.9.9", "1.0.6") == -1


def test_compare_version_segment_count_mismatch():
    """缺段按 0 处理"""
    assert compare_version("1.0", "1.0.0") == 0
    assert compare_version("1.0.1", "1.0") == 1
    assert compare_version("1", "1.0.0.1") == -1
    assert compare_version("2", "1.9.9.9") == 1


def test_compare_version_non_numeric_segment_treated_as_zero():
    """非数字段（预发布尾段等）按 0 处理——已知限制，非真实 semver 预发布优先级"""
    # "7-beta" 无法转 int，按 0 处理，实际参与比较的是 [1, 0, 0]
    assert compare_version("1.0.7-beta", "1.0.6") == -1
    assert compare_version("1.0.7-beta", "1.0.0") == 0


# ========== parse_release_info ==========

def _base_release(tag="v1.2.0", assets=None, body="更新说明"):
    return {
        "tag_name": tag,
        "body": body,
        "html_url": "https://github.com/liujialu0330/ProcessMonitor/releases/tag/v1.2.0",
        "assets": assets or [],
    }


def test_parse_release_info_with_setup_asset(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "1.1.0")
    data = _base_release(assets=[
        {"name": "Windows_v1.2.0_Setup.exe",
         "browser_download_url": "https://example.com/Windows_v1.2.0_Setup.exe",
         "size": 12345678},
        {"name": "source.zip", "browser_download_url": "https://example.com/source.zip", "size": 999},
    ])

    info = parse_release_info(data)

    assert info["version"] == "1.2.0"
    assert info["has_update"] is True
    assert info["notes"] == "更新说明"
    assert info["html_url"] == data["html_url"]
    assert info["asset_url"] == "https://example.com/Windows_v1.2.0_Setup.exe"
    assert info["asset_name"] == "Windows_v1.2.0_Setup.exe"
    assert info["asset_size"] == 12345678


def test_parse_release_info_without_setup_asset_degrades(monkeypatch):
    """无 Setup 资产时降级：asset_url/asset_name 为 None，asset_size 为 0，但 version/has_update 仍正常解析"""
    monkeypatch.setattr(config, "APP_VERSION", "1.1.0")
    data = _base_release(assets=[
        {"name": "source.zip", "browser_download_url": "https://example.com/source.zip", "size": 999},
    ])

    info = parse_release_info(data)

    assert info["version"] == "1.2.0"
    assert info["has_update"] is True
    assert info["asset_url"] is None
    assert info["asset_name"] is None
    assert info["asset_size"] == 0


def test_parse_release_info_no_assets_key():
    """assets 字段缺失时不应抛异常"""
    data = {"tag_name": "v1.0.0", "body": "", "html_url": ""}
    info = parse_release_info(data)
    assert info["asset_url"] is None
    assert info["asset_size"] == 0


def test_parse_release_info_no_update_when_same_version(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "1.2.0")
    data = _base_release(tag="v1.2.0")
    info = parse_release_info(data)
    assert info["has_update"] is False


def test_parse_release_info_older_remote_version(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "2.0.0")
    data = _base_release(tag="v1.2.0")
    info = parse_release_info(data)
    assert info["has_update"] is False


def test_parse_release_info_strips_v_prefix_and_empty_body():
    data = {"tag_name": "V1.3.0", "assets": []}
    info = parse_release_info(data)
    assert info["version"] == "1.3.0"
    assert info["notes"] == ""
