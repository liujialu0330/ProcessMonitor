"""
自动更新模块
通过 GitHub Releases API 检测新版本，下载安装包并调起安装
"""
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

import config

logger = logging.getLogger(__name__)


# GitHub Releases API 地址
GITHUB_API_LATEST = (
    f"https://api.github.com/repos/"
    f"{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases/latest"
)
# 网络超时（秒）
CHECK_TIMEOUT = 8
DOWNLOAD_TIMEOUT = 30
# GitHub API 要求提供 User-Agent
REQUEST_HEADERS = {
    'User-Agent': f'{config.GITHUB_REPO}/{config.APP_VERSION}',
    'Accept': 'application/vnd.github+json',
}

# 安装包下载只允许来自这些域名（含其子域）；GitHub Release 资产会 302 到后两者
ALLOWED_DOWNLOAD_HOSTS = (
    'github.com',
    'objects.githubusercontent.com',
    'release-assets.githubusercontent.com',
)
# 缺少 SHA-256 摘要时的提示：不下载、不安装
MSG_CANNOT_VERIFY = "无法校验安装包，请从发布页手动下载"

_SHA256_DIGEST_RE = re.compile(r'^sha256:([0-9a-fA-F]{64})$')


def is_allowed_download_url(url: str) -> bool:
    """
    下载地址白名单校验：必须是 https，且主机名等于白名单域名或为其子域。
    子域判定要求以 "." 分隔（evilgithub.com、github.com.evil.com 均不通过）；
    使用解析出的 hostname，避免 https://github.com@evil.com/ 之类的 userinfo 欺骗。
    """
    try:
        parsed = urllib.parse.urlparse(url or '')
        host = (parsed.hostname or '').lower().rstrip('.')
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() != 'https' or not host:
        return False
    if port not in (None, 443):
        return False
    return any(host == allowed or host.endswith('.' + allowed)
               for allowed in ALLOWED_DOWNLOAD_HOSTS)


def parse_sha256_digest(digest: Optional[str]) -> Optional[str]:
    """解析 GitHub asset 的 digest 字段（形如 "sha256:<64位hex>"），无效返回 None，结果统一小写"""
    match = _SHA256_DIGEST_RE.match((digest or '').strip())
    return match.group(1).lower() if match else None


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向目标同样必须通过白名单，防止从可信域名被跳转到任意地址"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_allowed_download_url(newurl):
            raise urllib.error.URLError(f"重定向目标不在允许的下载域名内: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_download(request, timeout):
    """打开下载连接（带重定向白名单校验；独立成函数便于测试替换网络层）"""
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(request, timeout=timeout)


def compare_version(a: str, b: str) -> int:
    """
    语义化版本比较，容忍 v/V 前缀，缺段按 0 处理

    Returns:
        int: a < b 返回 -1，a > b 返回 1，相等返回 0
    """
    def parts(v: str):
        v = str(v).strip().lstrip('vV')
        result = []
        for seg in v.split('.'):
            try:
                result.append(int(seg))
            except ValueError:
                # 非数字段（如 1.0.7-beta 的尾段）按 0 处理
                result.append(0)
        return result

    pa, pb = parts(a), parts(b)
    for i in range(max(len(pa), len(pb))):
        na = pa[i] if i < len(pa) else 0
        nb = pb[i] if i < len(pb) else 0
        if na < nb:
            return -1
        if na > nb:
            return 1
    return 0


def parse_release_info(data: dict) -> dict:
    """
    解析 GitHub Release API 返回的 JSON

    Returns:
        dict: {
            'version': 远端版本号（去除 v 前缀）,
            'has_update': 是否比当前版本新,
            'notes': 更新说明,
            'html_url': release 网页地址,
            'asset_url': 安装包下载地址（无则为 None）,
            'asset_name': 安装包文件名（无则为 None）,
            'asset_size': 安装包字节数（无则为 0）,
            'asset_digest': 安装包摘要，形如 "sha256:<hex>"（缺失或格式无效为 None）,
        }
    """
    tag = data.get('tag_name') or data.get('name') or ''
    version = str(tag).strip().lstrip('vV')

    asset_url = None
    asset_name = None
    asset_size = 0
    asset_digest = None
    for asset in data.get('assets') or []:
        name = asset.get('name') or ''
        if 'setup' in name.lower() and name.lower().endswith('.exe'):
            asset_url = asset.get('browser_download_url')
            asset_name = name
            asset_size = asset.get('size') or 0
            digest = asset.get('digest')
            asset_digest = digest if parse_sha256_digest(digest) else None
            break

    return {
        'version': version,
        'has_update': compare_version(version, config.APP_VERSION) > 0,
        'notes': (data.get('body') or '').strip(),
        'html_url': data.get('html_url') or '',
        'asset_url': asset_url,
        'asset_name': asset_name,
        'asset_size': asset_size,
        'asset_digest': asset_digest,
    }


def load_update_prefs() -> dict:
    """读取更新偏好（跳过的版本等），异常时返回空配置"""
    try:
        with open(config.UPDATE_PREFS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass  # 尚无偏好文件属正常情况
    except Exception:
        logger.warning("读取更新偏好失败: %s", config.UPDATE_PREFS_PATH, exc_info=True)
    return {}


def save_update_prefs(prefs: dict):
    """保存更新偏好，异常时静默忽略"""
    try:
        with open(config.UPDATE_PREFS_PATH, 'w', encoding='utf-8') as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("保存更新偏好失败: %s", config.UPDATE_PREFS_PATH, exc_info=True)


def get_skipped_version() -> str:
    """获取用户选择跳过的版本号"""
    return str(load_update_prefs().get('skipped_version') or '')


def set_skipped_version(version: str):
    """记录用户选择跳过的版本号"""
    prefs = load_update_prefs()
    prefs['skipped_version'] = version
    save_update_prefs(prefs)


class UpdateChecker(QThread):
    """检查更新的后台线程"""

    # 检查完成信号，携带 parse_release_info 的结果
    check_finished = pyqtSignal(dict)
    # 检查失败信号，携带错误描述
    error_occurred = pyqtSignal(str)

    def run(self):
        try:
            request = urllib.request.Request(
                GITHUB_API_LATEST, headers=REQUEST_HEADERS
            )
            with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            self.check_finished.emit(parse_release_info(data))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 仓库尚无任何 release
                self.error_occurred.emit("仓库暂无发布版本")
            else:
                logger.error("检查更新失败（HTTP %s）", e.code, exc_info=True)
                self.error_occurred.emit(f"检查更新失败（HTTP {e.code}）")
        except Exception as e:
            logger.error("检查更新失败", exc_info=True)
            self.error_occurred.emit(f"检查更新失败：{e}")


class UpdateDownloader(QThread):
    """
    下载安装包的后台线程。

    安全约束：下载地址必须通过 https + 域名白名单；必须提供 asset 的 SHA-256
    摘要（digest），下载完成后逐字节校验，不一致视为失败并删除文件；缺少摘要时
    不下载，经 error_occurred 提示用户去发布页手动下载。文件落在 mkdtemp 生成
    的唯一目录内。
    """

    # 下载进度信号（0-100；总大小未知时报 -1）
    download_progress = pyqtSignal(int)
    # 下载完成信号，携带本地安装包路径
    download_finished = pyqtSignal(str)
    # 下载失败信号
    error_occurred = pyqtSignal(str)

    def __init__(self, url: str, filename: str, asset_size: int = 0, parent=None,
                 digest: Optional[str] = None):
        """
        Args:
            url: 安装包下载地址
            filename: 保存的文件名
            asset_size: release 资产元数据里的字节数，Content-Length 缺失时的兜底
            parent: 父对象
            digest: asset 的摘要字符串，形如 "sha256:<hex>"（parse_release_info 的
                    asset_digest）；缺失或格式无效则拒绝下载
        """
        super().__init__(parent)
        self.url = url
        self.filename = filename
        self.asset_size = asset_size
        self.digest = digest
        self._cancelled = False

    def cancel(self):
        """请求取消下载"""
        self._cancelled = True

    def run(self):
        if not is_allowed_download_url(self.url):
            logger.error("拒绝下载：地址不在允许范围内: %s", self.url)
            self.error_occurred.emit("安装包下载地址不受信任，请从发布页手动下载")
            return

        expected_sha256 = parse_sha256_digest(self.digest)
        if expected_sha256 is None:
            logger.error("拒绝下载：release 资产缺少有效的 SHA-256 摘要: %s", self.url)
            self.error_occurred.emit(MSG_CANNOT_VERIFY)
            return

        work_dir = tempfile.mkdtemp(prefix='ProcessMonitor_update_')
        save_path = os.path.join(work_dir, os.path.basename(self.filename) or 'setup.exe')
        try:
            request = urllib.request.Request(self.url, headers=REQUEST_HEADERS)
            sha256 = hashlib.sha256()
            with _open_download(request, DOWNLOAD_TIMEOUT) as resp:
                total = int(resp.headers.get('Content-Length') or 0)
                downloaded = 0
                last_percent = -1
                with open(save_path, 'wb') as f:
                    while True:
                        if self._cancelled:
                            raise InterruptedError("用户取消下载")
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        sha256.update(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            percent = downloaded * 100 // total
                            if percent != last_percent:
                                last_percent = percent
                                self.download_progress.emit(percent)
                        else:
                            self.download_progress.emit(-1)

            # 完整性校验：优先用响应头 Content-Length，缺失（total<=0）时退回 release 资产
            # 元数据里的 asset_size；两者都拿不到时跳过长度检查（由 SHA-256 兜底）
            expected = total if total > 0 else self.asset_size
            if expected > 0 and downloaded != expected:
                logger.error("下载不完整: 已下载=%d 预期=%d url=%s",
                             downloaded, expected, self.url)
                self._cleanup(work_dir)
                self.error_occurred.emit(
                    f"下载不完整：已下载 {downloaded} 字节，预期 {expected} 字节，请重试")
                return

            if sha256.hexdigest() != expected_sha256:
                logger.error("安装包 SHA-256 校验失败: 实际=%s 预期=%s url=%s",
                             sha256.hexdigest(), expected_sha256, self.url)
                self._cleanup(work_dir)
                self.error_occurred.emit(
                    "安装包校验失败（SHA-256 不一致），已删除下载文件，请重试或从发布页手动下载")
                return

            self.download_finished.emit(save_path)
        except InterruptedError:
            self._cleanup(work_dir)
        except Exception as e:
            logger.error("下载更新失败", exc_info=True)
            self._cleanup(work_dir)
            self.error_occurred.emit(f"下载更新失败：{e}")

    @staticmethod
    def _cleanup(work_dir: str):
        """删除下载残留（整个唯一临时目录，其中只有本次下载的文件）"""
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            logger.error("清理下载残留失败: %s", work_dir, exc_info=True)


if __name__ == "__main__":
    # 简单自测：版本比较与真实 API 检查
    assert compare_version("1.0.6", "1.0.6") == 0
    assert compare_version("1.0.7", "1.0.6") == 1
    assert compare_version("v1.1.0", "1.0.6") == 1
    assert compare_version("1.0", "1.0.0") == 0
    assert compare_version("0.9.9", "1.0.6") == -1
    print("版本比较自测通过")

    import sys
    from PyQt5.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)
    checker = UpdateChecker()
    checker.check_finished.connect(
        lambda info: (print(f"检查结果: {info}"), app.quit()))
    checker.error_occurred.connect(
        lambda msg: (print(f"检查失败: {msg}"), app.quit()))
    checker.start()
    sys.exit(app.exec_())
