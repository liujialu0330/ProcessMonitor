<#
.SYNOPSIS
    一条命令拉起浸泡：被测负载 -> 本工具 -> 监控任务 -> 探针；-Stop 收尾并判定。

.DESCRIPTION
    启动阶段（默认）：
      1. 启动 -Leak / -Stable / -Restart 个负载（tests\fixtures\soak_target.py，隐藏窗口）；
      2. 本工具没在运行就启动它（缺省 -Exe 不给 = 源码运行 python main.py --background；
         给 -Exe = 启动安装版，有窗口）；
      3. 用 pmctl 给每个负载建一个任务（26 个指标、-Interval 秒周期；重启负载带续接方案）；
      4. 启动探针 tools\soak_probe.py，写 <OutDir>\soak_probe.csv 等。
    之后脚本立即返回（加 -Wait 则一直等到探针结束并自动收尾）。状态记在 <OutDir>\soak_state.json。

    收尾阶段（-Stop -OutDir 目录；-Wait 结束时自动执行）：
      各任务 verdict -> 导出证据包到 <OutDir>\evidence -> 停任务 -> 停负载与探针 ->
      soak_verdict 判定本工具自身。本脚本启动的无窗口实例会被 pmctl quit 结束；
      用户自己启动的安装版不会被关闭。

    24 小时浸泡的完整步骤、中途操作与要留的产物见 doc\24小时浸泡手册.md。

.EXAMPLE
    # 真实 24 小时浸泡（安装版，5 个任务）
    .\tools\soak_run.ps1 -Exe "C:\Program Files\进程监控助手\进程监控助手.exe" -OutDir D:\soak\run1

.EXAMPLE
    # 15 分钟缩短演练（源码运行、无窗口）
    .\tools\soak_run.ps1 -OutDir $env:TEMP\soak_drill -Hours 0.25 -Leak 1 -Stable 1 -Restart 1 `
        -LeakMbPerMin 3 -LeakHandlesPerMin 60 -RestartLifetimeS 120 -CrashEvery 3 -VerdictHours 0.2 -Wait
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [string]$Exe,
    [string]$DataDir,
    [string]$Python = 'python',
    [double]$Hours = 24,
    [double]$VerdictHours = 20,
    [int]$Leak = 1,
    [int]$Stable = 3,
    [int]$Restart = 1,
    [double]$LeakMbPerMin = 0.5,
    [double]$LeakHandlesPerMin = 10,
    [double]$RestartLifetimeS = 4500,
    [int]$CrashEvery = 0,
    [double]$Interval = 1,
    [switch]$Background,
    [switch]$Wait,
    [switch]$Stop
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$Root = Split-Path -Parent $PSScriptRoot
$TargetScript = Join-Path $Root 'tests\fixtures\soak_target.py'
$ProbeScript = Join-Path $Root 'tools\soak_probe.py'
$VerdictScript = Join-Path $Root 'tools\soak_verdict.py'

if (-not $OutDir) { $OutDir = Join-Path $env:TEMP ('soak_' + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$StateFile = Join-Path $OutDir 'soak_state.json'

function Quote([string]$Text) {
    if ($Text -match '[\s"]') { return '"' + ($Text -replace '"', '\"') + '"' }
    return $Text
}

function Join-Args([string[]]$Items) {
    return (($Items | ForEach-Object { Quote $_ }) -join ' ')
}

function Start-Hidden([string]$FilePath, [string[]]$Items, [string]$LogName) {
    $params = @{
        FilePath = $FilePath; ArgumentList = (Join-Args $Items); PassThru = $true
        WindowStyle = 'Hidden'; WorkingDirectory = $Root
    }
    if ($LogName) {
        $params.RedirectStandardOutput = Join-Path $OutDir ($LogName + '.out.log')
        $params.RedirectStandardError = Join-Path $OutDir ($LogName + '.err.log')
    }
    return Start-Process @params
}

function Invoke-Pmctl([string[]]$PmArgs, [switch]$Json, [switch]$NoLaunch) {
    $all = @()
    if ($script:DataDir) { $all += @('--data-dir', $script:DataDir) }
    if ($Json) { $all += '--json' }
    if ($NoLaunch) { $all += '--no-launch' }
    $all += $PmArgs
    if ($script:PmctlExe) { $out = & $script:PmctlExe @all 2>&1 }
    else { $out = & $Python (Join-Path $Root 'pmctl.py') @all 2>&1 }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Text = (($out | Out-String).Trim()) }
}

function Test-ToolReady {
    $r = Invoke-Pmctl -PmArgs @('ping') -NoLaunch
    return ($r.Code -eq 0)
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutS, [string]$What) {
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning "等待超时：$What"
    return $false
}

function Read-State { return (Get-Content -Raw -Encoding UTF8 $StateFile | ConvertFrom-Json) }

# ---------- 工具位置 ----------
if ($Stop -and (Test-Path $StateFile)) {
    # 收尾时沿用启动时记下的设置，命令行上显式给的优先
    $saved = Read-State
    if (-not $Exe -and $saved.exe) { $Exe = $saved.exe }
    if (-not $DataDir -and $saved.data_dir) { $DataDir = $saved.data_dir }
    if (-not $PSBoundParameters.ContainsKey('VerdictHours') -and $saved.verdict_hours) {
        $VerdictHours = [double]$saved.verdict_hours
    }
}
if ($Exe) {
    $Exe = [System.IO.Path]::GetFullPath($Exe)
    if (-not (Test-Path $Exe)) { throw "找不到 -Exe：$Exe" }
    $candidate = Join-Path (Split-Path -Parent $Exe) 'pmctl.exe'
    $script:PmctlExe = if (Test-Path $candidate) { $candidate } else { $null }
    if (-not $script:PmctlExe) { Write-Warning "安装目录里没有 pmctl.exe，改用源码里的 pmctl.py" }
} else {
    $script:PmctlExe = $null
    if (-not $DataDir) { $DataDir = Join-Path $OutDir 'data' }
}
$script:DataDir = $DataDir

# ---------- 收尾 ----------
function Stop-Soak {
    if (-not (Test-Path $StateFile)) { throw "找不到状态文件 $StateFile（OutDir 不对，或还没启动过）" }
    $state = Read-State
    $summary = New-Object System.Collections.Generic.List[string]

    Write-Host "== 各任务判定 =="
    foreach ($t in $state.targets) {
        if (-not $t.task_id) { continue }
        $r = Invoke-Pmctl -PmArgs @('verdict', '--task', $t.task_id)
        $line = "[$($t.label)] task=$($t.task_id) pmctl verdict 退出码 $($r.Code)"
        Write-Host $line; Write-Host $r.Text
        $summary.Add($line); $summary.Add($r.Text)
    }
    Set-Content -Encoding UTF8 -Path (Join-Path $OutDir 'task_verdicts.txt') -Value $summary

    Write-Host "== 导出证据包 =="
    $evidence = Join-Path $OutDir 'evidence'
    New-Item -ItemType Directory -Force $evidence | Out-Null
    foreach ($t in $state.targets) {
        if (-not $t.task_id) { continue }
        $r = Invoke-Pmctl -PmArgs @('export', '--task', $t.task_id, '--dir', $evidence, '--evidence')
        Write-Host "[$($t.label)] 导出退出码 $($r.Code)"
    }

    Write-Host "== 停任务、负载与探针 =="
    foreach ($t in $state.targets) {
        if ($t.task_id) { Invoke-Pmctl -PmArgs @('stop', '--task', $t.task_id) | Out-Null }
    }
    New-Item -ItemType File -Force (Join-Path $OutDir 'STOP') | Out-Null
    if ($state.probe_pid) {
        $probe = Get-Process -Id $state.probe_pid -ErrorAction SilentlyContinue
        if ($probe) { $probe.WaitForExit(30000) | Out-Null }
        $probe = Get-Process -Id $state.probe_pid -ErrorAction SilentlyContinue
        if ($probe) { Stop-Process -Id $state.probe_pid -Force }
    }
    foreach ($t in $state.targets) {
        foreach ($id in @($t.supervisor_pid, $t.pid)) {
            if ($id) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
        }
        if ($t.pid_file -and (Test-Path $t.pid_file)) {
            $current = [int](Get-Content $t.pid_file -ErrorAction SilentlyContinue)
            if ($current) { Stop-Process -Id $current -Force -ErrorAction SilentlyContinue }
        }
    }
    if ($state.started_tool) {
        Invoke-Pmctl -PmArgs @('quit') -NoLaunch | Out-Null
    } else {
        Write-Host "本工具是你自己启动的，没有替你关闭；诊断包、截图请按手册在界面里留存。"
    }

    Write-Host "== 本工具自身判定 =="
    & $Python $VerdictScript $OutDir --hours $VerdictHours | Tee-Object -FilePath (Join-Path $OutDir 'soak_verdict.txt') | Write-Host
    return $LASTEXITCODE
}

if ($Stop) {
    exit (Stop-Soak)
}

# ---------- 启动 ----------
if (($Leak + $Stable + $Restart) -gt 5) { throw "任务上限是 5 个：-Leak + -Stable + -Restart 不能超过 5" }
if (($Leak + $Stable + $Restart) -lt 1) { throw "至少需要一个负载" }
New-Item -ItemType Directory -Force $OutDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutDir 'targets') | Out-Null
Remove-Item -ErrorAction SilentlyContinue (Join-Path $OutDir 'STOP')
Write-Host "输出目录：$OutDir"

$metrics = (& $Python -c "import sys; sys.path.insert(0, r'$Root'); from utils.metrics import all_metric_types; print(','.join(all_metric_types()))" | Out-String).Trim()
if (-not $metrics) { throw "取不到指标清单（$Python 能导入项目吗？）" }
Write-Host ("指标：{0} 个" -f ($metrics -split ',').Count)

$state = [ordered]@{
    started = (Get-Date).ToString('s'); out_dir = $OutDir; data_dir = $DataDir; exe = $Exe
    hours = $Hours; verdict_hours = $VerdictHours; started_tool = $false
    tool_pid = $null; probe_pid = $null; targets = @()
}

# 1. 负载
$targets = @()
function New-Target([string]$Kind, [int]$Index, [string[]]$Extra) {
    $label = "$Kind$Index"
    $pidFile = Join-Path $OutDir "targets\$label.pid"
    Remove-Item -ErrorAction SilentlyContinue $pidFile
    $items = @($TargetScript, '--mode', $Kind, '--pid-file', $pidFile) + $Extra
    $proc = Start-Hidden $Python $items "targets\$label"
    return [ordered]@{ label = $label; kind = $Kind; pid_file = $pidFile; supervisor_pid = $proc.Id
                       pid = $null; task_id = $null }
}
for ($i = 1; $i -le $Leak; $i++) {
    $targets += New-Target 'leak' $i @('--leak-mb-per-min', "$LeakMbPerMin", '--leak-handles-per-min', "$LeakHandlesPerMin")
}
for ($i = 1; $i -le $Stable; $i++) { $targets += New-Target 'stable' $i @() }
for ($i = 1; $i -le $Restart; $i++) {
    $extra = @('--supervise', '--lifetime-s', "$RestartLifetimeS", '--restart-delay-s', '3')
    if ($CrashEvery -gt 0) { $extra += @('--crash-every', "$CrashEvery") }
    $targets += New-Target 'restart' $i $extra
}
foreach ($t in $targets) {
    Wait-Until { Test-Path $t.pid_file } 30 "$($t.label) 写出 PID 文件" | Out-Null
    if (Test-Path $t.pid_file) { $t.pid = [int](Get-Content $t.pid_file) }
    # 非监督的负载，进程就是 supervisor_pid 那一个；监督模式下 supervisor_pid 是监督进程
    if ($t.kind -ne 'restart') { $t.supervisor_pid = $null }
    Write-Host ("负载 {0} pid={1}" -f $t.label, $t.pid)
}
$state.targets = $targets

# 2. 本工具
if (Test-ToolReady) {
    Write-Host "本工具已在运行，直接使用"
} else {
    if ($Exe) {
        $toolArgs = @()
        if ($Background) { $toolArgs += '--background' }
        if ($DataDir) { $toolArgs += @('--data-dir', $DataDir) }
        if ($toolArgs.Count -gt 0) {
            $tool = Start-Process -FilePath $Exe -ArgumentList (Join-Args $toolArgs) -PassThru
        } else {
            $tool = Start-Process -FilePath $Exe -PassThru
        }
    } else {
        $tool = Start-Hidden $Python @((Join-Path $Root 'main.py'), '--background', '--data-dir', $DataDir) 'tool'
    }
    $state.started_tool = $true
    $state.tool_pid = $tool.Id
    if (-not (Wait-Until { Test-ToolReady } 90 "本工具就绪（ping）")) { throw "本工具没有就绪" }
}
Write-Host "本工具已就绪"

# 3. 任务
$planFile = Join-Path $OutDir 'restart_plan.json'
Set-Content -Encoding ASCII -Path $planFile -Value '{"schema_version": 1, "resume_on_restart": true, "restart_limit": 20}'
foreach ($t in $targets) {
    if (-not $t.pid) { Write-Warning "$($t.label) 没有 PID，跳过建任务"; continue }
    $args2 = @('start', '--pid', "$($t.pid)", '--metrics', $metrics, '--interval', "$Interval",
               '--note', "soak $($t.label)")
    if ($t.kind -eq 'restart') { $args2 += @('--plan', $planFile) }
    $r = Invoke-Pmctl -Json -PmArgs $args2
    if ($r.Code -ne 0) { Write-Warning "$($t.label) 建任务失败（退出码 $($r.Code)）：$($r.Text)"; continue }
    $t.task_id = ($r.Text | ConvertFrom-Json).result.task_id
    Write-Host ("任务 {0} task_id={1}" -f $t.label, $t.task_id)
}

# 4. 探针
$probeArgs = @($ProbeScript, '--out-dir', $OutDir, '--hours', "$Hours")
if ($Exe) {
    $probeArgs += @('--name', (Split-Path -Leaf $Exe))
} else {
    $probeArgs += '--by-ipc'
}
if ($DataDir) { $probeArgs += @('--data-dir', $DataDir) }
$probe = Start-Hidden $Python $probeArgs 'probe'
$state.probe_pid = $probe.Id
Write-Host "探针 pid=$($probe.Id)，$Hours 小时后自动结束"

($state | ConvertTo-Json -Depth 5) | Set-Content -Encoding UTF8 -Path $StateFile
Write-Host "状态已记到 $StateFile"

if ($Wait) {
    Write-Host "等待探针结束……"
    $probe.WaitForExit()
    exit (Stop-Soak)
}
Write-Host ""
Write-Host "结束时运行：.\tools\soak_run.ps1 -Stop -OutDir `"$OutDir`" -VerdictHours $VerdictHours"
