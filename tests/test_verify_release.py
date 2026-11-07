"""
build/verify_release.py 用例：每条规则的通过与失败（离线 --release-json），外加在线模式走本地假服务端

规则见脚本文件头 (a)–(g)。每个用例从一份"全部通过"的正式版 Release 出发只改一处，断言对应规则
失败、退出码为 1；基准用例断言全部通过、退出码为 0。
"""
import copy
import hashlib
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "build" / "verify_release.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_release_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vr = _load_script()

SETUP_BYTES = b"MZ" + b"\x00" * 4094
SETUP_SHA = hashlib.sha256(SETUP_BYTES).hexdigest()
REPO_URL = "https://github.com/liujialu0330/ProcessMonitor"


def _release(version="2.0.0", *, prerelease=None, draft=False, body=None, name=None,
             assets=None):
    tag = f"v{version}"
    setup = f"Windows_v{version}_Setup.exe"
    return {
        "tag_name": tag,
        "name": name if name is not None else f"进程监控助手 {tag}",
        "draft": draft,
        "prerelease": ("-" in version) if prerelease is None else prerelease,
        "html_url": f"{REPO_URL}/releases/tag/{tag}",
        "body": body if body is not None else "## 升级须知\n- 数据库升级到 v5\n\n## 亮点\n- 自动化",
        "assets": assets if assets is not None else [
            {"name": setup, "size": len(SETUP_BYTES), "state": "uploaded",
             "digest": f"sha256:{SETUP_SHA}",
             "browser_download_url": f"{REPO_URL}/releases/download/{tag}/{setup}"},
        ],
    }


@pytest.fixture
def workspace(tmp_path):
    """一套自洽的发布现场：config.py、CHANGELOG.md、本地安装包"""
    class Workspace:
        root = tmp_path

        def write_config(self, version):
            path = tmp_path / "config.py"
            path.write_text(
                f'APP_NAME = "进程监控助手"\nAPP_VERSION = "{version}"\n'
                'GITHUB_OWNER = "liujialu0330"\nGITHUB_REPO = "ProcessMonitor"\n',
                encoding="utf-8")
            return path

        def write_changelog(self, top_heading):
            path = tmp_path / "CHANGELOG.md"
            path.write_text(
                "# 更新日志\n\n> 说明\n\n"
                f"### {top_heading}\n\n#### 新增\n- 某功能\n\n### v1.4.1 (2026-07-16)\n- 修复\n",
                encoding="utf-8")
            return path

        def write_setup(self, content=SETUP_BYTES):
            path = tmp_path / "Windows_Setup_local.exe"
            path.write_bytes(content)
            return path

        def write_json(self, data, name="release.json"):
            path = tmp_path / name
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return path

        def run(self, release, *, tag=None, version="2.0.0", app_version=None,
                changelog=None, local_setup=True, extra=()):
            config = self.write_config(app_version or version)
            log = self.write_changelog(changelog or f"v{version} (2026-12-01)")
            argv = ["--tag", tag or f"v{version}", "--release-json", str(self.write_json(release)),
                    "--config", str(config), "--changelog", str(log)]
            if local_setup is True:
                argv += ["--local-setup", str(self.write_setup())]
            elif local_setup:
                argv += ["--local-setup", str(local_setup)]
            argv += list(extra)
            out = io.StringIO()
            code = vr.main(argv, stream=out)
            return code, out.getvalue()

    return Workspace()


def _failed_rules(output):
    """输出里失败项的规则编号集合"""
    rules = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("✗ ("):
            rules.add(line[3])
    return rules


# ========== 基准：全部通过 ==========

def test_ga_release_passes_all_rules(workspace):
    code, out = workspace.run(_release())
    assert code == 0, out
    assert _failed_rules(out) == set()
    assert "✗" not in out
    assert "结果：通过" in out and "失败 0 项" in out


def test_prerelease_passes_without_upgrade_notes(workspace):
    release = _release("2.0.0-beta.1", body="内测版本")
    code, out = workspace.run(release, version="2.0.0-beta.1")
    assert code == 0, out
    assert "- (e) 正文含“升级须知”：预发布不要求" in out


def test_rc_prerelease_passes(workspace):
    code, out = workspace.run(_release("2.0.0-rc.2"), version="2.0.0-rc.2")
    assert code == 0, out


# ========== (a) 版本四处一致 ==========

def test_a_config_version_mismatch(workspace):
    code, out = workspace.run(_release(), app_version="2.0.0-beta.1")
    assert code == 1 and _failed_rules(out) == {"a"}
    assert "APP_VERSION 为 2.0.0-beta.1" in out


def test_a_changelog_still_unreleased(workspace):
    code, out = workspace.run(_release(), changelog="未发布")
    assert code == 1 and _failed_rules(out) == {"a"}
    assert "顶部仍为“未发布”段" in out


@pytest.mark.parametrize("version", ["2.0.0-beta.1", "2.0.0-rc.1"])
def test_a_prerelease_may_keep_unreleased_changelog(workspace, version):
    """发布计划：beta / rc 期间 CHANGELOG 仍写在"未发布"段"""
    code, out = workspace.run(_release(version), version=version, changelog="未发布")
    assert code == 0, out
    assert "预发布期间为“未发布”段" in out


def test_a_prerelease_changelog_with_other_version_fails(workspace):
    code, out = workspace.run(_release("2.0.0-beta.2"), version="2.0.0-beta.2",
                              changelog="v2.0.0-beta.1 (2026-11-20)")
    assert code == 1 and _failed_rules(out) == {"a"}


def test_a_changelog_top_is_other_version(workspace):
    code, out = workspace.run(_release(), changelog="v1.4.1 (2026-07-16)")
    assert code == 1 and _failed_rules(out) == {"a"}


@pytest.mark.parametrize("name", ["", "进程监控助手 v2.0.0-beta.1", "v12.0.0", "2.0.0.1 预览"])
def test_a_release_name_without_exact_version(workspace, name):
    code, out = workspace.run(_release(name=name))
    assert code == 1 and _failed_rules(out) == {"a"}


@pytest.mark.parametrize("name", ["v2.0.0", "2.0.0", "进程监控助手 v2.0.0", "v2.0.0（正式版）",
                                  "进程监控助手v2.0.0."])
def test_a_release_name_accepted_forms(workspace, name):
    code, out = workspace.run(_release(name=name))
    assert code == 0, out


def test_a_release_tag_differs_from_argument(workspace):
    release = _release()
    release["tag_name"] = "v2.0.1"
    code, out = workspace.run(release)
    assert code == 1 and "a" in _failed_rules(out)
    assert "tag_name 为 'v2.0.1'" in out


# ========== (b) 唯一的 ASCII 安装包资产 ==========

def _asset(name, **extra):
    asset = {"name": name, "size": len(SETUP_BYTES), "state": "uploaded",
             "digest": f"sha256:{SETUP_SHA}", "browser_download_url": f"{REPO_URL}/x/{name}"}
    asset.update(extra)
    return asset


def test_b_two_setup_exes(workspace):
    release = _release(assets=[_asset("pmctl_setup.exe"), _asset("Windows_v2.0.0_Setup.exe")])
    code, out = workspace.run(release)
    assert code == 1 and _failed_rules(out) == {"b"}
    assert "找到 2 个" in out and "1.4.1 只取第一个" in out


def test_b_no_setup_exe(workspace):
    release = _release(assets=[_asset("Windows_v2.0.0_Installer.exe")])
    code, out = workspace.run(release)
    assert code == 1 and _failed_rules(out) == {"b"}
    assert "找到 0 个" in out


def test_b_non_ascii_name(workspace):
    release = _release(assets=[_asset("进程监控助手_v2.0.0_Setup.exe")])
    code, out = workspace.run(release)
    assert code == 1 and _failed_rules(out) == {"b"}
    assert "非 ASCII" in out


@pytest.mark.parametrize("name", ["Windows_v2.0.1_Setup.exe", "windows_v2.0.0_setup.exe",
                                  "ProcessMonitor_Setup.exe"])
def test_b_wrong_name(workspace, name):
    code, out = workspace.run(_release(assets=[_asset(name)]))
    assert code == 1 and _failed_rules(out) == {"b"}
    assert "应为 Windows_v2.0.0_Setup.exe" in out


def test_b_other_assets_do_not_count(workspace):
    """校验和、源码包、便携版不算安装包"""
    release = _release()
    release["assets"] = [_asset("Windows_v2.0.0_Setup.exe.sha256"), _asset("Source code (zip)"),
                         _asset("Windows_v2.0.0_Portable.zip")] + release["assets"]
    code, out = workspace.run(release)
    assert code == 0, out


def test_b_asset_not_fully_uploaded(workspace):
    release = _release(assets=[_asset("Windows_v2.0.0_Setup.exe", state="starter")])
    code, out = workspace.run(release)
    assert code == 1 and _failed_rules(out) == {"b"}


# ========== (c) digest ==========

@pytest.mark.parametrize("digest", [None, "", "md5:" + "a" * 32, "sha256:abc"])
def test_c_missing_or_invalid_digest(workspace, digest):
    extra = {} if digest is None else {"digest": digest}
    asset = _asset("Windows_v2.0.0_Setup.exe")
    del asset["digest"]
    asset.update(extra)
    code, out = workspace.run(_release(assets=[asset]))
    assert code == 1 and _failed_rules(out) == {"c"}
    assert "拒绝自动安装" in out


def test_c_digest_differs_from_local_file(workspace):
    other = workspace.write_setup(b"MZ" + b"\x01" * 4094)
    code, out = workspace.run(_release(), local_setup=other)
    assert code == 1 and _failed_rules(out) == {"c"}
    assert "≠ Release" in out


def test_c_size_differs_from_local_file(workspace):
    asset = _asset("Windows_v2.0.0_Setup.exe", size=len(SETUP_BYTES) + 1)
    code, out = workspace.run(_release(assets=[asset]))
    assert code == 1 and _failed_rules(out) == {"c"}


def test_c_local_file_missing(workspace):
    code, out = workspace.run(_release(), local_setup=workspace.root / "nope.exe")
    assert code == 1 and _failed_rules(out) == {"c"}


def test_c_without_local_setup_only_checks_format(workspace):
    code, out = workspace.run(_release(), local_setup=None)
    assert code == 0, out
    assert "未提供 --local-setup" in out


def test_c_digest_case_insensitive(workspace):
    asset = _asset("Windows_v2.0.0_Setup.exe", digest=f"sha256:{SETUP_SHA.upper()}")
    code, out = workspace.run(_release(assets=[asset]))
    assert code == 0, out


# ========== (d) prerelease / draft ==========

@pytest.mark.parametrize("version", ["2.0.0-beta.1", "2.0.0-rc.1"])
def test_d_suffixed_tag_must_be_prerelease(workspace, version):
    code, out = workspace.run(_release(version, prerelease=False), version=version)
    assert code == 1 and _failed_rules(out) == {"d"}
    assert "推送给 1.4.1 用户" in out


def test_d_ga_must_not_be_prerelease(workspace):
    code, out = workspace.run(_release(prerelease=True))
    assert code == 1 and _failed_rules(out) == {"d"}


def test_d_ga_must_not_be_draft(workspace):
    code, out = workspace.run(_release(draft=True))
    assert code == 1 and _failed_rules(out) == {"d"}


def test_d_allow_draft_for_pre_publish_check(workspace):
    code, out = workspace.run(_release(draft=True), extra=["--allow-draft"])
    assert code == 0, out
    assert "发布后请不带 --allow-draft 复核" in out


def test_d_prerelease_draft_needs_allow_draft(workspace):
    release = _release("2.0.0-beta.1", draft=True, body="内测")
    code, out = workspace.run(release, version="2.0.0-beta.1")
    assert code == 1 and _failed_rules(out) == {"d"}
    code, out = workspace.run(release, version="2.0.0-beta.1", extra=["--allow-draft"])
    assert code == 0, out


# ========== (e) 正文 ==========

def test_e_body_at_limit_passes(workspace):
    body = "\n".join(["## 升级须知"] + [f"- 第 {i} 条" for i in range(vr.MAX_BODY_LINES - 1)])
    code, out = workspace.run(_release(body=body + "\n\n"))
    assert code == 0, out


def test_e_body_too_long(workspace):
    body = "\n".join(["## 升级须知"] + [f"- 第 {i} 条" for i in range(vr.MAX_BODY_LINES)])
    code, out = workspace.run(_release(body=body))
    assert code == 1 and _failed_rules(out) == {"e"}
    assert f"{vr.MAX_BODY_LINES + 1} 行" in out


def test_e_body_crlf_counts_lines_once(workspace):
    body = "\r\n".join(["## 升级须知"] + ["- 条目"] * (vr.MAX_BODY_LINES - 1))
    code, out = workspace.run(_release(body=body))
    assert code == 0, out


def test_e_ga_body_without_upgrade_notes(workspace):
    code, out = workspace.run(_release(body="## 亮点\n- 自动化"))
    assert code == 1 and _failed_rules(out) == {"e"}


def test_e_prerelease_body_too_long_still_fails(workspace):
    body = "\n".join(["- 条目"] * (vr.MAX_BODY_LINES + 5))
    code, out = workspace.run(_release("2.0.0-beta.1", body=body), version="2.0.0-beta.1")
    assert code == 1 and _failed_rules(out) == {"e"}


# ========== (f) tag 形态 ==========

def test_f_tag_without_v(workspace):
    release = _release()
    release["tag_name"] = "2.0.0"
    code, out = workspace.run(release, tag="2.0.0")
    assert code == 1 and _failed_rules(out) == {"f"}
    assert "应为 v2.0.0" in out


@pytest.mark.parametrize("version", ["2.0.0-beta", "2.0.0-alpha.1", "2.0", "2.0.0-preview.1"])
def test_f_tag_outside_naming_rules(workspace, version):
    code, out = workspace.run(_release(version), version=version)
    assert code == 1 and "f" in _failed_rules(out)


# ========== (g) releases/latest（离线 --latest-json） ==========

def test_g_skipped_without_latest(workspace):
    code, out = workspace.run(_release())
    assert code == 0 and "- (g) releases/latest 指向：离线模式且未提供 --latest-json" in out


def test_g_ga_must_be_latest(workspace):
    latest = workspace.write_json({"tag_name": "v1.4.1"}, "latest.json")
    code, out = workspace.run(_release(), extra=["--latest-json", str(latest)])
    assert code == 1 and _failed_rules(out) == {"g"}

    latest = workspace.write_json({"tag_name": "v2.0.0"}, "latest.json")
    code, out = workspace.run(_release(), extra=["--latest-json", str(latest)])
    assert code == 0, out


def test_g_prerelease_must_not_be_latest(workspace):
    release = _release("2.0.0-beta.1", body="内测")
    latest = workspace.write_json({"tag_name": "v2.0.0-beta.1"}, "latest.json")
    code, out = workspace.run(release, version="2.0.0-beta.1",
                              extra=["--latest-json", str(latest)])
    assert code == 1 and _failed_rules(out) == {"g"}

    latest = workspace.write_json({"tag_name": "v1.4.1"}, "latest.json")
    code, out = workspace.run(release, version="2.0.0-beta.1",
                              extra=["--latest-json", str(latest)])
    assert code == 0, out


# ========== 辅助函数与输入 ==========

def test_read_config_value_does_not_execute_code(tmp_path):
    path = tmp_path / "config.py"
    path.write_text('import os\nos.exit_marker = 1\nAPP_VERSION = "3.1.4"\n', encoding="utf-8")
    assert vr.read_config_value(str(path), "APP_VERSION") == "3.1.4"
    assert not hasattr(os, "exit_marker")


def test_real_project_config_and_changelog_are_readable():
    """项目自己的 config.py / CHANGELOG.md 能被脚本读到（格式变了要同步改脚本）"""
    version = vr.read_config_value(str(PROJECT_ROOT / "config.py"), "APP_VERSION")
    import config
    assert version == config.APP_VERSION
    top = vr.changelog_top_section(str(PROJECT_ROOT / "CHANGELOG.md"))
    assert top is not None
    assert top.startswith(vr.UNRELEASED_TITLE) or top.lstrip("vV").startswith(
        vr.tag_version(f"v{version}").split("-")[0])


def test_symbol_fallback_for_gbk_console():
    gbk = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    assert vr._symbols(gbk) == ("√", "×", "-")
    assert vr._symbols(io.StringIO())[0] == "✓"


def test_bad_release_json_exits_1(workspace):
    path = workspace.root / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    out = io.StringIO()
    code = vr.main(["--tag", "v2.0.0", "--release-json", str(path),
                    "--config", str(workspace.write_config("2.0.0")),
                    "--changelog", str(workspace.write_changelog("v2.0.0"))], stream=out)
    assert code == 1 and "获取 Release 失败" in out.getvalue()


def test_command_line_exit_codes(workspace):
    """作为脚本运行：通过退出码 0、失败退出码 1、缺参数 2"""
    good = workspace.write_json(_release(), "good.json")
    bad = workspace.write_json(_release(prerelease=True), "bad.json")
    config = workspace.write_config("2.0.0")
    log = workspace.write_changelog("v2.0.0 (2026-12-01)")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                              env=env, timeout=60)

    common = ["--config", str(config), "--changelog", str(log)]
    ok = run("--tag", "v2.0.0", "--release-json", str(good), *common)
    assert ok.returncode == 0, ok.stdout.decode("utf-8") + ok.stderr.decode("utf-8")
    assert "✓ (b)" in ok.stdout.decode("utf-8")
    failed = run("--tag", "v2.0.0", "--release-json", str(bad), *common)
    assert failed.returncode == 1
    assert run("--release-json", str(good)).returncode == 2


# ========== 在线模式：本地假 GitHub API ==========

class _FakeApi:
    def __init__(self, routes):
        self.routes = routes
        self.auth = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fake.auth.append(self.headers.get("Authorization"))
                body = fake.routes.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    servers = []

    def make(routes):
        api = _FakeApi(routes)
        servers.append(api)
        return api
    yield make
    for api in servers:
        api.close()


def _online(workspace, api, tag, version, *extra):
    out = io.StringIO()
    code = vr.main(["--tag", tag, "--repo", "o/r", "--api-base", api.base,
                    "--config", str(workspace.write_config(version)),
                    "--changelog", str(workspace.write_changelog(f"v{version}")),
                    "--local-setup", str(workspace.write_setup()), *extra], stream=out)
    return code, out.getvalue()


def test_online_ga_published_and_latest(workspace, fake_api):
    api = fake_api({"/repos/o/r/releases/tags/v2.0.0": _release(),
                    "/repos/o/r/releases/latest": _release()})
    code, out = _online(workspace, api, "v2.0.0", "2.0.0", "--token", "t0k")
    assert code == 0, out
    assert "在线：o/r" in out and "✓ (g)" in out
    assert set(api.auth) == {"Bearer t0k"}


def test_online_ga_not_latest_fails(workspace, fake_api):
    api = fake_api({"/repos/o/r/releases/tags/v2.0.0": _release(),
                    "/repos/o/r/releases/latest": _release("1.4.1")})
    code, out = _online(workspace, api, "v2.0.0", "2.0.0")
    assert code == 1 and _failed_rules(out) == {"g"}


def test_online_draft_found_through_list_with_token(workspace, fake_api):
    """草稿按 tag 查不到（404），带令牌时从列表里找到"""
    draft = _release(draft=True)
    api = fake_api({"/repos/o/r/releases?per_page=100": [_release("1.4.1"), draft],
                    "/repos/o/r/releases/latest": _release("1.4.1")})
    code, out = _online(workspace, api, "v2.0.0", "2.0.0", "--token", "t", "--allow-draft")
    assert code == 0, out
    assert "- (g) releases/latest 指向：草稿尚未发布" in out


def test_online_missing_release_exits_1(workspace, fake_api):
    api = fake_api({})
    code, out = _online(workspace, api, "v2.0.0", "2.0.0")
    assert code == 1 and "获取 Release 失败" in out


def test_setup_assets_matches_v141_selection():
    """脚本判定"含 setup 的 exe"的口径与 1.4.1 更新器一致"""
    from tests.fixtures.frozen_v1_4_1 import update_checker as v141
    names = ["a.EXE", "Setup.exe.sha256", "x_SETUP.EXE", "setup.zip", "Source code (zip)",
             "my-setup-tool.exe"]
    release = {"tag_name": "v9.9.9", "assets": [{"name": n, "browser_download_url": n}
                                                for n in names]}
    picked = [a["name"] for a in vr.setup_assets(release)]
    assert picked == ["x_SETUP.EXE", "my-setup-tool.exe"]
    assert v141.parse_release_info(copy.deepcopy(release))["asset_name"] == picked[0]
