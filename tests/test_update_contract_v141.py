"""
更新通道契约：已安装的 1.4.1 如何看待 2.x 的 Release（规划 F-6）

`tests/fixtures/frozen_v1_4_1/update_checker.py` 是 1.4.1 更新器的冻结副本。存量用户能否从
1.4.1 自动升到 2.x，完全取决于这份已经发出去、再也改不了的代码怎么解析 `releases/latest`
返回的 JSON，所以 2.x 的每次发布都必须落在它能正确处理的形态里。本文件把它的实际行为
逐条钉住；下面的"发布契约"即由这些用例推出，`build/verify_release.py` 按同一份契约校验。

1.4.1 的实际行为（用例断言的事实）：
1. 版本：`tag_name`（缺失时退回 `name`）去掉 v/V 前缀即版本号；`v2.0.0` 对 1.4.1 判为有更新。
2. 选资产：按 API 返回顺序取**第一个**文件名（不区分大小写）含 `setup` 且以 `.exe` 结尾的资产，
   取其 `browser_download_url`、`name`、`size`；不看 `digest`、`content_type`、`state`，不校验域名。
   `Source code (zip)`、`*.exe.sha256`、`*_Setup.zip` 之类不会被误选，排在前面也不影响。
3. 两个都含 setup 的 exe：谁排在前面选谁，可能选错（例如选到 `pmctl_setup.exe`）。
4. 中文资产名：1.4.1 照样能选中（只看 setup 与 .exe），并把它原样当作本地文件名；但 GitHub
   上传时可能改写非 ASCII 文件名，最终名称不可控，故不能依赖。
5. 没有任何含 setup 的 exe：`asset_url` 为 None，1.4.1 的界面改为打开 `html_url` 发布页。
6. 不读 `digest`：资产缺 digest 或 digest 与内容不符，1.4.1 都照常下载并启动安装（下载流程见
   `tests/test_update_fake_release.py` 的 1.4.1 部分）。1.4.1 → 2.x 这一跳的完整性只靠 HTTPS、
   GitHub 自身与 Content-Length 长度检查；2.x 起才强制 SHA-256。
7. 预发布：GitHub 的 `releases/latest` 只返回最新的非 draft、非 prerelease 版本，所以正确标记为
   prerelease 的 `v2.0.0-beta.1` 不会推给 1.4.1 用户。一旦漏标，1.4.1 的旧版比较把
   `2.0.0-beta.1` 拆成 `[2, 0, 0, 1]`，判为高于 1.4.1，存量用户会被推送 beta；它甚至认为
   `2.0.0-beta.1` 高于 `2.0.0`、`2.0.0-rc.1` 低于 `2.0.0-beta.2`。

由此得出的发布契约（2.x 期间不得变更）：
- tag 为 `v{版本}`，正式版 `vX.Y.Z`；预发布只用 `vX.Y.Z-beta.N` / `vX.Y.Z-rc.N`，且必须勾选
  prerelease；正式版不得是 draft 或 prerelease。
- Release 恰好一个文件名含 setup 的 exe，名为 ASCII 的 `Windows_v{版本}_Setup.exe`；其他附件
  （校验和、便携包）的文件名不得同时含 setup 并以 .exe 结尾。
- 资产必须带 GitHub 生成的 `digest: sha256:…`：1.4.1 不用它，但 2.x 的更新器缺它就拒绝自动安装。
- Release 必须有 `html_url`（GitHub 总会给）：任何一端选不到资产时都靠它引导手动下载。
"""
import ast
import functools
import subprocess
from pathlib import Path

import pytest

from core.update_checker import parse_release_info as current_parse_release_info
from tests.fixtures.frozen_v1_4_1 import update_checker as v141

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FROZEN_PATH = PROJECT_ROOT / 'tests' / 'fixtures' / 'frozen_v1_4_1' / 'update_checker.py'

REPO_URL = "https://github.com/liujialu0330/ProcessMonitor"
SETUP_NAME = "Windows_v2.0.0_Setup.exe"
SETUP_URL = f"{REPO_URL}/releases/download/v2.0.0/{SETUP_NAME}"
SETUP_SIZE = 37_421_056
SETUP_DIGEST = "sha256:" + "5e" * 32


def _asset(name, url=None, size=1024, digest=None, **extra):
    """GitHub Release API 的 asset 对象（只放与选择逻辑相关的字段）"""
    asset = {
        "name": name,
        "browser_download_url": url or f"{REPO_URL}/releases/download/v2.0.0/{name}",
        "size": size,
        "content_type": "application/x-msdownload",
        "state": "uploaded",
    }
    if digest is not None:
        asset["digest"] = digest
    asset.update(extra)
    return asset


def _setup_asset(**overrides):
    fields = dict(url=SETUP_URL, size=SETUP_SIZE, digest=SETUP_DIGEST)
    fields.update(overrides)
    return _asset(SETUP_NAME, **fields)


def _release(tag="v2.0.0", assets=None, *, prerelease=False, draft=False,
             created_at="2026-12-01T08:00:00Z", name=None):
    """仿 GitHub `GET /repos/{owner}/{repo}/releases/latest` 的返回体"""
    return {
        "tag_name": tag,
        "name": name if name is not None else tag,
        "draft": draft,
        "prerelease": prerelease,
        "created_at": created_at,
        "published_at": created_at,
        "html_url": f"{REPO_URL}/releases/tag/{tag}",
        "body": "## 升级须知\n- 数据库升级到 v5",
        "assets": assets if assets is not None else [_setup_asset()],
        "zipball_url": f"https://api.github.com/repos/liujialu0330/ProcessMonitor/zipball/{tag}",
        "tarball_url": f"https://api.github.com/repos/liujialu0330/ProcessMonitor/tarball/{tag}",
    }


def github_latest(releases):
    """GitHub `releases/latest` 的选取规则（文档行为，测试里用假数据说明）：
    最新的非 draft、非 prerelease 发布，按 created_at 排序；没有则 404（此处返回 None）"""
    published = [r for r in releases if not r["draft"] and not r["prerelease"]]
    return max(published, key=lambda r: r["created_at"]) if published else None


# ========== 冻结副本本身 ==========

def test_frozen_copy_is_the_v141_updater():
    """冻结副本代表 1.4.1 的版本号与仓库地址"""
    assert v141.config.APP_VERSION == "1.4.1"
    assert v141.GITHUB_API_LATEST == (
        "https://api.github.com/repos/liujialu0330/ProcessMonitor/releases/latest")
    assert not hasattr(v141, 'parse_sha256_digest'), "1.4.1 没有 digest 校验"


def _top_level_defs(tree):
    return {node.name: ast.dump(node) for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


def test_frozen_copy_matches_tag_v141():
    """除文件头声明的改动外，函数与类的语法树与 tag v1.4.1 逐个一致（取不到 tag 时跳过）"""
    try:
        original = subprocess.run(
            ["git", "show", "v1.4.1:core/update_checker.py"], cwd=PROJECT_ROOT,
            capture_output=True, check=True, timeout=30).stdout.decode('utf-8')
    except (OSError, subprocess.SubprocessError):
        pytest.skip("取不到 tag v1.4.1（浅克隆或没有 git）")
    frozen = FROZEN_PATH.read_text(encoding='utf-8')
    assert _top_level_defs(ast.parse(frozen)) == _top_level_defs(ast.parse(original))


# ========== 正式版 2.0.0：1.4.1 必须能选中唯一的安装包 ==========

def test_v141_selects_the_ga_setup_asset():
    data = _release(assets=[
        _asset("Source code (zip)", url=f"{REPO_URL}/archive/refs/tags/v2.0.0.zip",
               content_type="application/zip"),
        _setup_asset(),
    ])

    info = v141.parse_release_info(data)

    assert info["version"] == "2.0.0"
    assert info["has_update"] is True
    assert info["asset_url"] == SETUP_URL
    assert info["asset_name"] == SETUP_NAME
    assert info["asset_size"] == SETUP_SIZE
    assert info["html_url"] == f"{REPO_URL}/releases/tag/v2.0.0"
    assert "asset_digest" not in info, "1.4.1 不读 digest"


def test_current_updater_selects_the_same_asset_with_digest():
    """2.x 自己的更新器对同一份 JSON 选中同一资产，并拿到 digest"""
    data = _release(assets=[_asset("Source code (zip)"), _setup_asset()])

    info = current_parse_release_info(data, current_version="2.0.0-rc.1")
    old = v141.parse_release_info(data)

    assert (info["asset_url"], info["asset_name"], info["asset_size"]) == (
        old["asset_url"], old["asset_name"], old["asset_size"])
    assert info["asset_digest"] == SETUP_DIGEST
    assert info["has_update"] is True


@pytest.mark.parametrize("name", [
    "Windows_v2.0.0_Setup.exe.sha256",   # 校验和附件：不以 .exe 结尾
    "Windows_v2.0.0_Setup.zip",
    "Source code (zip)",
    "Source code (tar.gz)",
    "Windows_v2.0.0_Portable.exe",       # 便携包：不含 setup
])
def test_v141_ignores_non_setup_assets_listed_first(name):
    data = _release(assets=[_asset(name), _setup_asset()])
    assert v141.parse_release_info(data)["asset_name"] == SETUP_NAME


def test_v141_matches_setup_case_insensitively():
    data = _release(assets=[_asset("WINDOWS_V2.0.0_SETUP.EXE")])
    assert v141.parse_release_info(data)["asset_name"] == "WINDOWS_V2.0.0_SETUP.EXE"


def test_v141_falls_back_to_name_when_tag_missing():
    data = _release()
    del data["tag_name"]
    data["name"] = "v2.0.0"
    assert v141.parse_release_info(data)["version"] == "2.0.0"


# ========== 反例：1.4.1 处理不好的形态 ==========

def test_two_setup_exes_v141_takes_whichever_comes_first():
    """两个含 setup 的 exe：1.4.1 只看顺序，排在前面的若不是安装包就会下载错文件"""
    wrong = _asset("pmctl_setup.exe", size=2048)
    right = _setup_asset()

    assert v141.parse_release_info(_release(assets=[wrong, right]))["asset_name"] == "pmctl_setup.exe"
    assert v141.parse_release_info(_release(assets=[right, wrong]))["asset_name"] == SETUP_NAME


def test_non_ascii_name_is_selected_verbatim_by_v141():
    """中文资产名：1.4.1 照样选中并原样用作本地文件名；GitHub 是否改写该名称不可控，契约要求 ASCII"""
    name = "进程监控助手_v2.0.0_Setup.exe"
    info = v141.parse_release_info(_release(assets=[_asset(name)]))
    assert info["asset_name"] == name
    assert not name.isascii()


def test_missing_or_wrong_digest_does_not_stop_v141():
    """缺 digest 或 digest 不对，1.4.1 的解析结果完全一样：它会照常下载安装"""
    without = v141.parse_release_info(_release(assets=[_setup_asset(digest=None)]))
    wrong = v141.parse_release_info(_release(assets=[_setup_asset(digest="sha256:" + "00" * 32)]))
    good = v141.parse_release_info(_release())
    assert without == wrong == good
    assert good["asset_url"] == SETUP_URL


def test_current_updater_refuses_asset_without_digest():
    """同样缺 digest 的 Release，2.x 的更新器得到 asset_digest=None（下载线程据此拒绝、界面引导到发布页）"""
    info = current_parse_release_info(_release(assets=[_setup_asset(digest=None)]),
                                      current_version="2.0.0-rc.1")
    assert info["asset_url"] == SETUP_URL and info["asset_digest"] is None


def test_no_setup_exe_leaves_v141_with_release_page_only():
    """没有含 setup 的 exe：asset_url 为 None，1.4.1 界面只能打开发布页"""
    info = v141.parse_release_info(_release(assets=[_asset("Windows_v2.0.0_Installer.exe")]))
    assert info["asset_url"] is None and info["asset_name"] is None
    assert info["has_update"] is True
    assert info["html_url"] == f"{REPO_URL}/releases/tag/v2.0.0"


# ========== 预发布 ==========

V141_GA = _release("v1.4.1", created_at="2026-07-16T08:00:00Z")
BETA_1 = _release("v2.0.0-beta.1", prerelease=True, created_at="2026-11-20T08:00:00Z")


def test_releases_latest_never_returns_a_prerelease():
    """正确标记的 beta 不会出现在 releases/latest：1.4.1 仍只看到 1.4.1，提示"已是最新" """
    latest = github_latest([V141_GA, BETA_1])
    assert latest["tag_name"] == "v1.4.1"
    assert v141.parse_release_info(latest)["has_update"] is False


def test_drafts_are_never_latest_either():
    draft_ga = _release("v2.0.0", draft=True, created_at="2026-12-01T08:00:00Z")
    assert github_latest([V141_GA, BETA_1, draft_ga])["tag_name"] == "v1.4.1"


def test_unflagged_beta_would_be_pushed_to_v141_users():
    """漏标 prerelease 的 beta 会成为 latest，1.4.1 判为有更新：verify_release 规则 (d) 拦截这种发布"""
    unflagged = dict(BETA_1, prerelease=False)
    latest = github_latest([V141_GA, unflagged])

    info = v141.parse_release_info(latest)

    assert latest["tag_name"] == "v2.0.0-beta.1"
    assert info["version"] == "2.0.0-beta.1"
    assert info["has_update"] is True


def test_ga_after_beta_becomes_latest_for_v141():
    ga = _release("v2.0.0", created_at="2026-12-01T08:00:00Z")
    latest = github_latest([V141_GA, BETA_1, ga])
    assert latest["tag_name"] == "v2.0.0"
    assert v141.parse_release_info(latest)["has_update"] is True


# 1.4.1 的 compare_version 现状（按 "." 分段、非数字段按 0）：记录而非期望
_V141_COMPARE_FACTS = [
    ("2.0.0", "1.4.1", 1),
    ("2.0.1", "1.4.1", 1),
    ("2.0.0-beta.1", "1.4.1", 1),     # 漏标 prerelease 时会被推送
    ("2.0.0-beta.1", "2.0.0", 1),     # 错：SemVer 下 beta 低于正式版
    ("2.0.0-rc.1", "2.0.0-beta.2", -1),   # 错：只剩尾段数字 1 < 2
    ("2.0.0-beta.10", "2.0.0-beta.9", 1),
    ("2.0.0-beta.1", "2.0.0-rc.1", 0),    # 错：两者都成了 [2, 0, 0, 1]
    ("1.4.1", "1.4.1", 0),
]


@pytest.mark.parametrize("a, b, expected", _V141_COMPARE_FACTS)
def test_v141_compare_version_facts(a, b, expected):
    assert v141.compare_version(a, b) == expected


def test_current_compare_version_fixes_the_prerelease_cases():
    """2.x 的比较按 SemVer：同一组输入里 1.4.1 判错的几条在这里是对的"""
    from utils.version import compare_version
    chain = ["1.4.1", "2.0.0-beta.1", "2.0.0-beta.2", "2.0.0-beta.10", "2.0.0-rc.1",
             "2.0.0", "2.0.1"]
    shuffled = list(reversed(chain))
    assert sorted(shuffled, key=functools.cmp_to_key(compare_version)) == chain


def test_v141_parses_beta_tag_as_update():
    """tag v2.0.0-beta.1 在 1.4.1 眼里：版本号原样保留后缀，判为有更新"""
    info = v141.parse_release_info(BETA_1)
    assert info["version"] == "2.0.0-beta.1"
    assert info["has_update"] is True
