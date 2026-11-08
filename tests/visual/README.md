# 离屏双主题截图夹具

## 用途

界面改动（尤其是深色主题的配色、卡片底色、胶囊与徽标取色）单靠断言很难发现问题，需要人看图。
本目录提供一个可复用的夹具：在离屏（`offscreen`）平台下，把同一个控件分别在**浅色**与**深色**主题下
各新建一次、放进带 Fluent 窗口底色的容器（浅 `#f3f3f3` / 深 `#202020`）、按指定尺寸截图，
得到 `{名称}_light.png` 与 `{名称}_dark.png` 两张图。

- 每种主题都**重新构造控件**：同一控件切换主题会残留旧样式，截出来的图不可信。
- 主题只在内存里切换（`qconfig._theme = Theme.X` + `qconfig.themeChanged.emit(Theme.X)`），
  不写配置文件，结束后恢复原主题。
- 主窗口在 Windows 11 上启用 Mica（自身背景透明），离屏时没有系统合成的底色；容器垫一层窗口底色，
  深色截图才不会出现“深色控件 + 白底”的假象。
- 数据目录：根 `tests/conftest.py` 的自动夹具已把 `PROCESS_MONITOR_DATA_DIR` 指向每个用例的临时目录，
  `visual_data_dir` 夹具再核对一次，截图用例绝不碰开发机的 `data\` 与 `%LOCALAPPDATA%` 下的真实数据。

## 怎么跑

截图用例默认跳过（慢、产物要人看），显式加 `--visual`：

```powershell
$env:QT_QPA_PLATFORM='offscreen'; $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_FONTDIR='C:\Windows\Fonts'
python -m pytest tests/visual --visual
# 指定输出目录
python -m pytest tests/visual --visual --visual-out D:\tmp\shots
```

`--visual` / `--visual-out` 由 `tests/visual/conftest.py` 注册，必须在命令行里直接给出 `tests/visual`
（pytest 只在解析参数前加载命令行路径上的 conftest）。全量运行 `python -m pytest` 时本目录的用例显示为跳过。
需要 `QT_QPA_FONTDIR` 指向系统字体目录，否则离屏渲染的中文是方块。

## 输出位置

默认 `tests/visual/.out/`（已加入 `.gitignore`，不提交），可用 `--visual-out` 覆盖。现有用例输出：

| 文件 | 内容 |
|------|------|
| `monitor_light.png` / `monitor_dark.png` | 主窗口 · 监控页（空任务引导、新建表单） |
| `analysis_light.png` / `analysis_dark.png` | 主窗口 · 分析页（一条已结束任务：基线 + 2 轮打点，3 个指标共 300 点） |
| `setting_light.png` / `setting_dark.png` | 主窗口 · 设置页（快捷键为假后端“已启用”） |
| `e1_{monitor,analysis,analysis_group,analysis_member,analysis_full}_{800,1000,1280,1920}_{light,dark}.png` | 响应式布局四档宽度（800 宽高 700，其余高 820；`full` 为拉高到 2000 的整页长图，小倍数图全部展开）：监控页、分析页（普通任务、进程组任务及其成员视图），见 `test_responsive_layout.py` |

均为 1280×820。用例断言：PNG 存在、尺寸正确、浅深两张像素差异 > 1%、深色整图平均亮度 < 100、浅色 > 170。
画得好不好（对比度、裁切、重叠）仍需人看。

## 后续阶段怎么复用

在 `tests/visual/` 下新建 `test_*.py`，使用 `visual_snapshot` 夹具：

```python
def test_summary_table_dual_theme(visual_snapshot):
    def build():
        table = SummaryTable()
        table.set_rows(rows, basis, focus="句柄数")
        return table                      # 每种主题调用一次，返回新控件

    light, dark = visual_snapshot(build, "summary_table", (1130, 300))
    assert pixel_difference_ratio(light, dark) > 0.01
```

- `build`：无参工厂，返回一个**新**控件（可以是 `MainWindow`）；控件随容器一起释放，调用方不要再持有或释放它。
- `settle`（可选关键字）：显示后、截图前调用，参数为控件，用于等待异步数据（如分析页的后台查询、
  监控页的进程列表），见 `test_pages_dual_theme.py` 的 `_settle_analysis`。
- 不经夹具时可直接调用 `tests.visual.snapshot.capture_dual_theme(build, name, size, out_dir, settle=)`；
  另有 `pixel_difference_ratio`、`mean_luminance`、`theme_applied`（上下文管理器）可用。
- 需要整窗装配时照 `test_pages_dual_theme.py`：`app.bootstrap.build_context(paths)` 组装，只替换进程查询、
  对话框与快捷键后端（**测试绝不注册真实热键**），关掉主窗口 3 秒后的联网检查更新。
