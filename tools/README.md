# 命令行与脚本控制

进程监控助手 2.0 起可以被命令行与脚本驱动：开始监控、打点、停止、等待、导出报告、取泄漏判定。
设置页「允许命令行与脚本控制」关闭后，除 `ping` / `activate` 外的命令一律回 `disabled`。

## pmctl

打包后与主程序同目录的 `pmctl.exe`（控制台程序）；源码运行时用 `python pmctl.py`。

- 程序在运行（有窗口或无窗口）：直接连接它；
- 程序不在：自动以 `--background` 拉起一个**无窗口实例**（没有主窗口和托盘，用 `pmctl quit` 结束；
  `--no-launch` 可禁止自动拉起）；
- `--data-dir` 决定连接哪个实例，与主程序的 `--data-dir` 规则相同。

```bat
pmctl ping
pmctl start --pid 1234 --metrics memory_rss,num_handles --interval 5
pmctl marker --task all --label "第 1 轮"
pmctl wait --task <任务ID> --until "rounds>=10" --timeout-s 3600
pmctl stop --task <任务ID>
pmctl export --task <任务ID> --dir D:\out --kinds csv,report,summary
pmctl verdict --task <任务ID>
pmctl quit
```

一条命令跑完（适合 CI）：开始 → 每 S 秒自动打一轮点 → 到时或到轮数停止 → 导出 → 判定。

```bat
pmctl run --name MyApp.exe --wait --rounds-every 60 --rounds 20 --out D:\out --junit D:\out\leak.xml
```

进程组：`--tree` 监控 `--pid`（或 `--launch` 启动的程序）及其全部子孙进程，根进程退出任务才结束；
`--group-name 进程名` 监控全部同名进程（不要求唯一，当前没有也能开始，成员归零不结束任务）。
两者都不能是本程序自身。进程组不接受峰值类与基本优先级指标，会自动加上“进程总数”；判定结果
另含每个成员（同名进程的合并）各指标的结论，任一成员持续上涨即整体持续上涨。

```bat
pmctl start --tree --pid 1234
pmctl start --group-name chrome.exe --metrics memory_private,num_handles
pmctl run --tree --launch D:pp\App.exe --stop-after 3600 --out D:\out
```

协议里对应 `start` 的 `target_kind` 参数（`pid` 缺省 / `tree` / `name`）；`status` 的每个任务带
`target_kind`，`verdict` 在进程组任务里多一个 `members` 数组。

被测进程重启后自动续接过的任务（`start` 的方案里开了续接）按启动段逐段判定，整体取最差的一段
（持续上涨 > 数据不足 > 平稳）；`verdict` 多出 `segments`（每段的 `number` / `start` / `end` /
`verdict` / `rising_metrics` / `rising_names` / `samples`）与 `conclusion`（纯文本，如“各段判定：
第 1 段 持续上涨（私有字节）；第 2 段 本段数据不足”）。中途异常退出过（崩溃后被续接）整体为
`target_exited`（退出码 30），另有 `mid_run_crashes`，`conclusion` 写“中途异常退出 N 次（最近一次
0xC0000017（内存不足））”与各段判定；指标行的数值是最后一段的，基线在更早的启动段时 `baseline`
为空、`baseline_note` 写“基线在第 N 段，不可比”。

多个目标（多个 `--pid` / `--name` / `--group-name`）时取最严重的判定：被测进程退出 > 持续上涨 > 数据不足 > 平稳。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 平稳（或普通命令成功） |
| 10 | 持续上涨（疑似泄漏） |
| 20 | 数据不足（样本或轮数不够，无法判定） |
| 30 | 被测进程在监控期间退出（含中途崩溃后被续接过） |
| 2 | 参数错误（含 `bad_request` / `unknown_command` / `invalid_args`） |
| 3 | 连接失败（找不到程序、拉起超时、连接断开、身份校验失败、命令行控制已关闭） |
| 1 | 其他错误（任务不存在、状态不允许、权限不足、内部错误；`wait` 未满足） |

`--junit` 写出 JUnit XML：`testsuite name="进程监控助手"`，每个指标一个 `testcase`；持续上涨为
`failure`，数据不足为 `skipped`，被测进程退出为 `error`。

## 协议（自己写客户端时参考）

- 传输：Windows 命名管道 `\\.\pipe\<实例名>`，只允许当前用户连接；实例名由用户名与数据目录哈希算出
  （见 `utils/instance_name.py`）。
- 帧：每行一条 UTF-8 JSON，以 `\n` 结尾，单行不超过 1 MiB；未知字段忽略。
- 请求：`{"v":1,"id":1,"cmd":"status","args":{}}`
- 成功：`{"v":1,"id":1,"ok":true,"result":{...}}`
- 失败：`{"v":1,"id":1,"ok":false,"error":{"code":"not_found","message":"..."}}`
- 事件：`{"v":1,"id":1,"event":"heartbeat","data":{...}}`（`wait` / `export` 等长命令每 5 秒一次，
  收到最终回复前可能有多条）。

错误码：`bad_request`、`unknown_command`、`invalid_args`、`not_found`、`invalid_state`、`busy`、
`forbidden`、`disabled`、`not_ready`、`timeout`、`internal`。

`tools/pmclient.py` 是不到 50 行的最小示例（只用标准库，不拉起程序、不核验服务端身份）：

```bat
python tools\pmclient.py status
python tools\pmclient.py marker "{\"task_id\":\"all\"}"
```

## 安全

- 管道只对当前用户开放（DACL）；服务端核对客户端进程的完整性级别，低于本程序时拒绝
  `start` / `stop` / `export` / `quit`（`forbidden`）。
- pmctl 连接后核对管道服务端的 PID 与 `ping` 报告的一致、且属于当前用户，防止被冒名的管道骗取数据。

## 浸泡工具（24 小时浸泡与缩短演练）

回答“监控工具自己有没有在泄漏”：用独立探针记录本工具的资源曲线，再用与分析页同一套趋势判定
（`core.analysis.trend.judge`）判这次浸泡过没过。完整步骤、中途操作与要留的产物见
`doc小时浸泡手册.md`。

| 文件 | 作用 |
|---|---|
| `tools\soak_run.ps1` | 一条命令拉起：负载 → 本工具（缺省 `-Exe` 不给 = 源码无窗口；给 = 启动安装版）→ 用 `pmctl` 建 26 指标任务 → 探针；`-Stop -OutDir 目录` 收尾（各任务 `verdict`、导出证据包、停负载与探针、跑判定）；`-Wait` 等探针结束后自动收尾 |
| `tools\soak_probe.py` | 独立探针（标准库 + psutil，IPC 只用 `core.ipc.client`）：每 10 秒记本工具的私有字节 / 工作集 / 句柄 / GDI / USER / 线程 / CPU，并经 `status` 记各任务的 `planned_cycles` / `done_cycles` / `missed_cycles`；输出目录里的 `soak_probe.csv`、`soak_tasks.csv`、`soak_events.csv`（事件：`probe_start` / `target_found` / `target_exited` / `target_not_found` / `ipc_lost` / `ipc_ok` / `probe_stop`）；每行即时 flush、已有文件时追加；本工具退出后默认继续等它重新出现（`--on-exit exit` 则一并退出）；目录里出现 `STOP` 文件、到 `--hours` 或 Ctrl+C 正常收尾。找目标：`--pid` / `--name`（缺省 `进程监控助手.exe`）/ `--by-ipc`（经 ping 取 PID，需 `--data-dir`） |
| `tools\soak_verdict.py` | 读探针 CSV 判定：窗口 `--hours`（缺省 20）内私有字节增长 ≤ `--max-growth-mb`（30）且判定非“持续上涨”；句柄 / GDI / USER / 线程非“持续上涨”（小容差内只提示）；每个任务采样完成率 ≥ `--min-completion`（0.995）；工具未退出；记录覆盖。退出码 0 通过 / 1 未通过 / 2 输入错误；`--json` 机读 |
| `testsixtures\soak_target.py` | 被测负载：`--mode leak`（泄漏内存与句柄，速率可配）/ `stable`（锯齿不涨）/ `restart`（定时退出，`--exit-code 0xC0000005` 模拟崩溃，`--supervise` 由监督进程重新拉起，使本工具能按“重启”续接） |

```bat
python tools\soak_probe.py --out-dir D:\soakun1 --by-ipc --data-dir D:\soakun1\data --hours 24
python tools\soak_verdict.py D:\soakun1 --hours 20
```
