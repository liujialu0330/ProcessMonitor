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
- 每个用例重复 3 次取中位数，与预算比较；会话结束在终端打印"实测 / 预算 / 系数"汇总，
  本次实测写入 `tests/perf/.cache/last_run.json`（CI 上传为 artifact）。
- 首次运行会生成合成库（本机约 25 秒，缓存约 710 MB），之后复用；整套约 75 秒（已缓存）。
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
`PERF_MACHINE_FACTOR`（如 `2.0`），此时不再按校准值换算。

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
| `list_processes_cold` / `list_processes_hot` | 同上 | `core.process_catalog.list_processes()`：新子进程里第一次调用（冷）与紧接着的第二次（热） |
| `import_main_window_cold` | 同上 | 新子进程里 `import ui.main_window` 的耗时（不含解释器启动） |

子进程测量的数据目录指向临时目录；所有用例都不碰项目根 `data\`。

## 相关：加速浸泡

`tests/test_soak_accelerated.py`（`@pytest.mark.soak`，同样默认排除）用假时钟 + 假采集器
模拟 7 天运行并检查汇总/空洞/计数不变式：

```powershell
python -m pytest tests/test_soak_accelerated.py -m soak -s
```
