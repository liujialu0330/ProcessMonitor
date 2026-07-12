"""
AboutPage 静默更新提醒信号验证（v1.3.0 批4，C5，修复遗留 P2-2）

覆盖方案 §4.4：静默检查（启动3秒后自动检查）发现新版本时应 emit
update_available_silent 信号、不再直接弹模态对话框；手动检查（用户点击"检查
更新"按钮）路径保持不变，仍直接弹模态对话框。独立构造 AboutPage（不经
MainWindow），mock 掉 _show_update_dialog 断言是否被调用，避免真的弹出
QDialog 阻塞测试。
"""
from ui.pages.about_page import AboutPage


def _make_update_info(version="9.9.9") -> dict:
    """构造一个"发现新版本"的 info 字典，形态对齐
    core.update_checker.parse_release_info 的返回值"""
    return {
        "version": version,
        "has_update": True,
        "notes": "",
        "html_url": "",
        "asset_url": None,
        "asset_name": None,
        "asset_size": 0,
    }


def test_silent_update_emits_signal_not_dialog(qapp, monkeypatch):
    """静默检查（silent=True）发现新版本时应 emit update_available_silent
    信号，不应直接弹出模态对话框"""
    page = AboutPage()

    # 版本跳过偏好读取真实磁盘文件（data/update_prefs.json），用不存在的版本号
    # 兜底之外，显式 monkeypatch 到恒返回空字符串，避免测试结果依赖开发机上
    # 真实的历史"跳过版本"记录
    monkeypatch.setattr('ui.pages.about_page.get_skipped_version', lambda: '')

    received = []
    page.update_available_silent.connect(lambda info: received.append(info))

    dialog_calls = []
    monkeypatch.setattr(page, '_show_update_dialog', lambda info: dialog_calls.append(info))

    info = _make_update_info()
    page._on_check_finished(info, True)

    assert received == [info]
    assert dialog_calls == []


def test_manual_update_still_shows_dialog(qapp, monkeypatch):
    """手动检查（silent=False）发现新版本时行为不变：直接弹模态对话框，不
    emit update_available_silent 信号"""
    page = AboutPage()
    monkeypatch.setattr('ui.pages.about_page.get_skipped_version', lambda: '')

    received = []
    page.update_available_silent.connect(lambda info: received.append(info))

    dialog_calls = []
    monkeypatch.setattr(page, '_show_update_dialog', lambda info: dialog_calls.append(info))

    info = _make_update_info()
    page._on_check_finished(info, False)

    assert dialog_calls == [info]
    assert received == []


def test_show_update_dialog_for_delegates_to_show_update_dialog(qapp, monkeypatch):
    """show_update_dialog_for 是主窗口 InfoBar「查看」按钮的回调入口，应直接
    复用 _show_update_dialog，不重复实现弹窗逻辑"""
    page = AboutPage()
    dialog_calls = []
    monkeypatch.setattr(page, '_show_update_dialog', lambda info: dialog_calls.append(info))

    info = _make_update_info()
    page.show_update_dialog_for(info)

    assert dialog_calls == [info]
