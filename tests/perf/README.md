# 性能基准套件（tests/perf/）

固化"现状基线"：2.0 后续阶段（存储 v5、查询改写、系统快照采集）用同一套基准证明改进。
全部用例标 `@pytest.mark.perf`，`pytest.ini` 默认排除（`-m "not perf and not soak"`），
平时的 `pytest` / CI 不会跑到。

## 怎么跑

```powershell
$env:QT_QPA_PLATFORM='offscreen'; $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_FONTDIR='C:\Windows\Fonts'
python -m pytest tests/perf -m perf -v
```

- 命令行的 `-m perf` 覆盖 `pytest.ini` 里的默认排除。
- 主要场景重复 3 次取中位数，与预算比较；会话结束在终端打印"实测 / 预算 / 系数"汇总。
  每次运行都有独立的 `tests/perf/.cache/runs/<run_id>/result.json`，
  `last_run.json` 只指向最近一次记录；CI 同时上传两者，失败结果也保留。
- 首次运行会生成合成库，之后复用；耗时取决于机器、场景和缓存状态，以本次独立记录为准。
- CI：`.github/workflows/perf.yml` 每周一次 + 手动触发（`workflow_dispatch`）。

## 预算与机器系数

`budgets.json`：

| 字段 | 含义 |
|---|---|
| `calibration_ms` | 基线机器（回填预算的那台）的校准值 |
| `slack` | 回填倍数，阶段 0 为 1.5 |
| `budgets.<名称>.budget` | 预算 = 基线机器实测 × `slack` |
| `budgets.<名称>.measured` | 回填时的实测值（仅供参考，不参与比较） |

`calibrate.py` 是一段固定工作量（20 万次字典/列表操作 + 内存 SQLite 5 万行带索引插入与
聚合），取 5 次中位数为本机校准值（可 `python tests/perf/calibrate.py` 单独查看）。运行时：

```
系数 = 本机校准值 / calibration_ms
本机预算 = budget × 系数
```

慢机器系数大于 1，预算随之放宽。需要临时手工指定系数时设环境变量
`PERF_MACHINE_FACTOR`（如 `2.0`），此时不再按校准值换算。系数必须是有限正数，
结果中会保留实际值。本轮工程修复不调整现有预算或场景目标。

五任务采集和首次进程列表（快照路径）分别使用既有场景目标 `15 ms/s × 系数`、`100 ms × 系数`；
其余断言使用 `budgets.json`。JSON 中 `limit`（兼容字段 `budget`）就是本次断言实际使用的值，
`limit_source` 标明来源，`baseline_budget` 另存历史基线。快照子项只用于观察，状态为
`observed`，不显示通过或失败；回填模式为 `recorded`。原始实测和阈值均保留精度，
状态直接取同一比较结果；写入 p50 失败后仍会记录和检查 p95。

## 回填预算

```powershell
python -m pytest tests/perf -m perf --perf-update-budgets
```

- 只在显式要求时写文件；也可用环境变量 `PERF_UPDATE_BUDGETS=1`（从 `tests/` 起跑、
  命令行选项未注册时用它）。
- 回填模式不做比较；会话结束把跑到的条目改为"实测 × 1.5"，`calibration_ms` 换成本机校准值，
  没跑到的条目按"新校准值 / 旧校准值"同步换算，保持口径一致。
- 后续阶段收紧预算时直接改 `budget` 或调小 `slack` 后回填。校准工作量（`calibrate.py`
  的参数）一旦改动，必须在基线机器上重新回填全部条目。

## 合成库

`synth.py` 经仓储接口（`db.tasks.save` / `db.points.append` / `db.markers.add`）写入当前
版本库，不手写 SQL，存储结构升级后生成器仍可用；`points.append` 本身是批量事务，按
1 万个周期一批调用。缓存放 `tests/perf/.cache/`（已加入 `.gitignore`），文件名含场景名、
参数摘要（含生成器版本 `SYNTH_VERSION`）与 `SCHEMA_VERSION`，任一变化自动重建。

同一工作区的正式测量通过跨进程锁串行执行，避免两套测试互相争用资源；同参数缓存生成
另有独立锁，等待后重新检查完整缓存。同场景不同参数的旧缓存会保留，避免删除其他进程正在
使用的数据库；需要清理时请等全部性能作业结束。结果使用唯一临时文件和原子替换，锁由操作
系统持有，进程退出即释放；`.locks` 中的空锁文件保留，不代表仍有进程占用。

| 场景 | 内容 |
|---|---|
| `3d` / `7d` | 单任务 5 指标（`memory_private, num_handles, num_gdi_objects, num_user_objects, num_threads`），1 秒周期；每 3 小时一个 2 分钟空洞；基线（开始后 30 分钟）+ 6 轮打点；数值缓慢上涨 + 噪声。约 128 万 / 299 万行 |
| `write100k` | 同上指标，2 万个周期 = 10 万行，无空洞无打点（写入延迟的底库） |
| `v1` | 1.4.1 的 v1 旧库（手工建表，含 v3 会删除的两个旧索引），4 个任务共 100 万行，指标含 `memory_vms` 以走合并去重路径 |

## 用例

| 名称 | 文件 | 测什么 |
|---|---|---|
| `analysis_all_3d` / `analysis_all_7d` | `test_perf_analysis.py` | 分析页"全部"整批：5 个指标各一次 `run_history_query(build_request(..., 'all', analysis=True))`，`ThreadPoolExecutor(5)` 并行（与 `AnalysisViewModel` 每指标一个后台查询相同），锚点不预缓存 |
| `analysis_1h_3d` | 同上 | 自动刷新路径："最近 1 小时"范围整批（3 天库） |
| `write_cycle_p50` / `write_cycle_p95` | `test_perf_storage.py` | 10 万行库上连续 200 个周期，每周期 5 个指标共用一个时间戳，经 `db.save_data_points`（采集线程的落库写法，内部为 `points.append`）写入，取 p50 / p95 |
| `migration_v1_1m` | 同上 | `MigrationRunner` 把 100 万行 v1 库迁到当前版本，含迁移前文件级备份 |
| `collect_leak_preset_us` | `test_perf_collect.py` | `ProcessCollector.collect_metrics` 泄漏预设 5 项，对测试进程本身，200 次中位数（微秒） |
| `collect_5_tasks_ms_per_s` | 同上 | 五任务泄漏预设采集，每秒总开销；拆分系统快照与进程指标子项用于观察 |
| `list_processes_cold_snapshot` | 同上 | 新子进程里经快照首次列出进程的耗时，含快照器自检 |
| `list_processes_cold` / `list_processes_hot` | 同上 | `core.process_catalog.list_processes()`：新子进程里第一次调用（冷）与紧接着的第二次（热） |
| `import_main_window_cold` | 同上 | 新子进程里 `import ui.main_window` 的耗时（不含解释器启动） |

子进程测量的数据目录指向临时目录；所有用例都不碰项目根 `data\`。

## 运行证据与解释边界

`result.json` 包含源码与预算 SHA-256、Python 与计时器信息、机器系数、数据生成参数/种子/
时区身份/缓存大小，以及每个测试阶段开始和结束时的墙钟、进程 CPU 与 IO 累计值、内存、
线程数、优先级、亲和性和系统累计负载。Windows 额外记录接电/电池/节电状态、显式
Power Throttling 配置与当次所在逻辑处理器。读取失败会记录错误，不改变系统设置。

这些快照在计时函数外采集，阶段总耗时还包含准备与收尾，不能替代具体场景的实测值。
快照也无法证明两次采样之间是否锁屏、睡眠、发生电源切换或持续运行在哪个核心；显式
Power Throttling 掩码为零表示由系统管理，不能据此断言有效 QoS 等级。出现波动时，应先
比较同源码、同参数的独立运行记录，再决定是否需要更细的专项追踪，不凭一次慢结果归因。

## 相关：加速浸泡

`tests/test_soak_accelerated.py`（`@pytest.mark.soak`，同样默认排除）用假时钟 + 假采集器
模拟 7 天运行并检查汇总/空洞/计数不变式：

```powershell
python -m pytest tests/test_soak_accelerated.py -m soak -s
```

缓存身份同时记录固定本地起点对应的 UTC 毫秒、覆盖日期的 UTC 映射与时区名；改变系统时区后会生成新缓存，避免把顺序追加误测为乱序写入。性能预算不会因此自动调整。
