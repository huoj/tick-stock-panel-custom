# TSP · A股量化工作台 —— 一键启动（Windows / PowerShell）
#
# 启动：pwsh -File .\start-tsp.ps1
# 停止：pwsh -File .\stop-tsp.ps1
#
# 说明：
#   - 后端 FastAPI  http://localhost:3018
#   - 前端 Vite    http://localhost:3011  ← 浏览器打开这个
#   - 本机代理工具把 NO_PROXY 写成 `...,::1,[::1]`，httpx 解析会崩溃，
#     后端统一经 run-local.py 启动（在进程内清洗该变量），不要直接跑 uvicorn。

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$LogDir = Join-Path $Root 'data\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-PortBusy($port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

foreach ($p in 3018, 3011) {
    if (Test-PortBusy $p) {
        Write-Host "[TSP] 端口 $p 已被占用 —— 可能已有一份在运行。先执行 stop-tsp.ps1" -ForegroundColor Yellow
        exit 1
    }
}

$Py = Join-Path $BackendDir '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { throw "未找到后端虚拟环境：$Py（请先执行 uv sync --frozen）" }

# ===== 关键依赖自检 =====
# 这几个是可选的 extra, 未安装时不会在启动期报错, 而是在"用到那一刻"失败:
#   vectorbt 缺 → 回测接口直接 400(前端只显示错误, 看不出是缺依赖)
#   pytest   缺 → 无法跑测试套件
# 放在启动前检查, 缺了就明确提示补装命令, 避免运行中莫名报错。
$depMissing = @()
$probe = @'
import importlib.util as u
miss = []
for mod, extra in (("vectorbt", "backtest"), ("openai", None), ("polars", None), ("duckdb", None)):
    if u.find_spec(mod) is None:
        miss.append(f"{mod}:{extra}" if extra else mod)
print("MISSING=" + ",".join(miss))
'@
$probeOut = & $Py -c $probe 2>&1
foreach ($line in @($probeOut)) {
    if ($line -is [string] -and $line.StartsWith('MISSING=')) {
        $raw = $line.Substring(8).Trim()
        if ($raw) { $depMissing = $raw.Split(',') }
    }
}
if ($depMissing.Count -gt 0) {
    Write-Host "[TSP] 缺少后端依赖: $($depMissing -join ', ')" -ForegroundColor Yellow
    $extras = @($depMissing | Where-Object { $_ -match ':backtest$' } | ForEach-Object { ($_ -split ':')[0] })
    if ($extras.Count -gt 0) {
        Write-Host "      回测功能将不可用。补装:  cd backend; uv sync --frozen --extra backtest --extra dev" -ForegroundColor Yellow
    } else {
        Write-Host "      请执行:  cd backend; uv sync --frozen" -ForegroundColor Yellow
    }
    Write-Host "      继续启动（相关功能会在调用时报错）..." -ForegroundColor DarkGray
}

Write-Host '[TSP] 启动后端 ...' -ForegroundColor DarkGray
$backend = Start-Process -FilePath $Py `
    -ArgumentList (Join-Path $Root 'run-local.py') `
    -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $LogDir 'backend.out.log') `
    -RedirectStandardError  (Join-Path $LogDir 'backend.err.log') `
    -NoNewWindow -PassThru
$backend.Id | Set-Content (Join-Path $LogDir 'backend.pid')

Write-Host '[TSP] 启动前端 ...' -ForegroundColor DarkGray
$env:BACKEND_HOST = '0.0.0.0'
$env:BACKEND_PORT = '3018'
$pnpm = (Get-Command 'pnpm.cmd' -ErrorAction SilentlyContinue).Source
if (-not $pnpm) { $pnpm = (Get-Command 'pnpm' -ErrorAction Stop).Source }
$frontend = Start-Process -FilePath $env:ComSpec `
    -ArgumentList @('/c', ('"{0}" dev --host 0.0.0.0 --port 3011' -f $pnpm)) `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput (Join-Path $LogDir 'frontend.out.log') `
    -RedirectStandardError  (Join-Path $LogDir 'frontend.err.log') `
    -NoNewWindow -PassThru
$frontend.Id | Set-Content (Join-Path $LogDir 'frontend.pid')

# 等待两个端口就绪
$deadline = (Get-Date).AddSeconds(120)
$backendOk = $false
$frontendOk = $false
while ((Get-Date) -lt $deadline -and -not ($backendOk -and $frontendOk)) {
    Start-Sleep -Seconds 2
    if (-not $backendOk)  { $backendOk  = Test-PortBusy 3018 }
    if (-not $frontendOk) { $frontendOk = Test-PortBusy 3011 }
    if ($backend.HasExited) { break }
}

# 记录「真正监听端口」的进程 PID。
# 为什么不能直接用 Start-Process 返回的 Id:
#   - 后端: run-local.py 再起 python3.13 子进程跑 uvicorn, 父进程只是壳;
#   - 前端: cmd.exe -> pnpm.exe(多层) -> node/vite, 包装层一大堆。
# 直接结束包装层会留下孤儿进程继续占用端口。
#
# 实测差异: 前端 cmd.exe 执行完 pnpm 就退出, 只剩 node 子进程常驻 →
# 前端的"root"从一开始就不可靠, 必须以 listener 为准。后端 python 壳会
# 常驻, root 与 listener 都可保留作双保险。
#
# 端口刚 LISTEN 时 OwningProcess 偶发取不到, 故重试若干次再落盘。
foreach ($item in @(@{ Port = 3018; File = 'backend.pid'; Root = $backend.Id; KeepRoot = $true },
                    @{ Port = 3011; File = 'frontend.pid'; Root = $frontend.Id; KeepRoot = $false })) {
    $listenerPid = 0
    for ($try = 0; $try -lt 15; $try++) {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $item.Port -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn -and $conn.OwningProcess -gt 0) { $listenerPid = $conn.OwningProcess; break }
        Start-Sleep -Seconds 1
    }
    if ($listenerPid -gt 0) {
        $listenerPid | Set-Content (Join-Path $LogDir $item.File)
    }
    $rootFile = Join-Path $LogDir ("{0}.root" -f ($item.File -replace '\.pid$',''))
    if ($item.KeepRoot -and (Get-Process -Id $item.Root -ErrorAction SilentlyContinue)) {
        $item.Root | Set-Content $rootFile
    } else {
        Remove-Item $rootFile -Force -ErrorAction SilentlyContinue
    }
}

# 记录人类可读的启动清单, 排查时不用再猜谁是谁
$manifest = @{
    started_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    backend = @{ pid_file = 'backend.pid'; root = $backend.Id; port = 3018 }
    frontend = @{ pid_file = 'frontend.pid'; root = $frontend.Id; port = 3011 }
} | ConvertTo-Json -Depth 3
$manifest | Set-Content (Join-Path $LogDir 'start-manifest.json')

if ($backendOk -and $frontendOk) {
    Write-Host ''
    Write-Host '  TSP 已启动' -ForegroundColor Green
    Write-Host '  浏览器打开： http://localhost:3011' -ForegroundColor Green
    Write-Host '  后端接口：   http://localhost:3018' -ForegroundColor DarkGray
    Write-Host "  日志目录：   $LogDir" -ForegroundColor DarkGray
    Write-Host '  停止服务：   pwsh -File .\stop-tsp.ps1' -ForegroundColor DarkGray
} else {
    Write-Host "[TSP] 启动未完成（后端就绪=$backendOk，前端就绪=$frontendOk），请查看 $LogDir" -ForegroundColor Red
    exit 1
}
