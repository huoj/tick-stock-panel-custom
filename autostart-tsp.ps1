# TSP 开机自启守护脚本
#
# 职责:
#   1. 等待网络与磁盘就绪（开机初期磁盘/网络尚未准备好，直接启动会失败）
#   2. 若服务已在运行则跳过（避免重复启动叠加内存，每个后端常驻 1GB+）
#   3. 启动后校验端口，失败则写日志并退出（由计划任务的"失败重启"兜底）
#   4. 所有输出写入 data\logs\autostart.log，便于事后排查
#
# 调用方式:
#   计划任务 / 启动文件夹快捷方式均指向本脚本（-NoProfile -ExecutionPolicy Bypass -File）
#
# 参数:
#   -DelaySeconds  启动前额外等待秒数（默认 30，避开开机高峰）
#   -Detached      以隐藏窗口方式拉起 start-tsp.ps1 后立即返回（默认开启）

[CmdletBinding()]
param(
    [int]$DelaySeconds = 30,
    [switch]$WaitForExit
)

$ErrorActionPreference = 'Continue'
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir  = Join-Path $Root 'data\logs'
$LogFile = Join-Path $LogDir 'autostart.log'
$StartScript = Join-Path $Root 'start-tsp.ps1'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-AutostartLog($msg) {
    $line = "[{0}] {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# 日志轮转: 超过 512KB 保留最近 200 行，避免长期累积
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 512KB)) {
    $tail = Get-Content $LogFile -Tail 200 -ErrorAction SilentlyContinue
    Set-Content -Path $LogFile -Value $tail -Encoding UTF8
}

Write-AutostartLog "=== 自启开始 (DelaySeconds=$DelaySeconds) ==="

function Test-PortBusy($port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

# ---- 1. 等待基础条件 ----
if ($DelaySeconds -gt 0) {
    Write-AutostartLog "等待 $DelaySeconds 秒（避开开机高峰）..."
    Start-Sleep -Seconds $DelaySeconds
}

# 等待仓库目录可访问（OneDrive/网盘占位、机械盘休眠都可能短暂不可读）
$probe = Join-Path $Root 'backend\pyproject.toml'
for ($i = 0; $i -lt 20; $i++) {
    if (Test-Path $probe) { break }
    Write-AutostartLog "仓库目录尚不可读，重试 $($i+1)/20 ..."
    Start-Sleep -Seconds 15
}
if (-not (Test-Path $probe)) {
    Write-AutostartLog "失败: 仓库目录不可读 ($Root)，放弃本次自启"
    exit 1
}

# ---- 2. 已在运行则跳过 ----
# 两个端口都被占用 = 视为已完整运行；只占一个 = 半残状态，先清理再启动
$bUp = Test-PortBusy 3018
$fUp = Test-PortBusy 3011
if ($bUp -and $fUp) {
    Write-AutostartLog "服务已在运行 (3018 + 3011 均就绪)，跳过自启"
    exit 0
}
if ($bUp -or $fUp) {
    Write-AutostartLog "检测到半残状态 (backend=$bUp frontend=$fUp)，先清理旧实例"
    $stopScript = Join-Path $Root 'stop-tsp.ps1'
    if (Test-Path $stopScript) {
        & pwsh -NoProfile -ExecutionPolicy Bypass -File $stopScript *>> $LogFile
        Start-Sleep -Seconds 5
    }
}

# ---- 3. 启动 ----
if (-not (Test-Path $StartScript)) {
    Write-AutostartLog "失败: 找不到 $StartScript"
    exit 1
}

Write-AutostartLog "启动服务..."
# 关键: 脱离当前会话启动。计划任务结束时若子进程仍依附于任务, 会被一起结束。
# -WindowStyle Hidden + Start-Process 保证 pwsh 退出后 node/python 继续存活。
$proc = Start-Process -FilePath 'pwsh' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $StartScript) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru
Write-AutostartLog "已拉起 start-tsp.ps1 (pid=$($proc.Id))"

# ---- 4. 校验结果 ----
$deadline = (Get-Date).AddSeconds(180)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if ((Test-PortBusy 3018) -and (Test-PortBusy 3011)) { $ok = $true; break }
    if ($proc.HasExited -and -not (Test-PortBusy 3018)) {
        Write-AutostartLog "启动进程已退出 (exit=$($proc.ExitCode))"
        break
    }
}

if ($ok) {
    Write-AutostartLog "自启成功: backend=http://localhost:3018 frontend=http://localhost:3011"
    exit 0
}

Write-AutostartLog "自启失败: 端口未就绪，详见 backend.err.log / frontend.err.log"
exit 1
