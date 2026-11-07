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

多个目标（多个 `--pid` / `--name`）时取最严重的判定：被测进程退出 > 持续上涨 > 数据不足 > 平稳。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 平稳（或普通命令成功） |
| 10 | 持续上涨（疑似泄漏） |
| 20 | 数据不足（样本或轮数不够，无法判定） |
| 30 | 被测进程在监控期间退出 |
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
