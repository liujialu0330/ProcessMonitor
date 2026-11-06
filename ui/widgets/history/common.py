"""
分析页控件共用的版式常量。

响应式阈值原先按页面视口宽度判断；控件改为按自身宽度判断后，要扣掉页面左右
边距与卡片左右内边距，才能与原来的换行/换列时机保持一致。
"""

# 页面左右边距（PageScaffold 约定的 24px）
PAGE_MARGIN = 24
# 卡片左右内边距（设计规范：卡片内边距 16）
CARD_PADDING_H = 16
# 原"视口宽度"与现"卡片内容宽度"之差：视口 = 内容宽度 + 2×页边距 + 2×卡片内边距
VIEWPORT_TO_CONTENT_OFFSET = 2 * PAGE_MARGIN + 2 * CARD_PADDING_H

# 明细与悬停读数的时间格式：含月日，避免跨天数据歧义
DETAIL_TIME_FORMAT = '%m-%d %H:%M:%S'


def content_width_of(widget) -> int:
    """控件宽度减去其布局左右边距，即卡片内容区宽度。"""
    layout = widget.layout()
    if layout is None:
        return widget.width()
    margins = layout.contentsMargins()
    return widget.width() - margins.left() - margins.right()
