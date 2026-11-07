"""
更新通道端到端：本地假 GitHub 服务端（规划 F-6）

本地起一个 `http.server` 线程扮演 GitHub：`api.github.com` 的 `releases/latest`、`github.com`
的 `browser_download_url`（302 到资产域名）、资产域名本身，以及白名单外的"恶意"域名。
传输层把所有 http/https 请求改连到这台本地服务端（保留原始 Host 头，服务端据此分派），
`core.update_checker` 的检查、下载、重定向白名单与校验代码原样执行，不访问真实网络、不走代理。

覆盖的场景与当前更新器的结果：
- 正常（302 到 release-assets / objects 域名）→ 文件落地、SHA-256 校验通过；
- 302 到白名单外域名或降级到 http → 拒绝，恶意域名收不到任何请求；
- digest 缺失 → 不发起下载，界面引导到发布页；
- digest 不一致 → 删除下载文件并报错；
- Content-Length 大于实际（连接提前断开）/ 小于实际 / 缺失且与资产元数据不符 → 报错，无残留；
- 下载途中取消、读完最后一块之后才取消、结果交付前取消 → 不发信号、无残留文件；
- 资产 404 → 报错，无残留。

后半部分用 1.4.1 冻结副本走同一台服务端，记录存量用户升级到 2.x 这一跳的真实行为
（不校验 digest、302 到任何域名都跟随）。
"""
import hashlib
import http.client
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.request
from typing import Callable, Dict, List, Tuple

import pytest
from PyQt5.QtCore import Qt

import config
import core.update_checker as update_checker_module
from core.update_checker import MSG_CANNOT_VERIFY, UpdateChecker, UpdateDownloader
from tests.factories import make_about_update_group, make_services
from tests.fixtures.frozen_v1_4_1 import update_checker as v141

OWNER, REPO = "liujialu0330", "ProcessMonitor"
API_HOST = "api.github.com"
API_PATH = f"/repos/{OWNER}/{REPO}/releases/latest"
TAG = "v2.0.1"
SETUP_NAME = "Windows_v2.0.1_Setup.exe"
DOWNLOAD_PATH = f"/{OWNER}/{REPO}/releases/download/{TAG}/{SETUP_NAME}"
DOWNLOAD_URL = f"https://github.com{DOWNLOAD_PATH}"
ASSET_HOST = "release-assets.githubusercontent.com"
ASSET_PATH = "/github-production-release-asset/1/abc?sp=r&sig=x"
OBJECTS_HOST = "objects.githubusercontent.com"
EVIL_HOST = "evil.example.com"

# 跨过多个 64 KiB 读块，进度与取消检查点都会经过
PAYLOAD = bytes(range(256)) * 1024 + b"tail"   # 262148 字节
PAYLOAD_DIGEST = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()
CHUNK = 64 * 1024


# ---------------------------------------------------------------- 假服务端


class FakeGitHub:
    """按 (Host, 路径) 分派的本地 HTTP 服务端；记录收到的每个请求"""

    def __init__(self):
        self.routes: Dict[Tuple[str, str], Callable] = {}
        self.requests: List[Tuple[str, str, dict]] = []
        self.gate = threading.Event()          # 取消用例：服务端发完第一块后等它
        self._lock = threading.Lock()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"      # 每个响应后关闭连接，长度不符时客户端读到 EOF

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                headers = {k.lower(): v for k, v in self.headers.items()}
                with fake._lock:
                    fake.requests.append((host, self.path, headers))
                route = fake.routes.get((host, self.path))
                if route is None:
                    self.send_error(404, "Not Found")
                    return
                try:
                    route(self)
                except (ConnectionError, OSError):
                    pass   # 客户端已断开（拒绝或取消）

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()

    # ---- 路由 ----

    def route(self, host: str, path: str, responder: Callable) -> None:
        self.routes[(host, path)] = responder

    def hosts_requested(self) -> List[str]:
        with self._lock:
            return [host for host, _, _ in self.requests]


def json_body(data) -> Callable:
    raw = json.dumps(data).encode("utf-8")

    def respond(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)
    return respond


def redirect(location: str, code: int = 302) -> Callable:
    def respond(h):
        h.send_response(code)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        h.end_headers()
    return respond


def payload(content: bytes = PAYLOAD, content_length="auto", gate: threading.Event = None
            ) -> Callable:
    """content_length: "auto" 为真实长度，None 为不发该头，整数为指定值（可与实际不符）；
    gate 给出时先发第一块，等 gate 置位（最多 5 秒）再发其余"""
    def respond(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        if content_length == "auto":
            h.send_header("Content-Length", str(len(content)))
        elif content_length is not None:
            h.send_header("Content-Length", str(content_length))
        h.end_headers()
        if gate is not None:
            h.wfile.write(content[:CHUNK])
            h.wfile.flush()
            gate.wait(5)
            h.wfile.write(content[CHUNK:])
        else:
            h.wfile.write(content)
    return respond


def release_json(*, digest=PAYLOAD_DIGEST, url=DOWNLOAD_URL, size=len(PAYLOAD), tag=TAG):
    asset = {"name": SETUP_NAME, "browser_download_url": url, "size": size,
             "state": "uploaded", "content_type": "application/x-msdownload"}
    if digest is not None:
        asset["digest"] = digest
    return {
        "tag_name": tag, "name": tag, "draft": False, "prerelease": False,
        "html_url": f"https://github.com/{OWNER}/{REPO}/releases/tag/{tag}",
        "body": "## 升级须知\n- 示例",
        "assets": [{"name": "Source code (zip)", "size": 1,
                    "browser_download_url": f"https://github.com/{OWNER}/{REPO}/archive/{tag}.zip"},
                   asset],
    }


# ---------------------------------------------------------------- 传输层：一律改连本地


class _LoopbackMixin:
    port = 0

    def _connect(self, host, timeout=None, **_ignored):
        # 原始主机名留在 Host 头里，连接本身只去本地假服务端
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)


class _LoopbackHTTPS(_LoopbackMixin, urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(self._connect, req)


class _LoopbackHTTP(_LoopbackMixin, urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(self._connect, req)


@pytest.fixture
def server(monkeypatch):
    fake = FakeGitHub()
    fake.start()

    https, plain = _LoopbackHTTPS(), _LoopbackHTTP()
    https.port = plain.port = fake.port
    real_build_opener = urllib.request.build_opener

    def build_opener(*handlers):
        # 生产代码传入的处理器（如重定向白名单）原样保留；代理清空，http/https 都改连本地
        return real_build_opener(urllib.request.ProxyHandler({}), https, plain, *handlers)

    def urlopen(req, timeout=None, **_kw):
        return build_opener().open(req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    # 当前版本固定为 2.0.0，与 config 里正在开发的版本号解耦
    monkeypatch.setattr(config, "APP_VERSION", "2.0.0")
    yield fake
    fake.stop()


class _WorkDirs(list):
    """本次用例里 mkdtemp 创建的目录（按创建顺序）；root 为它们的共同上级"""
    root = None


@pytest.fixture
def work_dirs(tmp_path, monkeypatch):
    """mkdtemp 重定向到 tmp_path/dl 下并记录创建的目录；同时把系统临时目录指到这里（1.4.1 用 gettempdir）"""
    root = tmp_path / "dl"
    root.mkdir()
    created = _WorkDirs()
    created.root = root
    real_mkdtemp = tempfile.mkdtemp

    def _mkdtemp(*args, **kwargs):
        kwargs["dir"] = str(root)
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(update_checker_module.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return created


def _leftovers(work_dirs) -> List[str]:
    found = []
    for dirpath, _dirs, files in os.walk(work_dirs.root):
        found.extend(os.path.join(dirpath, f) for f in files)
    return found


# ---------------------------------------------------------------- 流程


def check(server_obj) -> dict:
    """跑一遍检查线程（同线程直接 run），返回 parse_release_info 的结果"""
    checker = UpdateChecker()
    got, errors = [], []
    checker.check_finished.connect(got.append, Qt.DirectConnection)
    checker.error_occurred.connect(errors.append, Qt.DirectConnection)
    checker.run()
    assert errors == [], errors
    return got[0]


def download(info: dict, on_progress=None):
    d = UpdateDownloader(info["asset_url"], info["asset_name"], info.get("asset_size", 0),
                         digest=info.get("asset_digest"))
    signals = {"finished": [], "errors": [], "failed": [], "result": []}
    d.download_finished.connect(signals["finished"].append, Qt.DirectConnection)
    d.error_occurred.connect(signals["errors"].append, Qt.DirectConnection)
    d.failed.connect(signals["failed"].append, Qt.DirectConnection)
    d.result.connect(signals["result"].append, Qt.DirectConnection)
    if on_progress is not None:
        d.download_progress.connect(lambda p: on_progress(d, p), Qt.DirectConnection)
    d.run()
    return d, signals


def standard_routes(fake, asset_responder=None, *, release=None):
    fake.route(API_HOST, API_PATH, json_body(release or release_json()))
    fake.route("github.com", DOWNLOAD_PATH, redirect(f"https://{ASSET_HOST}{ASSET_PATH}"))
    fake.route(ASSET_HOST, ASSET_PATH, asset_responder or payload())


# ================================================================ 当前更新器


def test_happy_path_check_download_verify(server, work_dirs, qapp):
    standard_routes(server)

    info = check(server)
    assert info["version"] == "2.0.1" and info["has_update"] is True
    assert info["asset_url"] == DOWNLOAD_URL and info["asset_digest"] == PAYLOAD_DIGEST

    _, sig = download(info)

    assert sig["errors"] == [] and len(sig["finished"]) == 1
    path = sig["finished"][0]
    assert os.path.basename(path) == SETUP_NAME
    assert os.path.dirname(path) == work_dirs[0]
    with open(path, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == PAYLOAD_DIGEST.split(":", 1)[1]
    assert server.hosts_requested() == [API_HOST, "github.com", ASSET_HOST]
    ua = {req[2].get("user-agent") for req in server.requests}
    assert ua == {update_checker_module.REQUEST_HEADERS["User-Agent"]}


def test_two_hop_redirect_through_objects_domain(server, work_dirs, qapp):
    obj_path = "/github-production-release-asset-2e65be/1"
    server.route("github.com", DOWNLOAD_PATH, redirect(f"https://{OBJECTS_HOST}{obj_path}", 301))
    server.route(OBJECTS_HOST, obj_path, redirect(f"https://sub.{ASSET_HOST}{ASSET_PATH}", 307))
    server.route(f"sub.{ASSET_HOST}", ASSET_PATH, payload())

    _, sig = download({"asset_url": DOWNLOAD_URL, "asset_name": SETUP_NAME,
                       "asset_size": len(PAYLOAD), "asset_digest": PAYLOAD_DIGEST})

    assert sig["errors"] == [] and len(sig["finished"]) == 1
    assert server.hosts_requested() == ["github.com", OBJECTS_HOST, f"sub.{ASSET_HOST}"]


@pytest.mark.parametrize("location", [
    f"https://{EVIL_HOST}/{SETUP_NAME}",
    f"http://{ASSET_HOST}{ASSET_PATH}",               # 降级到 http
    f"https://{ASSET_HOST}:8443{ASSET_PATH}",          # 非默认端口
    f"https://{ASSET_HOST}.{EVIL_HOST}{ASSET_PATH}",   # 前缀伪装
])
def test_redirect_outside_whitelist_is_refused(server, work_dirs, qapp, location):
    """恶意域名提供的是"正确"的安装包，只有白名单能挡住它"""
    standard_routes(server)
    server.route("github.com", DOWNLOAD_PATH, redirect(location))
    for host in (EVIL_HOST, ASSET_HOST, f"{ASSET_HOST}.{EVIL_HOST}"):
        server.route(host, f"/{SETUP_NAME}", payload())

    _, sig = download(check(server))

    assert sig["finished"] == [] and len(sig["errors"]) == 1
    assert "不在允许的下载域名内" in sig["errors"][0]
    assert server.hosts_requested() == [API_HOST, "github.com"], "白名单外地址不得被请求"
    assert _leftovers(work_dirs) == [] and not os.path.exists(work_dirs[0])


def test_untrusted_browser_download_url_is_refused_before_any_request(server, work_dirs, qapp):
    standard_routes(server, release=release_json(url=f"https://{EVIL_HOST}/{SETUP_NAME}"))
    server.route(EVIL_HOST, f"/{SETUP_NAME}", payload())

    _, sig = download(check(server))

    assert sig["finished"] == [] and "不受信任" in sig["errors"][0]
    assert server.hosts_requested() == [API_HOST]
    assert work_dirs == []


def test_missing_digest_refuses_without_downloading(server, work_dirs, qapp):
    standard_routes(server, release=release_json(digest=None))

    info = check(server)
    assert info["asset_digest"] is None and info["asset_url"] == DOWNLOAD_URL
    _, sig = download(info)

    assert sig["errors"] == [MSG_CANNOT_VERIFY] and sig["finished"] == []
    assert server.hosts_requested() == [API_HOST]
    assert work_dirs == []


def test_missing_digest_guides_user_to_release_page(server, work_dirs, qapp, monkeypatch):
    """界面侧：安装版检查到缺 digest 的新版本，不启动下载线程，提示并给出"打开发布页" """
    standard_routes(server, release=release_json(digest=None))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    services = make_services()
    group = make_about_update_group(services)
    info = check(server)

    group._start_update(info)

    assert group._downloader is None and services.threads.entries() == []
    notice = services.notifier.last("warning")
    assert notice.title == "无法自动更新"
    assert not group.release_button.isHidden()
    notice.bar.widgets[0].click()
    assert services.dialogs.calls[-1].args["url"] == info["html_url"]
    assert server.hosts_requested() == [API_HOST]


def test_digest_mismatch_deletes_file_and_reports(server, work_dirs, qapp):
    standard_routes(server, payload(PAYLOAD[:-4] + b"evil"))

    _, sig = download(check(server))

    assert sig["finished"] == [] and len(sig["errors"]) == 1
    assert "校验失败" in sig["errors"][0] and "已删除" in sig["errors"][0]
    assert _leftovers(work_dirs) == [] and not os.path.exists(work_dirs[0])


@pytest.mark.parametrize("content_length, asset_size, expected", [
    (len(PAYLOAD) + 1000, len(PAYLOAD), "不完整"),     # 服务端声称更长、提前断开
    (len(PAYLOAD) - 1000, len(PAYLOAD), "校验失败"),   # 声称更短：只读到声称的长度，摘要兜底
    (None, len(PAYLOAD) + 1, "不完整"),                # 无 Content-Length：按资产元数据核对
])
def test_length_mismatch_reports_and_leaves_nothing(server, work_dirs, qapp,
                                                    content_length, asset_size, expected):
    standard_routes(server, payload(content_length=content_length),
                    release=release_json(size=asset_size))

    _, sig = download(check(server))

    assert sig["finished"] == [] and len(sig["errors"]) == 1
    assert expected in sig["errors"][0]
    assert _leftovers(work_dirs) == []


def test_asset_404_reports_and_leaves_nothing(server, work_dirs, qapp):
    standard_routes(server)
    del server.routes[(ASSET_HOST, ASSET_PATH)]

    _, sig = download(check(server))

    assert sig["finished"] == [] and "404" in sig["errors"][0]
    assert _leftovers(work_dirs) == []


def test_cancel_midway_emits_nothing_and_leaves_nothing(server, work_dirs, qapp):
    """服务端发完第一块就停住；收到第一个进度即取消"""
    standard_routes(server, payload(gate=server.gate))

    def cancel_once(d, _percent):
        d.cancel()
        server.gate.set()

    d, sig = download(check(server), on_progress=cancel_once)

    assert d.is_cancelled
    assert sig == {"finished": [], "errors": [], "failed": [], "result": []}
    assert _leftovers(work_dirs) == [] and not os.path.exists(work_dirs[0])


def test_cancel_after_last_chunk_leaves_nothing(server, work_dirs, qapp, monkeypatch):
    """读完最后一块、核对摘要时才到的取消：结果不交付，安装包也不能留在临时目录"""
    standard_routes(server)
    holder = {}
    real_sha256 = hashlib.sha256

    class _CancelOnDigest:
        def __init__(self):
            self._h = real_sha256()

        def update(self, data):
            self._h.update(data)

        def hexdigest(self):
            holder["d"].cancel()
            return self._h.hexdigest()

    monkeypatch.setattr(update_checker_module.hashlib, "sha256", _CancelOnDigest)
    info = check(server)
    d = UpdateDownloader(info["asset_url"], info["asset_name"], info["asset_size"],
                         digest=info["asset_digest"])
    holder["d"] = d
    signals = []
    for sig in (d.download_finished, d.error_occurred, d.failed, d.result):
        sig.connect(signals.append, Qt.DirectConnection)

    d.run()

    assert signals == []
    assert _leftovers(work_dirs) == [], "取消后安装包不应留在临时目录"


def test_cancel_between_execute_and_delivery_removes_installer(server, work_dirs, qapp):
    """execute 已返回、结果尚未交付时取消：cancel 删除安装包，handle_result 不再交付"""
    standard_routes(server)
    info = check(server)
    d = UpdateDownloader(info["asset_url"], info["asset_name"], info["asset_size"],
                         digest=info["asset_digest"])
    signals = []
    d.download_finished.connect(signals.append, Qt.DirectConnection)
    d.result.connect(signals.append, Qt.DirectConnection)

    path = d.execute(d.token)
    assert os.path.exists(path)
    d.cancel()
    d.handle_result(path)

    assert signals == []
    assert _leftovers(work_dirs) == []


def test_cancel_after_delivery_keeps_installer(server, work_dirs, qapp):
    """结果交付之后再取消（如退出时登记表统一取消）：安装包已归界面，不得删除"""
    standard_routes(server)
    d, sig = download(check(server))
    assert len(sig["finished"]) == 1

    d.cancel()

    assert os.path.exists(sig["finished"][0])


def test_ui_end_to_end_download_then_install(server, work_dirs, qapp, qtbot, monkeypatch):
    """界面侧完整链路（真实后台线程）：开始更新 → 下载校验 → 确认安装 → 启动安装包 → 请求退出"""
    import ui.pages.about_update as about_update

    class _AcceptInstall:
        def __init__(self, parent, version):
            self.version = version

        def exec(self):
            return 1

    standard_routes(server)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(about_update, "InstallConfirmDialog", _AcceptInstall)
    services = make_services()
    group = make_about_update_group(services)
    info = check(server)

    with qtbot.waitSignal(group.install_requested, timeout=10000):
        group._start_update(info)
    qtbot.waitUntil(lambda: group._downloader is None, timeout=5000)

    opened = [c.args["path"] for c in services.dialogs.calls if c.method == "open_file"]
    assert len(opened) == 1
    with open(opened[0], "rb") as f:
        assert f.read() == PAYLOAD
    assert group.status_label.text() == "新版本 v2.0.1 已下载，待安装"
    assert services.notifier.messages("warning") == []


# ================================================================ 1.4.1 冻结副本


def v141_check() -> dict:
    checker = v141.UpdateChecker()
    got, errors = [], []
    checker.check_finished.connect(got.append, Qt.DirectConnection)
    checker.error_occurred.connect(errors.append, Qt.DirectConnection)
    checker.run()
    assert errors == [], errors
    return got[0]


def v141_download(info: dict):
    d = v141.UpdateDownloader(info["asset_url"], info["asset_name"], info["asset_size"])
    finished, errors = [], []
    d.download_finished.connect(finished.append, Qt.DirectConnection)
    d.error_occurred.connect(errors.append, Qt.DirectConnection)
    d.run()
    return finished, errors


def test_v141_upgrades_to_2x_through_the_same_server(server, work_dirs, qapp):
    """1.4.1 用户的真实链路：检查 → 选中唯一的 Setup.exe → 跟随 302 → 落到系统临时目录"""
    standard_routes(server, release=release_json(tag="v2.0.0"))

    info = v141_check()
    assert info["has_update"] is True and info["asset_name"] == SETUP_NAME
    finished, errors = v141_download(info)

    assert errors == []
    assert finished == [os.path.join(str(work_dirs.root), SETUP_NAME)]
    with open(finished[0], "rb") as f:
        assert f.read() == PAYLOAD
    v141_ua = {req[2].get("user-agent") for req in server.requests}
    assert v141_ua == {f"{REPO}/1.4.1"}


def test_v141_installs_tampered_package_without_noticing(server, work_dirs, qapp):
    """发布契约第 6 条：1.4.1 不校验 digest，内容被换掉也照常交给安装"""
    standard_routes(server, payload(PAYLOAD[:-4] + b"evil"), release=release_json(tag="v2.0.0"))

    finished, errors = v141_download(v141_check())

    assert errors == [] and len(finished) == 1


def test_v141_follows_redirect_to_any_domain(server, work_dirs, qapp):
    """1.4.1 没有域名白名单：302 到任何地方都跟随"""
    standard_routes(server, release=release_json(tag="v2.0.0"))
    server.route("github.com", DOWNLOAD_PATH, redirect(f"https://{EVIL_HOST}/{SETUP_NAME}"))
    server.route(EVIL_HOST, f"/{SETUP_NAME}", payload())

    finished, errors = v141_download(v141_check())

    assert errors == [] and len(finished) == 1
    assert EVIL_HOST in server.hosts_requested()


def test_v141_still_checks_content_length(server, work_dirs, qapp):
    """1.4.1 仅有的完整性检查：长度不符时报错并删除文件"""
    standard_routes(server, payload(content_length=len(PAYLOAD) + 1000),
                    release=release_json(tag="v2.0.0"))

    finished, errors = v141_download(v141_check())

    assert finished == [] and "不完整" in errors[0]
    assert _leftovers(work_dirs) == []
