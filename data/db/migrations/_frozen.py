"""
迁移专用的辅助函数冻结副本

迁移代码描述的是"某个历史版本的数据长什么样"，不能随运行时代码演进而改变行为，
所以这里的函数一经发布不再修改；运行时的同名逻辑另行维护。
"""
import json
from typing import List


def parse_metric_types(text: str) -> List[str]:
    """
    解析 tasks.metric_type 列内容为指标列表

    - JSON 数组文本 → 元素转字符串后的列表；空数组 '[]' → []
    - 旧版裸指标名、或以 '[' 开头但不是合法 JSON 数组的文本 → [原文本]
    """
    if text and text.startswith('['):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(m) for m in parsed]
        except (json.JSONDecodeError, TypeError):
            pass  # 解析失败回退为单值
    return [text]
