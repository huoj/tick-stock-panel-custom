# TSP 升级预演脚本
# 用途：在真正更新 GitHub 版本前，先在不影响工作区的前提下试合并，报告冲突与风险。
# 用法：pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\preflight-upgrade.ps1
#       pwsh ... -File scripts\preflight-upgrade.ps1 -Apply   # 预演通过后直接升级常用

param(
    [switch]$Apply,           # 预演通过后执行实际升级
    [switch]$KeepWorktree     # 保留预演目录便于排查
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$WT = Join-Path (Split-Path -Parent $RepoRoot) 'wt-preflight'
$PyBackend = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

Push-Location $RepoRoot
try {
    Step 0 '环境检查'
    $branch = (git branch --show-current).Trim()
    $head   = (git rev-parse --short HEAD).Trim()
    Write-Host "  分支: $branch"
    Write-Host "  HEAD: $head"

    $dirty = @(git status --porcelain)
    if ($dirty.Count -gt 0) {
        Warn "工作区有 $($dirty.Count) 项未提交改动。预演忽略它们；正式升级前请先提交或 stash。"
        $dirty | Select-Object -First 8 | ForEach-Object { Write-Host "    $_" }
    } else { Ok '工作区干净' }

    Step 1 '拉取上游最新'
    git fetch origin --prune 2>&1 | Out-Null

    # 用完整 ref 避免 origin/main 歧义
    $upRef = 'refs/remotes/origin/main'
    $upstream = (git rev-parse --short $upRef).Trim()
    $mergeBase = (git merge-base HEAD $upRef).Trim()

    # 只取"本地独有"的提交（merge-base 之后、且不在上游的分支）
    $localCommits = @(git rev-list --no-merges "$upRef..HEAD")
    $behindCount  = @(git rev-list --count "HEAD..$upRef")
    $aheadCount   = $localCommits.Count

    Write-Host "  origin/main: $upstream"
    Write-Host "  合并基点:    $($mergeBase.Substring(0,7))"
    Write-Host "  上游新增:    $behindCount 个提交"
    Write-Host "  本地独有:    $aheadCount 个提交"

    if ($behindCount -eq 0) {
        Ok '已是最新版本，无需升级'
        if (-not $Apply) { Pop-Location; return }
    }

    Step 2 '创建隔离预演环境'
    if (Test-Path $WT) { git worktree remove $WT --force 2>&1 | Out-Null }
    git worktree add --detach $WT HEAD 2>&1 | Out-Null
    Push-Location $WT
    try {
        git checkout -B _preflight $upRef 2>&1 | Out-Null
        Write-Host "  预演目录: $WT"

        Step 3 "重放本地 $aheadCount 个提交并检测冲突"
        $conflict = $false
        $failedCommits = @()
        # rev-list 是逆序（新→旧），cherry-pick 必须按时间正序
        $ordered = @($localCommits) | Select-Object -Skip 0
        [array]::Reverse($ordered)
        foreach ($c in $ordered) {
            $subject = (git log -1 --format='%s' $c).Trim()
            $r = git cherry-pick $c 2>&1
            if ($LASTEXITCODE -ne 0) {
                $conflict = $true
                $failedCommits += "$($c.Substring(0,7)) $subject"
                Bad "冲突: $($c.Substring(0,7))  $subject"
                $r | Where-Object { $_ -match 'CONFLICT|would be overwritten|error:' } |
                    Select-Object -First 4 | ForEach-Object { Write-Host "      $_" }
                git cherry-pick --abort 2>&1 | Out-Null
            } else {
                Ok "$($c.Substring(0,7))  $subject"
            }
        }

        if ($conflict) {
            Bad '存在冲突，不建议直接升级'
            Write-Host "  冲突提交数: $($failedCommits.Count) / $aheadCount"
        } else {
            Ok '零冲突，全部本地提交可干净应用'

            Step 4 '静态检查'
            $changed = @(git diff --name-only $upRef HEAD 2>&1 | Where-Object { $_ -match '\.py$' })
            $syntaxBad = @()
            if ((Test-Path $PyBackend) -and $changed.Count -gt 0) {
                foreach ($f in $changed) {
                    if (Test-Path $f) {
                        & $PyBackend -c "import ast; ast.parse(open(r'$f',encoding='utf-8').read())" 2>&1 | Out-Null
                        if ($LASTEXITCODE -ne 0) { $syntaxBad += $f }
                    }
                }
            }
            if ($syntaxBad.Count -eq 0) { Ok "语法检查通过（$($changed.Count) 个 Python 文件）" }
            else { Bad "语法错误: $($syntaxBad -join ', ')" }

            Step 5 '关键模块导入检查'
            $env:PYTHONPATH = Join-Path $WT 'backend'
            $env:PYTHONIOENCODING = 'utf-8'
            $mods = @('app.services.backtest','app.api.backtest','app.backtest.matrix',
                      'app.backtest.strategy','app.custom.assistant.streaming','app.strategy.paper')
            $importBad = @()
            if (Test-Path $PyBackend) {
                Push-Location (Join-Path $WT 'backend')
                foreach ($m in $mods) {
                    & $PyBackend -c "import $m" 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) { $importBad += $m }
                }
                Pop-Location
            }
            if ($importBad.Count -eq 0) { Ok "模块导入通过（$($mods.Count) 个）" }
            else { Bad "导入失败: $($importBad -join ', ')" }

            Step 6 'FastAPI 应用创建检查'
            if (Test-Path $PyBackend) {
                Push-Location (Join-Path $WT 'backend')
                $out = & $PyBackend -c "from app.main import app; print(len(app.routes))" 2>&1
                Pop-Location
                if ($LASTEXITCODE -eq 0) { Ok "FastAPI 创建成功，路由数 $(@($out)[-1])" }
                else {
                    Bad 'FastAPI 创建失败'
                    @($out) | Select-Object -Last 5 | ForEach-Object { Write-Host "      $_" }
                }
            }

            Step 7 '依赖变更检查'
            foreach ($d in @('backend/pyproject.toml','backend/uv.lock','frontend/package.json','frontend/pnpm-lock.yaml')) {
                $diff = @(git diff $upRef HEAD -- $d 2>&1 | Where-Object { $_ })
                if ($diff.Count -gt 0) { Warn "$d 有变更，升级后需重新同步依赖" }
            }
        }

        $preflightOk = -not $conflict
    } finally {
        Pop-Location
        if (-not $KeepWorktree) {
            git worktree remove $WT --force 2>&1 | Out-Null
            Write-Host "`n  预演目录已清理"
        } else {
            Write-Host "`n  预演目录保留: $WT"
        }
    }

    Write-Host "`n$('=' * 60)"
    if ($preflightOk) { Write-Host '预演结论：可以安全升级' -ForegroundColor Green }
    else              { Write-Host '预演结论：存在冲突，请先解决' -ForegroundColor Red }
    Write-Host ('=' * 60)

    if ($Apply -and $preflightOk) {
        Step 8 '执行实际升级'
        $ts = Get-Date -Format 'yyyyMMdd-HHmm'
        $bundle = Join-Path (Split-Path -Parent $RepoRoot) "git-backup-$ts.bundle"
        git bundle create $bundle --all 2>&1 | Out-Null
        Ok "已备份到 $bundle"

        git rebase $upRef
        if ($LASTEXITCODE -eq 0) {
            Ok 'rebase 完成'
            Write-Host "`n  后续请手动执行:"
            Write-Host "    cd backend; uv sync --frozen --extra backtest --extra dev"
            Write-Host "    cd ..\frontend; pnpm install"
            Write-Host "    cd ..; pwsh start-tsp.ps1"
        } else {
            Bad 'rebase 失败，可用 git rebase --abort 回滚'
        }
    } elseif ($Apply) {
        Warn '预演未通过，已跳过实际升级'
    } else {
        Write-Host "`n  加 -Apply 参数可在预演通过后直接升级"
    }
} finally {
    Pop-Location
}
