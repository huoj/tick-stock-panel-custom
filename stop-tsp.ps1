# TSP ── 停止后端与前端
#
# 按进程树清理。依据三处来源, 优先级从高到低:
#   1. *.root        —— start-tsp.ps1 记录的包装层根进程 (最可靠, 能整棵结束)
#   2. *.pid         —— 真正监听端口的进程 (端口漂移时可能已失效)
#   3. 端口兜底      —— 仍占用 3018/3011 的进程
#
# 安全边界: 只结束命令行里出现本仓库路径的进程, 避免误杀其他项目的同名服务
# (同一台机器上可能有别的 python/node 也在监听端口的情况)。
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'data\logs'

# 判定某个 PID 是否属于本仓库: 命令行包含仓库根路径 (或 backend/frontend 子目录)
function Test-OwnedByRepo($procId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    $cmd = [string]$p.CommandLine
    if (-not $cmd) {
        # 有进程读不到命令行(系统进程/权限不足): 保守视为不属于本仓库, 不杀
        return $false
    }
    return ($cmd -like "*$Root*") -or ($cmd -like '*tick-stock-panel*')
}

function Stop-Tree($procId, $label) {
    if ($procId -notmatch '^\d+$') { return $false }
    if (-not (Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue)) { return $false }
    if (-not (Test-OwnedByRepo ([int]$procId))) {
        Write-Host "[TSP] 跳过 $label (PID $procId): 不属于本仓库进程" -ForegroundColor Yellow
        return $false
    }
    Write-Host "[TSP] 结束 $label (PID $procId)" -ForegroundColor DarkGray
    & cmd /c "taskkill /F /T /PID $procId 2>nul" | Out-Null
    return $true
}

foreach ($name in 'backend', 'frontend') {
    $rootFile = Join-Path $LogDir "$name.root"
    $pidFile = Join-Path $LogDir "$name.pid"

    $rootId = $null
    if (Test-Path $rootFile) {
        $rootId = (Get-Content $rootFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    }
    $listenerId = $null
    if (Test-Path $pidFile) {
        $listenerId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    }

    # 先按树根结束(能带走全部子进程), 再补监听进程(树根已死时兜住)
    $null = Stop-Tree $rootId     "$name (root)"
    $null = Stop-Tree $listenerId "$name (listener)"

    Remove-Item $rootFile, $pidFile -Force -ErrorAction SilentlyContinue
}

# 兜底: 仍占用端口的进程, 同样过归属校验
foreach ($port in 3018, 3011) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($p in @($conns.OwningProcess | Sort-Object -Unique)) {
        if ($p -gt 0) { $null = Stop-Tree $p "port $port" }
    }
}

# 清理启动清单, 避免下次启动时误读旧状态
Remove-Item (Join-Path $LogDir 'start-manifest.json') -Force -ErrorAction SilentlyContinue

Write-Host '[TSP] 已停止' -ForegroundColor Green
