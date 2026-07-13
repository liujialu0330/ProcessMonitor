"""应用级排版 token 契约。"""
from qfluentwidgets import fontFamilies, setFontFamilies

from ui.typography import (
    DATA_FONT_FAMILIES,
    UI_FONT_FAMILIES,
    PageTitleLabel,
    TypeScale,
    configure_application_typography,
    data_font,
)


def test_typography_uses_chinese_ui_and_data_font_roles(qapp):
    """Fluent、原生 Qt 与遥测数字必须使用明确且不同的字体角色。"""
    previous_families = fontFamilies()
    previous_app_font = qapp.font()
    try:
        configure_application_typography(qapp)

        assert fontFamilies() == list(UI_FONT_FAMILIES)
        assert qapp.font().families() == list(UI_FONT_FAMILIES)
        assert data_font().families() == list(DATA_FONT_FAMILIES)

        title = PageTitleLabel("历史数据")
        assert title.font().pixelSize() == TypeScale.PAGE_TITLE
        title.close()
    finally:
        setFontFamilies(previous_families, save=False)
        qapp.setFont(previous_app_font)
