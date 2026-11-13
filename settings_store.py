"""后台可独立使用的配置读写；所有值均为普通 Python/JSON 数据。"""
import copy
import json
import logging
import os
import tempfile
from typing import Callable, Optional

from settings_schema import (
    CONFIG_VERSION_KEY, CURRENT_CONFIG_VERSION, SETTINGS, config_version_of,
    migrate_config, read_values, serialize_values,
)

logger = logging.getLogger(__name__)


def write_json_atomic(path: str, data: dict) -> None:
    """完成序列化后再替换原文件，失败时不留下半份配置。"""
    text = json.dumps(data, ensure_ascii=False, indent=4)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + '.',
                                     suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def prepare_config_file(config_path: str, *, log=None,
                        migrate: Callable = migrate_config,
                        current_version: int = CURRENT_CONFIG_VERSION) -> tuple:
    """返回 (有效原始字典, 是否首启)，只对旧版本做迁移回写。

    损坏文件保留到下一次明确保存；未来版本按已知字段读取，不能降版本或抹去未知键。
    """
    log = logger if log is None else log
    if not os.path.exists(config_path):
        return {}, True
    try:
        with open(config_path, encoding='utf-8') as stream:
            raw = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        log.warning('读取配置文件失败，按默认配置加载: %s', config_path, exc_info=True)
        return {}, False
    if not isinstance(raw, dict):
        return {}, False
    version = config_version_of(raw)
    if version > current_version:
        log.warning('配置文件版本 %d 高于当前支持的 %d，不改写文件，按能识别的键读取: %s',
                    version, current_version, config_path)
        return raw, False
    migrated, steps = migrate(raw)
    if steps:
        try:
            write_json_atomic(config_path, migrated)
        except OSError:
            log.warning('回写迁移后的配置文件失败: %s', config_path, exc_info=True)
            return raw, False
        log.info('配置文件已迁移到版本 %d（%s）: %s',
                 current_version, '，'.join(steps), config_path)
    return migrated, False


def import_legacy_preferences(prefs_path: str, *, current: Callable[[], str],
                              save: Callable[[str], None], log=None) -> None:
    """导入旧更新偏好；新值没有成功保存时保留唯一的旧文件供下次重试。"""
    log = logger if log is None else log
    if not os.path.exists(prefs_path):
        return
    legacy = ''
    try:
        with open(prefs_path, encoding='utf-8') as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            legacy = str(data.get('skipped_version') or '')
    except OSError:
        # 无法读取不等于内容无效；保留唯一来源，待占用或权限问题解除后重试。
        log.warning('读取旧版更新偏好失败，保留旧文件: %s', prefs_path, exc_info=True)
        return
    except (ValueError, UnicodeError):
        log.warning('读取旧版更新偏好失败，按未跳过版本处理: %s', prefs_path, exc_info=True)
    if legacy and not current():
        try:
            save(legacy)
        except (OSError, TypeError, ValueError):
            log.warning('保存导入的更新偏好失败，保留旧文件: %s', prefs_path, exc_info=True)
            return
        log.info('已从旧版更新偏好导入跳过的版本: %s', legacy)
    try:
        os.remove(prefs_path)
    except OSError:
        log.warning('删除旧版更新偏好文件失败: %s', prefs_path, exc_info=True)


class RuntimeSettings:
    """独立配置值快照；不依赖模块全局单例、Qt 信号或界面状态。"""

    def __init__(self, raw: Optional[dict] = None, config_path: Optional[str] = None):
        self._raw = copy.deepcopy(raw or {})
        self._values = read_values(self._raw)
        self.config_path = config_path

    def get(self, name: str):
        """返回副本，调用方改动列表/字典不会悄悄改掉后续命令的默认配置。"""
        return copy.deepcopy(self._values[name])

    def snapshot(self) -> dict:
        """完整 JSON 形式，含保留的主题与未来字段；调用不读写配置文件。"""
        return serialize_values(self._values, self._raw)

    def save(self) -> None:
        if self.config_path is None:
            raise ValueError('配置尚未关联文件，不能保存')
        write_json_atomic(self.config_path, self.snapshot())

    def set(self, name: str, value, *, save: bool = True) -> bool:
        corrected = SETTINGS[name].correct(value)
        if self._values[name] == corrected:
            return False
        previous = self._values[name]
        self._values[name] = corrected
        if save:
            try:
                self.save()
            except (OSError, TypeError, ValueError):
                self._values[name] = previous
                raise
        return True


def load_settings(config_path: str, prefs_path: str) -> RuntimeSettings:
    """后台入口：与 GUI 共用迁移/校验定义，首次配置仍保持跟随系统主题。"""
    raw, first_launch = prepare_config_file(config_path)
    if first_launch:
        raw = {CONFIG_VERSION_KEY: CURRENT_CONFIG_VERSION,
               'QFluentWidgets': {'ThemeMode': 'Auto'}}
    settings = RuntimeSettings(raw, config_path)
    if first_launch:
        settings.save()
    import_legacy_preferences(
        prefs_path, current=lambda: str(settings.get('skipped_version') or ''),
        save=lambda value: settings.set('skipped_version', value))
    return settings
