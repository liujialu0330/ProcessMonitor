<#
.SYNOPSIS
安装包冒烟：旧版本（默认 1.4.1）覆盖升级到当前构建的全过程，CI 的 release 工作流与本机都能跑。

.DESCRIPTION
步骤（任何一步失败都立即停止、退出码 1，工作目录保留供查看）：
  1. 准备：核对本机没有正式安装的本程序，也没有正在运行的实例（有则拒绝，绝不动它）
  2. 取得旧版安装包：-OldInstaller 给本地文件，否则从 GitHub Releases 下载并校验 sha256 摘要
  3. 静默安装旧版到工作目录下的临时目录（/DIR=），并在 _internal 里放一个只有旧版才有的
     残留文件，用来证明 [InstallDelete] 生效
  4. 在临时数据目录放入 golden 旧库（v1.4.1 写出的 v1 库）、旧版更新偏好 update_prefs.json
     与 1.x 风格的配置（数据目录一律用 --data-dir 指向临时目录，不碰 %LOCALAPPDATA%）
  5. 静默覆盖安装当前构建的安装包（同一目录）
  6. 安装目录的文件清单、大小与 SHA-256 与 dist 完全一致，没有任何旧版残留
  7. 在安装目录里跑一遍打包自检（build\run_self_test.py；-SkipSelfTest 可跳过）
  8. 启动新版，等日志出现「数据库迁移完成」：.bak_v1 存在、update_prefs.json 已被删除、
     配置里的旧值保留
  9. taskkill（不加 /F，即正常关闭窗口）退出：进程自行结束、退出码 0、会话锁已删除
 10. 静默卸载：程序文件被删，数据目录保留

全部路径在工作目录（默认系统临时目录下的新目录）内；通过后删除工作目录，-KeepWork 保留。

.PARAMETER NewInstaller   当前构建的安装包；缺省取 dist\installer\Windows_v{config.APP_VERSION}_Setup.exe
.PARAMETER OldInstaller   本地的旧版安装包；不给则按 -OldVersion 从 GitHub Releases 下载
.PARAMETER OldVersion     旧版版本号，缺省 1.4.1（Release 资产名 Windows_v{版本}_Setup.exe）
.PARAMETER OldSha256      旧版安装包的期望摘要（给了就据此校验；没给则取 Release 的 digest，取不到只告警）
.PARAMETER Repo           owner/name；缺省取 config.py 的 GITHUB_OWNER/GITHUB_REPO
.PARAMETER WorkDir        工作目录（必须不存在或为空）
.PARAMETER DistDir        onedir 产物目录；缺省 dist\{config.APP_NAME}
.PARAMETER GoldenDb       golden 旧库（gzip）；缺省 tests\fixtures\legacy_db\v1.4.1_v1.db.gz
.EXAMPLE
powershell -File build\installer_smoke.ps1
.EXAMPLE
powershell -File build\installer_smoke.ps1 -OldInstaller D:\old\Windows_v1.4.1_Setup.exe -WorkDir D:\tmp\smoke
#>
[CmdletBinding()]
param(
    [string]$NewInstaller,
    [string]$OldInstaller,
    [string]$OldVersion = '1.4.1',
    [string]$OldSha256,
    [string]$Repo,
    [string]$WorkDir,
    [string]$DistDir,
    [string]$GoldenDb,
    [int]$MigrationTimeoutSec = 120,
    [int]$ExitTimeoutSec = 60,
    [switch]$SkipSelfTest,
    [switch]$KeepWork
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
# python 子进程按 UTF-8 输出，PowerShell 才能正确读到中文的 APP_NAME 与自检输出
$env:PYTHONIOENCODING = 'utf-8'

$Root = Split-Path -Parent $PSScriptRoot
$script:StepNo = 0
$script:StepTotal = 10
$script:Checks = 0

# ---------------------------------------------------------------- 输出

function Step([string]$Title) {
    $script:StepNo++
    Write-Host ""
    Write-Host ("[步骤 {0}/{1}] {2}" -f $script:StepNo, $script:StepTotal, $Title)
}
function Ok([string]$Message) {
    $script:Checks++
    Write-Host ("  [通过] {0}" -f $Message)
}
function Info([string]$Message) { Write-Host ("  - {0}" -f $Message) }
function Warn([string]$Message) { Write-Host ("  [警告] {0}" -f $Message) }
function Fail([string]$Message) { throw $Message }

# ---------------------------------------------------------------- 工具

function Get-ConfigValue([string]$Name) {
    $value = & python -c "import config; print(config.$Name)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $value) { Fail "读取 config.$Name 失败（需要 python，且在项目根目录可导入 config）" }
    return ([string]$value).Trim()
}

function Get-AppIdKey {
    $iss = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'setup.iss') -Raw -Encoding UTF8
    # .iss 里 AppId={{GUID} 的第一个花括号是转义，真正的 AppId 是 {GUID}
    if ($iss -match 'AppId=\{(\{[0-9A-Fa-f-]+\})') { return ($Matches[1] + '_is1') }
    Fail 'setup.iss 里找不到 AppId'
}

function Test-SetupRegistered([string]$KeyName) {
    foreach ($root in 'HKCU:', 'HKLM:') {
        foreach ($sub in 'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                         'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall') {
            if (Test-Path -LiteralPath "$root\$sub\$KeyName") { return "$root\$sub\$KeyName" }
        }
    }
    return $null
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-Program([string]$Path, [string[]]$ArgumentList, [int]$TimeoutSec, [string]$What) {
    $proc = Start-Process -FilePath $Path -ArgumentList $ArgumentList -PassThru -WindowStyle Hidden
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        Fail ("{0} 超过 {1} 秒没有结束，已结束该进程" -f $What, $TimeoutSec)
    }
    if ($proc.ExitCode -ne 0) { Fail ("{0} 失败，退出码 {1}" -f $What, $proc.ExitCode) }
}

function Get-FileMap([string]$Dir, [string[]]$ExcludePatterns) {
    $map = @{}
    $base = (Resolve-Path -LiteralPath $Dir).Path.TrimEnd('\')
    foreach ($file in Get-ChildItem -LiteralPath $base -Recurse -File -Force) {
        $rel = $file.FullName.Substring($base.Length + 1)
        $skip = $false
        foreach ($pattern in $ExcludePatterns) { if ($rel -like $pattern) { $skip = $true } }
        if ($skip) { continue }
        $map[$rel.ToLowerInvariant()] = [pscustomobject]@{
            Path = $rel; Size = $file.Length; Hash = (Get-Sha256 $file.FullName) }
    }
    return $map
}

# 日志文件可能正被程序写入：以共享读方式打开
function Read-SharedText([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        return $reader.ReadToEnd()
    } finally { $stream.Dispose() }
}

function Wait-LogPattern([string]$LogPath, [string]$Pattern, [int]$TimeoutSec, $Process, [string]$What) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ((Read-SharedText $LogPath) -match $Pattern) { return }
        if ($Process.HasExited) { Fail ("{0}：程序已提前退出（退出码 {1}），日志里没有出现「{2}」" -f $What, $Process.ExitCode, $Pattern) }
        Start-Sleep -Milliseconds 500
    }
    Fail ("{0}：{1} 秒内日志里没有出现「{2}」（日志 {3}）" -f $What, $TimeoutSec, $Pattern, $LogPath)
}

function Stop-ProcessesUnder([string]$Dir) {
    # 只按安装路径结束，绝不按映像名（那会误伤本机上的正式版本）
    foreach ($proc in Get-Process -ErrorAction SilentlyContinue) {
        $path = $null
        try { $path = $proc.Path } catch { }
        if ($path -and $path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase)) {
            try { $proc.Kill() } catch { }
        }
    }
}

# ---------------------------------------------------------------- 主流程

$succeeded = $false
$appDir = $null
try {
    Push-Location $Root
    $version = Get-ConfigValue 'APP_VERSION'
    $appName = Get-ConfigValue 'APP_NAME'
    if (-not $NewInstaller) { $NewInstaller = Join-Path $Root "dist\installer\Windows_v${version}_Setup.exe" }
    if (-not $DistDir) { $DistDir = Join-Path $Root "dist\$appName" }
    if (-not $GoldenDb) { $GoldenDb = Join-Path $Root 'tests\fixtures\legacy_db\v1.4.1_v1.db.gz' }
    if (-not $Repo) { $Repo = (Get-ConfigValue 'GITHUB_OWNER') + '/' + (Get-ConfigValue 'GITHUB_REPO') }
    if (-not $WorkDir) {
        $WorkDir = Join-Path ([IO.Path]::GetTempPath()) ('pm_installer_smoke_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    }
    $exeName = "$appName.exe"

    Write-Host "安装包冒烟：$OldVersion → $version"
    Write-Host "  新版安装包：$NewInstaller"
    Write-Host "  onedir 产物：$DistDir"
    Write-Host "  工作目录  ：$WorkDir"
    foreach ($required in @($NewInstaller, $GoldenDb, (Join-Path $DistDir $exeName))) {
        if (-not (Test-Path -LiteralPath $required)) { Fail "找不到必需的文件：$required" }
    }

    # ---- 1 准备
    Step '准备：确认不会碰到本机正式安装的版本'
    $registered = Test-SetupRegistered (Get-AppIdKey)
    if ($registered) { Fail "本机已安装本程序（$registered）：静默安装会改写它的注册信息，请在没有安装本程序的环境（如 CI）运行" }
    Ok '本机没有正式安装的本程序'
    if (Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($exeName)) -ErrorAction SilentlyContinue) {
        Fail '本机有正在运行的本程序，请先关闭（脚本不会动它）'
    }
    Ok '没有正在运行的实例'
    if ((Test-Path -LiteralPath $WorkDir) -and (Get-ChildItem -LiteralPath $WorkDir -Force | Select-Object -First 1)) {
        Fail "工作目录必须不存在或为空：$WorkDir"
    }
    New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
    $appDir = Join-Path $WorkDir 'app'
    $dataDir = Join-Path $WorkDir 'data'
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $installedExe = Join-Path $appDir $exeName

    # ---- 2 旧版安装包
    Step "取得旧版 $OldVersion 安装包并校验"
    $assetName = "Windows_v${OldVersion}_Setup.exe"
    $expectedHash = if ($OldSha256) { $OldSha256.Trim().ToLowerInvariant() -replace '^sha256:', '' } else { $null }
    if ($OldInstaller) {
        if (-not (Test-Path -LiteralPath $OldInstaller)) { Fail "旧版安装包不存在：$OldInstaller" }
        $oldPath = $OldInstaller
        Info "使用本地旧版安装包：$oldPath"
    } else {
        $oldPath = Join-Path $WorkDir $assetName
        $headers = @{ 'User-Agent' = 'ProcessMonitor-installer-smoke'; 'Accept' = 'application/vnd.github+json' }
        if ($env:GITHUB_TOKEN) { $headers['Authorization'] = "Bearer $env:GITHUB_TOKEN" }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/tags/v$OldVersion" -Headers $headers
        $asset = @($release.assets | Where-Object { $_.name -eq $assetName }) | Select-Object -First 1
        if (-not $asset) { Fail "Release v$OldVersion 里没有资产 $assetName" }
        if (-not $expectedHash) {
            if (-not $asset.digest -or $asset.digest -notmatch '^sha256:([0-9a-fA-F]{64})$') {
                Fail "Release 资产 $assetName 没有 sha256 摘要，无法校验下载"
            }
            $expectedHash = $Matches[1].ToLowerInvariant()
        }
        Info "下载 $($asset.browser_download_url)（$([math]::Round($asset.size / 1MB, 1)) MB）"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $oldPath -Headers @{ 'User-Agent' = 'ProcessMonitor-installer-smoke' }
        if ((Get-Item -LiteralPath $oldPath).Length -ne [int64]$asset.size) { Fail "下载的文件大小与 Release 记录不符" }
    }
    $oldHash = Get-Sha256 $oldPath
    if ($expectedHash) {
        if ($oldHash -ne $expectedHash) { Fail "旧版安装包摘要不符：实际 $oldHash，期望 $expectedHash" }
        Ok "旧版安装包 sha256 校验通过（$($oldHash.Substring(0, 12))…）"
    } else {
        Warn "没有可比对的摘要，旧版安装包 sha256=$oldHash 未校验"
    }

    # ---- 3 安装旧版
    Step '静默安装旧版到临时目录'
    $silent = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', '/TASKS=""', "/DIR=`"$appDir`"")
    Invoke-Program $oldPath ($silent + "/LOG=`"$WorkDir\old_install.log`"") 300 '旧版静默安装'
    if (-not (Test-Path -LiteralPath $installedExe)) { Fail "旧版安装后找不到 $installedExe" }
    Ok "旧版已安装到 $appDir"
    $internal = Join-Path $appDir '_internal'
    if (-not (Test-Path -LiteralPath $internal)) { Fail '旧版安装目录里没有 _internal（不是 onedir 版本？）' }
    $staleFile = Join-Path $internal 'stale_from_old_version.dll'
    $staleDir = Join-Path $internal 'stale_old_dir'
    Set-Content -LiteralPath $staleFile -Value 'old' -Encoding ASCII
    New-Item -ItemType Directory -Force -Path $staleDir | Out-Null
    Set-Content -LiteralPath (Join-Path $staleDir 'x.bin') -Value 'old' -Encoding ASCII
    Ok '已在 _internal 放入只有旧版才有的残留文件与目录'

    # ---- 4 数据
    Step '放入 golden 旧库、旧版更新偏好与 1.x 配置（--data-dir 指向临时目录）'
    $dbPath = Join-Path $dataDir 'monitor.db'
    $source = [IO.File]::OpenRead($GoldenDb)
    try {
        $gzip = New-Object IO.Compression.GZipStream($source, [IO.Compression.CompressionMode]::Decompress)
        $target = [IO.File]::Create($dbPath)
        try { $gzip.CopyTo($target) } finally { $target.Dispose(); $gzip.Dispose() }
    } finally { $source.Dispose() }
    $prefsPath = Join-Path $dataDir 'update_prefs.json'
    [IO.File]::WriteAllText($prefsPath, '{"skipped_version": "1.4.0"}', (New-Object Text.UTF8Encoding($false)))
    $configPath = Join-Path $dataDir 'config.json'
    # 关闭到托盘必须为 false，否则 taskkill 的关窗会被藏进托盘
    $legacyConfig = '{"Monitor": {"DefaultInterval": 2}, "Behavior": {"CloseToTray": false}, "QFluentWidgets": {"ThemeMode": "Light"}}'
    [IO.File]::WriteAllText($configPath, $legacyConfig, (New-Object Text.UTF8Encoding($false)))
    Ok ("golden 库 {0:N0} 字节、update_prefs.json、config.json 已就位" -f (Get-Item -LiteralPath $dbPath).Length)

    # ---- 5 覆盖安装
    Step '静默覆盖安装当前构建'
    Invoke-Program $NewInstaller ($silent + "/LOG=`"$WorkDir\new_install.log`"") 300 '新版静默覆盖安装'
    Ok '覆盖安装完成（退出码 0）'

    # ---- 6 文件清单
    Step '安装目录与 dist 的文件清单一致，无旧版残留'
    if ((Test-Path -LiteralPath $staleFile) -or (Test-Path -LiteralPath $staleDir)) {
        Fail '旧版残留文件仍在 _internal 里：[InstallDelete] 没有生效'
    }
    Ok '[InstallDelete] 已清掉 _internal 里的旧版残留'
    $expected = Get-FileMap $DistDir @()
    $icon = Join-Path $PSScriptRoot 'app_green_icon.ico'
    if (Test-Path -LiteralPath $icon) {
        # setup.iss 另外把图标复制到安装目录根
        $expected['app_green_icon.ico'] = [pscustomobject]@{ Path = 'app_green_icon.ico'; Size = (Get-Item -LiteralPath $icon).Length; Hash = (Get-Sha256 $icon) }
    }
    $actual = Get-FileMap $appDir @('unins*.exe', 'unins*.dat', 'unins*.msg')
    $problems = New-Object System.Collections.Generic.List[string]
    foreach ($key in $expected.Keys) {
        if (-not $actual.ContainsKey($key)) { $problems.Add("缺少：" + $expected[$key].Path); continue }
        if ($actual[$key].Size -ne $expected[$key].Size -or $actual[$key].Hash -ne $expected[$key].Hash) {
            $problems.Add("内容不同：" + $expected[$key].Path)
        }
    }
    foreach ($key in $actual.Keys) {
        if (-not $expected.ContainsKey($key)) { $problems.Add("多余（残留）：" + $actual[$key].Path) }
    }
    if ($problems.Count -gt 0) {
        $problems | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" }
        Fail ("安装目录与 dist 不一致，共 {0} 处差异" -f $problems.Count)
    }
    Ok ("安装目录 {0} 个文件的路径、大小与 SHA-256 与 dist 完全一致" -f $actual.Count)

    # ---- 7 自检
    Step '在安装目录里运行打包自检'
    if ($SkipSelfTest) {
        Warn '已按参数跳过'
    } else {
        $selfTestOut = & python (Join-Path $PSScriptRoot 'run_self_test.py') $installedExe 2>&1 | Out-String
        $selfTestCode = $LASTEXITCODE
        Write-Host ($selfTestOut.TrimEnd() -replace '(?m)^', '    ')
        if ($selfTestCode -ne 0) { Fail "安装目录里的打包自检失败（退出码 $selfTestCode）" }
        Ok '安装后的程序通过打包自检'
    }

    # ---- 8 启动、迁移
    Step '启动新版，等待数据库迁移完成'
    $logPath = Join-Path $dataDir 'logs\app.log'
    $app = Start-Process -FilePath $installedExe -ArgumentList @('--data-dir', "`"$dataDir`"") -PassThru -WindowStyle Hidden
    Info "已启动，PID $($app.Id)"
    Wait-LogPattern $logPath '数据库迁移完成' $MigrationTimeoutSec $app '等待迁移'
    Ok '日志出现「数据库迁移完成」'
    if (-not (Test-Path -LiteralPath "$dbPath.bak_v1")) { Fail '迁移后 monitor.db.bak_v1 不存在' }
    Ok 'monitor.db.bak_v1 已保留（可回退 1.4.1）'
    # update_prefs.json 在读取配置时导入并删除，早于迁移，这里迁移完成后必然已处理
    if (Test-Path -LiteralPath $prefsPath) { Fail 'update_prefs.json 仍然存在：程序没有导入并删除它' }
    Ok 'update_prefs.json 已被程序导入并删除'
    $configAfter = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($configAfter.Monitor.DefaultInterval -ne 2) { Fail 'config.json 里的旧设置（DefaultInterval=2）没有保留' }
    Ok 'config.json 里的旧设置保留'
    Wait-LogPattern $logPath '命令行控制已就绪' 60 $app '等待主窗口就绪'
    Ok '主窗口已就绪'

    # ---- 9 正常退出
    Step 'taskkill 正常关闭（不加 /F）'
    & taskkill.exe /PID $app.Id | Out-Null
    if (-not $app.WaitForExit($ExitTimeoutSec * 1000)) {
        try { $app.Kill() } catch { }
        Fail "正常关闭后 $ExitTimeoutSec 秒内程序没有退出"
    }
    Ok "程序已自行退出（退出码 $($app.ExitCode)）"
    if ($app.ExitCode -ne 0) { Fail "程序退出码不是 0：$($app.ExitCode)" }
    if ((Read-SharedText $logPath) -notmatch '应用退出，退出码=0') { Fail '日志里没有「应用退出，退出码=0」' }
    Ok '日志记录了正常退出路径'
    if (Test-Path -LiteralPath (Join-Path $dataDir 'session.lock')) { Fail '退出后会话锁 session.lock 仍然存在' }
    Ok '会话锁已删除'

    # ---- 10 卸载
    Step '静默卸载，数据目录保留'
    $uninstaller = Get-ChildItem -LiteralPath $appDir -Filter 'unins*.exe' | Select-Object -First 1
    if (-not $uninstaller) { Fail '安装目录里没有卸载程序' }
    Invoke-Program $uninstaller.FullName @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 120 '静默卸载'
    # 卸载程序会把自己复制到临时目录再执行，原进程先退出：轮询到程序文件被删
    $deadline = (Get-Date).AddSeconds(60)
    while ((Test-Path -LiteralPath $installedExe) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Test-Path -LiteralPath $installedExe) { Fail '卸载后主程序文件仍在' }
    Ok '主程序已卸载'
    if (Test-Path -LiteralPath (Join-Path $appDir '_internal')) { Fail '卸载后 _internal 仍在' }
    Ok '_internal 已清除'
    if (-not (Test-Path -LiteralPath $dbPath) -or -not (Test-Path -LiteralPath "$dbPath.bak_v1")) { Fail '卸载后数据目录里的数据库丢失' }
    Ok '数据目录（monitor.db 与 .bak_v1）保留'

    $succeeded = $true
    Write-Host ""
    Write-Host ("安装包冒烟通过（{0} 项检查）" -f $script:Checks)
} catch {
    Write-Host ""
    Write-Host ("安装包冒烟失败：{0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host ("  工作目录保留在 {0}（安装日志 old_install.log / new_install.log，程序日志 data\logs\app.log）" -f $WorkDir)
} finally {
    if ($appDir) { Stop-ProcessesUnder $appDir }
    Pop-Location
    if ($succeeded -and -not $KeepWork -and $WorkDir -and (Test-Path -LiteralPath $WorkDir)) {
        Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($succeeded) { exit 0 } else { exit 1 }
