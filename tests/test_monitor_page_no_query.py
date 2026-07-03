"""
监控页周期路径无查库验证（v1.2.0 批3）
TaskCard 计数改为内存计数（卡片建时置0，_on_data_updated 内+1），彻底删除
每采集周期调用 Database.get_sample_count 的查库路径。用源码静态检查（等价于
grep 调用点）钉死这条回归线：_on_data_updated 是 data_updated 信号槽，每采集
周期触发一次，一旦有人重新引入 get_sample_count 调用，本用例即失败。
"""
import inspect

from ui.pages.monitor_page import MonitorPage, TaskCard


def test_on_data_updated_does_not_call_get_sample_count():
    """data_updated 槽函数源码中不应再出现 get_sample_count 调用"""
    source = inspect.getsource(MonitorPage._on_data_updated)
    assert "get_sample_count" not in source


def test_task_card_count_starts_at_zero_without_db_query():
    """TaskCard 计数初值直接置0，不查库（构造函数不应调用 get_sample_count；
    允许注释里提及该方法名做说明，故只断言不存在"调用"形式）"""
    init_source = inspect.getsource(TaskCard.__init__)
    ui_source = inspect.getsource(TaskCard._init_ui)
    assert "get_sample_count(" not in init_source
    assert "get_sample_count(" not in ui_source
    assert "self.count = 0" in init_source


def test_task_card_increment_count_updates_in_memory_only():
    """increment_count 只做内存自增+文案刷新，不接触数据库"""
    source = inspect.getsource(TaskCard.increment_count)
    assert "get_sample_count" not in source
    assert "self.db" not in source
