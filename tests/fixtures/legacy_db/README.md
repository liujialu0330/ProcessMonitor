# 真实旧库样本（golden 库）

用各已发布版本**自己的** `data.database.Database` 按固定场景写出的真实数据库，gzip 压缩后入库。
`tests/test_migration_golden.py` 只读本目录的 `manifest.json` 与 `*.db.gz`，把每份样本迁到当前
`data.db.schema.SCHEMA_VERSION` 并核对结构、计数、启动校正、历史查询与幂等；生成器只在手动运行时执行。

## 样本清单

| 文件 | 来源 | user_version | 日志模式 | 说明 |
|---|---|---|---|---|
| `v1.0.6_v0.db.gz` | tag v1.0.6 | 0 | delete | 无 `user_version`、`data_points` 无 `metric_type` 列、一任务一指标 |
| `v1.1.0_v1.db.gz` | tag v1.1.0 | 1 | delete | 首个 v1：`tasks.metric_type` 为 JSON 数组、`data_points.metric_type` 列 |
| `v1.2.0_v1.db.gz` | tag v1.2.0 | 1 | wal | 表结构同 v1.1.0；起用 WAL（文件头不同） |
| `v1.4.1_v1.db.gz` | tag v1.4.1 | 1 | wal | 最后一个已发布版本（v1.3.0 / v1.4.0 的 `database.py` 与之相同） |
| `derived_v2_from_v1.4.1.db.gz` | v1.4.1 样本 + 当前冻结迁移链只迁到 v2 | 2 | wal | 从未发布，供覆盖链中段 |
| `derived_v3_from_v1.4.1.db.gz` | v1.4.1 样本 + 迁到 v3，再按 v3 结构补写 | 3 | wal | 从未发布；补了任务 A 的中文备注、带 3 个打点的任务 D |

v1.0.5 的 `data/database.py` 与 v1.0.6 逐字节相同（只差 `config.py` 里数据目录的位置），不单独出样本。
每份样本的来源 commit、生成日期、库大小、期望计数见 `manifest.json`。

固定场景（详见 `generate.py` 的 `build_scenario`）：

- **任务 A**：已结束，中文进程名，5 个指标（`memory_rss` / `memory_vms` / `memory_private` /
  `memory_pagefile` / `num_handles`，其中三项内存指标同值，是 1.x 的重复指标），600 个周期。
  v1.0.x 一任务只能一个指标，按当年用法拆成 5 个同进程、同时间戳的单指标任务（`golden-a` 是
  其中的 `memory_vms` 任务）；
- **任务 B**：`status` 仍为 `running`（模拟异常退出），200 个周期，含 `memory_pagefile`；
- **任务 C**：已结束的空任务，0 个点；
- **任务 D / 备注**：已发布版本都没有，只在派生的 v3 样本里补写。

期望值由生成器按现有迁移链的口径**独立推算**（不运行被测迁移链）：v2 把同一任务、同一时间戳下的
三项重复内存指标合并为一行 `memory_private`（不跨任务合并），指标列表同样改名去重；
`sample_count` = 不同时间戳数；遗留 `running` 任务经启动校正变为 `stopped`，结束时间取最后一次采样。
派生样本的 `rows_written*` 记的是 1.4.1 写入时的行数，派生库里已是去重后的数据。

## 重新生成

在项目根目录：

```
python tests/fixtures/legacy_db/generate.py            # 工作目录建在系统临时目录，结束后删除
python tests/fixtures/legacy_db/generate.py --keep     # 保留工作目录便于排查
```

生成是确定性的（固定场景、gzip `mtime=0`）：代码不变时重复生成的 `*.db.gz` 逐字节相同；
`manifest.json` 只会变生成日期与派生样本记录的 HEAD commit。

**数据目录隔离**：旧版代码只用 `git archive <tag> -- config.py data/__init__.py data/database.py data/models.py`
导出到临时目录，再以 `python -I` 子进程运行。旧版 `config.py` 在非打包环境把数据目录定为
"`config.py` 所在目录 `\data`"，即临时导出目录；写库脚本先断言导入的模块来自导出目录、
`config.DATA_DIR` 与目标库路径都不在本仓库内，否则拒绝写库。旧版代码绝不会在仓库里运行，
也就碰不到项目根 `data\` 下的用户数据。工作目录本身也禁止落在仓库内。

## 新增 tag

1. 先看该 tag 的 `data/database.py` 是否与已有样本的某个版本相同（`git show <tag>:data/database.py`
   比对）；相同则不必新增；
2. 在 `generate.py` 的 `TAG_SAMPLES` 加一行 `(tag, 该版本 user_version, 写库接口风格)`；接口风格
   与已有的 `single` / `multi` 都不同时，在 `CHILD_SCRIPT` 里加对应分支；该版本新增的功能
   （备注、打点等）要写进场景，并在 `expected_after_migration` 里给出期望；
3. 新的数据库版本（如 v5）从未发布前，用 `DERIVED_SAMPLES` 截断迁移派生；
4. 重新运行生成器，跑 `tests/test_migration_golden.py`，把新样本与 `manifest.json` 一起提交。

## 反向兼容测试

`frozen_v1_4_1/` 是 v1.4.1 `data/database.py`、`data/models.py` 的冻结副本（只改了导入方式、
`db_path` 改为必填、删掉自测段，见文件头），**禁止修改**。`tests/test_reverse_compat_v141.py`
用它打开当前版本建的库，模拟用户回退到 1.4.1：能列任务、读点、新增任务写点、删除任务，
不改 `user_version`、不迁移、不生成 `.bak`；再用当前版本打开时降级期间的数据仍在。

这条路径的意义：1.4.1 只判断 `user_version >= 1` 就按原样使用库，任何新结构都会被它直接读写。
库结构每次升级都必须保证"回退到 1.4.1 不损坏数据、升回来不丢数据"，否则用户在新版出问题时
没有安全的退路。阶段 1 升到 v5 后的预期（降级期间写入空的 `data_points`、升级时导回）写在
该测试文件开头。
