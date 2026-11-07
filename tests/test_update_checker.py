"""
core/update_checker.py 纯函数用例
覆盖 compare_version 全边界与 parse_release_info（mock JSON dict）
"""
import pytest

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


def test_compare_version_prerelease_of_newer_core_is_newer():
    """预发布只在主版本相同时才降低优先级：1.0.7-beta 仍高于 1.0.6 与 1.0.0"""
    assert compare_version("1.0.7-beta", "1.0.6") == 1
    assert compare_version("1.0.7-beta", "1.0.0") == 1
    assert compare_version("1.0.7-beta", "1.0.7") == -1


# 每组 (较小, 较大)：按 SemVer 2.0 §11 的优先级严格递增
_STRICTLY_ORDERED = [
    ("2.0.0-beta.1", "2.0.0-beta.2"),
    ("2.0.0-beta.2", "2.0.0-rc.1"),
    ("2.0.0-rc.1", "2.0.0-rc.2"),
    ("2.0.0-rc.2", "2.0.0"),
    ("2.0.0", "2.0.1"),
    ("2.0.0-beta.1", "2.0.0"),          # 修复点：beta 用户必须收到正式版
    ("2.0.0-beta.9", "2.0.0-beta.10"),  # 数字标识符按数值而非字典序
    ("2.0.0-beta", "2.0.0-beta.1"),     # 前缀相同时标识符少者小
    ("2.0.0-alpha", "2.0.0-alpha.1"),
    ("2.0.0-alpha.1", "2.0.0-alpha.beta"),  # 数字标识符低于字母数字标识符
    ("2.0.0-alpha.beta", "2.0.0-beta"),
    ("2.0.0-beta.11", "2.0.0-rc.1"),
    ("2.0.0-1", "2.0.0-alpha"),          # 纯数字预发布低于字母预发布
    ("2.0.0-RC.1", "2.0.0-rc.1"),        # 字母数字按 ASCII（大写字母码位更小）
    ("1.4.1", "2.0.0-beta.1"),           # 旧正式版用户能收到更高主版本的预发布
    ("1.9.9", "2.0.0-alpha"),
    ("v2.0.0-beta.2", "V2.0.0-rc.1"),    # v/V 前缀照常容忍
    ("2.0", "2.0.1-beta.1"),             # 缺段补 0 后再比预发布
    ("2.0.0-beta.1+build.9", "2.0.0-beta.2+build.1"),  # 构建元数据不参与比较
]

_EQUAL = [
    ("2.0.0+build.1", "2.0.0+build.2"),
    ("2.0.0+abc", "2.0.0"),
    ("v2.0.0-rc.1", "2.0.0-rc.1"),
    ("2.0-rc.1", "2.0.0-rc.1"),
    ("2.0.0-beta.01", "2.0.0-beta.1"),   # 宽容：数字标识符的前导零按数值比较
    ("2.0.0-", "2.0.0"),                 # 宽容："-" 后为空视为没有预发布
]


@pytest.mark.parametrize("lower, higher", _STRICTLY_ORDERED)
def test_compare_version_semver_order(lower, higher):
    assert compare_version(lower, higher) == -1
    assert compare_version(higher, lower) == 1


@pytest.mark.parametrize("a, b", _EQUAL)
def test_compare_version_semver_equal(a, b):
    assert compare_version(a, b) == 0
    assert compare_version(b, a) == 0


def test_compare_version_full_chain_is_sorted():
    """完整发布链按 compare_version 排序后与预期顺序一致"""
    import functools
    chain = ["2.0.0-beta.1", "2.0.0-beta.2", "2.0.0-rc.1", "2.0.0", "2.0.1"]
    shuffled = [chain[3], chain[0], chain[4], chain[2], chain[1]]
    assert sorted(shuffled, key=functools.cmp_to_key(compare_version)) == chain


@pytest.mark.parametrize("bad", [
    "", "   ", "abc", "v", "-", "+", "1..2", "1.x.3", "1.0.0-beta..1", "1.0.0-+",
    None, 1, 2.5, "１.０.０",
])
def test_compare_version_never_raises_on_bad_input(bad):
    """非法输入不抛异常，结果仍是 -1/0/1；非数字主版本段按 0 处理"""
    assert compare_version(bad, "1.0.0") in (-1, 0, 1)
    assert compare_version("1.0.0", bad) in (-1, 0, 1)


def test_compare_version_non_numeric_core_segment_is_zero():
    """主版本里的非数字段（含全角数字、空段）按 0 处理，沿用旧宽容度"""
    assert compare_version("1.x.3", "1.0.3") == 0
    assert compare_version("1..2", "1.0.2") == 0
    assert compare_version("abc", "0") == 0


def test_parse_release_info_beta_user_sees_ga_as_update():
    """beta 用户检查更新时，正式版必须判定为有更新；正式版用户不被同号 beta 打扰"""
    assert parse_release_info(_base_release(tag="v2.0.0"),
                              current_version="2.0.0-beta.1")["has_update"] is True
    assert parse_release_info(_base_release(tag="v2.0.0-rc.1"),
                              current_version="2.0.0")["has_update"] is False
    assert parse_release_info(_base_release(tag="v2.0.0-beta.2"),
                              current_version="2.0.0-beta.1")["has_update"] is True


# ========== parse_release_info ==========

def _base_release(tag="v1.2.0", assets=None, body="更新说明"):
    return {
        "tag_name": tag,
        "body": body,
        "html_url": "https://github.com/liujialu0330/ProcessMonitor/releases/tag/v1.2.0",
        "assets": assets or [],
    }


def test_parse_release_info_with_setup_asset():
    data = _base_release(assets=[
        {"name": "Windows_v1.2.0_Setup.exe",
         "browser_download_url": "https://example.com/Windows_v1.2.0_Setup.exe",
         "size": 12345678},
        {"name": "source.zip", "browser_download_url": "https://example.com/source.zip", "size": 999},
    ])

    info = parse_release_info(data, current_version="1.1.0")

    assert info["version"] == "1.2.0"
    assert info["has_update"] is True
    assert info["notes"] == "更新说明"
    assert info["html_url"] == data["html_url"]
    assert info["asset_url"] == "https://example.com/Windows_v1.2.0_Setup.exe"
    assert info["asset_name"] == "Windows_v1.2.0_Setup.exe"
    assert info["asset_size"] == 12345678


def test_parse_release_info_without_setup_asset_degrades():
    """无 Setup 资产时降级：asset_url/asset_name 为 None，asset_size 为 0，但 version/has_update 仍正常解析"""
    data = _base_release(assets=[
        {"name": "source.zip", "browser_download_url": "https://example.com/source.zip", "size": 999},
    ])

    info = parse_release_info(data, current_version="1.1.0")

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


def test_parse_release_info_no_update_when_same_version():
    data = _base_release(tag="v1.2.0")
    info = parse_release_info(data, current_version="1.2.0")
    assert info["has_update"] is False


def test_parse_release_info_older_remote_version():
    data = _base_release(tag="v1.2.0")
    info = parse_release_info(data, current_version="2.0.0")
    assert info["has_update"] is False


def test_parse_release_info_strips_v_prefix_and_empty_body():
    data = {"tag_name": "V1.3.0", "assets": []}
    info = parse_release_info(data)
    assert info["version"] == "1.3.0"
    assert info["notes"] == ""


# ========== asset digest / 偏好读写日志 ==========

def test_parse_release_info_reads_asset_digest():
    digest = "sha256:" + "ab" * 32
    data = _base_release(assets=[
        {"name": "Windows_v1.2.0_Setup.exe",
         "browser_download_url": "https://github.com/x/y/releases/download/v1.2.0/s.exe",
         "size": 10, "digest": digest},
    ])
    assert parse_release_info(data, current_version="1.1.0")["asset_digest"] == digest


def test_parse_release_info_missing_or_invalid_digest_is_none():
    for digest_field in ({}, {"digest": None}, {"digest": ""}, {"digest": "md5:abc"},
                         {"digest": "sha256:short"}):
        asset = {"name": "Windows_v1.2.0_Setup.exe",
                 "browser_download_url": "https://github.com/x", "size": 1}
        asset.update(digest_field)
        assert parse_release_info(_base_release(assets=[asset]))["asset_digest"] is None


def test_no_setup_asset_digest_is_none():
    info = parse_release_info(_base_release(assets=[]))
    assert info["asset_digest"] is None


# ========== UpdateChecker（BackgroundTask 子类，mock 网络层，同线程直接 run） ==========

def test_update_prefs_helpers_moved_out_of_update_checker():
    """跳过的版本改存 app_config：本模块不再读写 update_prefs.json"""
    import core.update_checker as module
    for name in ("load_update_prefs", "save_update_prefs",
                 "get_skipped_version", "set_skipped_version"):
        assert not hasattr(module, name), name


def _run_checker(monkeypatch, urlopen):
    from PyQt5.QtCore import Qt
    from core.update_checker import UpdateChecker
    monkeypatch.setattr('core.update_checker.urllib.request.urlopen', urlopen)
    checker = UpdateChecker()
    got = {'finished': [], 'errors': [], 'result': [], 'failed': []}
    checker.check_finished.connect(got['finished'].append, Qt.DirectConnection)
    checker.error_occurred.connect(got['errors'].append, Qt.DirectConnection)
    checker.result.connect(got['result'].append, Qt.DirectConnection)
    checker.failed.connect(got['failed'].append, Qt.DirectConnection)
    checker.run()
    return got


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_checker_success_emits_check_finished_and_result(qapp, monkeypatch):
    import json
    body = json.dumps(_base_release()).encode('utf-8')
    got = _run_checker(monkeypatch, lambda req, timeout: _Resp(body))

    assert got['errors'] == [] and got['failed'] == []
    assert len(got['finished']) == 1 and got['result'] == got['finished']
    assert got['finished'][0]['version'] == parse_release_info(_base_release())['version']


def _http_error(code):
    import urllib.error

    def _raise(req, timeout):
        raise urllib.error.HTTPError("https://api.github.com", code, "x", {}, None)
    return _raise


def test_checker_404_reports_no_release_without_error_log(qapp, monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        got = _run_checker(monkeypatch, _http_error(404))
    assert got['errors'] == ["仓库暂无发布版本"] and got['failed'] == got['errors']
    assert got['finished'] == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_checker_http_error_reports_status(qapp, monkeypatch):
    got = _run_checker(monkeypatch, _http_error(500))
    assert got['errors'] == ["检查更新失败（HTTP 500）"]


def test_checker_network_error_reports_message(qapp, monkeypatch):
    def _raise(req, timeout):
        raise OSError("连接超时")
    got = _run_checker(monkeypatch, _raise)
    assert got['errors'] == ["检查更新失败：连接超时"]
    assert got['finished'] == []


def test_parse_release_info_defaults_to_app_version():
    """不传 current_version 时与 config.APP_VERSION 比较"""
    import config
    assert parse_release_info(_base_release(tag=f"v{config.APP_VERSION}"))["has_update"] is False
