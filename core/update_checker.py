"""
自动更新模块
通过 GitHub Releases API 检测新版本，下载安装包并调起安装
"""
import json
import logging
import os
import tempfile
import urllib.request
import urllib.error
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
        }
    """
    tag = data.get('tag_name') or data.get('name') or ''
    version = str(tag).strip().lstrip('vV')

    asset_url = None
    asset_name = None
    asset_size = 0
    for asset in data.get('assets') or []:
        name = asset.get('name') or ''
        if 'setup' in name.lower() and name.lower().endswith('.exe'):
            asset_url = asset.get('browser_download_url')
            asset_name = name
            asset_size = asset.get('size') or 0
            break

    return {
        'version': version,
        'has_update': compare_version(version, config.APP_VERSION) > 0,
        'notes': (data.get('body') or '').strip(),
        'html_url': data.get('html_url') or '',
        'asset_url': asset_url,
        'asset_name': asset_name,
        'asset_size': asset_size,
    }


def load_update_prefs() -> dict:
    """读取更新偏好（跳过的版本等），异常时返回空配置"""
    try:
        with open(config.UPDATE_PREFS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_update_prefs(prefs: dict):
    """保存更新偏好，异常时静默忽略"""
    try:
        with open(config.UPDATE_PREFS_PATH, 'w', encoding='utf-8') as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
    """下载安装包的后台线程"""

    # 下载进度信号（0-100；总大小未知时报 -1）
    download_progress = pyqtSignal(int)
    # 下载完成信号，携带本地安装包路径
    download_finished = pyqtSignal(str)
    # 下载失败信号
    error_occurred = pyqtSignal(str)

    def __init__(self, url: str, filename: str, asset_size: int = 0, parent=None):
        super().__init__(parent)
        self.url = url
        self.filename = filename
        self.asset_size = asset_size  # release 资产元数据里的字节数，Content-Length 缺失时的兜底
        self._cancelled = False

    def cancel(self):
        """请求取消下载"""
        self._cancelled = True

    def run(self):
        save_path = os.path.join(tempfile.gettempdir(), self.filename)
        try:
            request = urllib.request.Request(self.url, headers=REQUEST_HEADERS)
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as resp:
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
                        downloaded += len(chunk)
                        if total > 0:
                            percent = downloaded * 100 // total
                            if percent != last_percent:
                                last_percent = percent
                                self.download_progress.emit(percent)
                        else:
                            self.download_progress.emit(-1)

            # 完整性校验：优先用响应头 Content-Length，缺失（total<=0）时退回 release 资产
            # 元数据里的 asset_size；两者都拿不到时无法校验，视为通过（避免误判）
            expected = total if total > 0 else self.asset_size
            if expected > 0 and downloaded != expected:
                logger.error("下载不完整: 已下载=%d 预期=%d url=%s",
                              downloaded, expected, self.url)
                self._cleanup(save_path)
                self.error_occurred.emit(
                    f"下载不完整：已下载 {downloaded} 字节，预期 {expected} 字节，请重试")
                return

            self.download_finished.emit(save_path)
        except InterruptedError:
            self._cleanup(save_path)
        except Exception as e:
            logger.error("下载更新失败", exc_info=True)
            self._cleanup(save_path)
            self.error_occurred.emit(f"下载更新失败：{e}")

    @staticmethod
    def _cleanup(path: str):
        """删除下载残留的不完整文件"""
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


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
