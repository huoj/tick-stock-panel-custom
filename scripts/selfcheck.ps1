# TSP 升级后全面功能自检
$ErrorActionPreference = 'Continue'
$TSP = 'http://127.0.0.1:3018'
$script:pass = 0; $script:warn = 0; $script:fail = 0

function Ck($name, [scriptblock]$fn) {
    try {
        $r = & $fn
        if ($r -ne $null -and $r -ne '' -and $r -ne 0 -and $r -ne $false) {
            Write-Host ("  [PASS] {0}  -> {1}" -f $name, $r) -ForegroundColor Green
            $script:pass++
        } else {
            Write-Host ("  [WARN] {0}  -> 空/零" -f $name) -ForegroundColor Yellow
            $script:warn++
        }
    } catch {
        $m = $_.Exception.Message
        Write-Host ("  [FAIL] {0}  -> {1}" -f $name, $m.Substring(0, [Math]::Min(90, $m.Length))) -ForegroundColor Red
        $script:fail++
    }
}

$null = Invoke-WebRequest -Uri "$TSP/api/auth/login" -Method Post -ContentType 'application/json' -Body '{"password":"tsp3018"}' -UseBasicParsing -SessionVariable sess -TimeoutSec 30

Write-Host "`n=== 1. 数据读取 ===" -ForegroundColor Cyan
Ck '行情日K' { (Invoke-WebRequest -Uri "$TSP/api/kline/daily?symbol=000333.SZ&limit=5" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content.Length }
# 注意: symbol 是必需参数(Query(...)), 缺了会 422。
# 且盘中之外 row 为 null(内存无当日实时K线)属正常, 故断言返回的 symbol 而非 row。
Ck '最新行情' { $r=(Invoke-WebRequest -Uri "$TSP/api/kline/daily/latest?symbol=000333.SZ" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; $r.symbol }
Ck '市场概览' { $r=(Invoke-WebRequest -Uri "$TSP/api/overview/market" -UseBasicParsing -WebSession $sess -TimeoutSec 90).Content|ConvertFrom-Json; $r.breadth.up }
Ck '市场快照' { $r=(Invoke-WebRequest -Uri "$TSP/api/screener/market-snapshot" -UseBasicParsing -WebSession $sess -TimeoutSec 120).Content|ConvertFrom-Json; @($r.rows).Count }
Ck '指数日K' { (Invoke-WebRequest -Uri "$TSP/api/index/daily?symbol=000001.SH&limit=5" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content.Length }
Ck '市场环境' { $r=(Invoke-WebRequest -Uri "$TSP/api/regime/latest" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; $r.row.phase }

Write-Host "`n=== 2. 扩展表 ===" -ForegroundColor Cyan
foreach ($t in 'ext_valuation','ext_shareholders','ext_unlock','ext_dividend','ext_fundhold','ext_forecast','ext_blocktrade','ext_research','ext_bsdetail','ext_gdhs_ts','ext_gn_ths','ext_hy_ths') {
    $tn = $t
    Ck $tn {
        $r = (Invoke-WebRequest -Uri "$TSP/api/ext-data/$tn/rows?limit=1" -UseBasicParsing -WebSession $sess -TimeoutSec 90).Content | ConvertFrom-Json
        [int]$r.total
    }
}

Write-Host "`n=== 3. 策略与因子 ===" -ForegroundColor Cyan
Ck '策略列表' { $r=(Invoke-WebRequest -Uri "$TSP/api/screener/strategies" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; @($r.presets).Count }
Ck '因子库' { $r=(Invoke-WebRequest -Uri "$TSP/api/factors" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; $all=if($r.items){@($r.items)}else{@($r)}; $all.Count }
Ck '因子列' { (Invoke-WebRequest -Uri "$TSP/api/backtest/factor/columns" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content.Length }

Write-Host "`n=== 4. 扫描器 ===" -ForegroundColor Cyan
Ck '扫描器' {
    $b = @{ conditions=@('signal_ma20_breakout'); limit=3 } | ConvertTo-Json
    $r = (Invoke-WebRequest -Uri "$TSP/api/screener/run" -Method Post -ContentType 'application/json' -Body $b -UseBasicParsing -WebSession $sess -TimeoutSec 90).Content | ConvertFrom-Json
    @($r.rows).Count
}
Ck '扫描器+扩展列' {
    $b = @{ conditions=@('signal_ma20_breakout'); limit=2; ext_columns='ext_valuation.pe_ttm' } | ConvertTo-Json
    $r = (Invoke-WebRequest -Uri "$TSP/api/screener/run" -Method Post -ContentType 'application/json' -Body $b -UseBasicParsing -WebSession $sess -TimeoutSec 90).Content | ConvertFrom-Json
    @($r.rows[0].PSObject.Properties.Name | Where-Object { $_ -like 'ext_*' }).Count
}
Ck '连板梯队' { (Invoke-WebRequest -Uri "$TSP/api/screener/limit-ladder" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content.Length }

Write-Host "`n=== 5. 自选与分析 ===" -ForegroundColor Cyan
Ck '自选股' { $r=(Invoke-WebRequest -Uri "$TSP/api/watchlist" -UseBasicParsing -WebSession $sess -TimeoutSec 30).Content|ConvertFrom-Json; @($r.symbols).Count }
Ck '关键价位' { $r=(Invoke-WebRequest -Uri "$TSP/api/stock-analysis/levels?symbol=000333.SZ" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; $r.close }
Ck '异动总览' { $r=(Invoke-WebRequest -Uri "$TSP/api/abnormal/overview" -UseBasicParsing -WebSession $sess -TimeoutSec 90).Content|ConvertFrom-Json; $r.counts.triggered }
Ck '龙虎榜' { $r=(Invoke-WebRequest -Uri "$TSP/api/market-recap/dragon-tiger" -UseBasicParsing -WebSession $sess -TimeoutSec 90).Content|ConvertFrom-Json; $r.state }

Write-Host "`n=== 6. 模拟盘 ===" -ForegroundColor Cyan
Ck '账户列表' { $r=(Invoke-WebRequest -Uri "$TSP/api/paper/accounts" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; @($r.accounts).Count }
Ck '持仓' { (Invoke-WebRequest -Uri "$TSP/api/paper/positions?account=acc_mvxyztiip" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content.Length }

Write-Host "`n=== 7. 自定义信号 ===" -ForegroundColor Cyan
Ck '自定义信号' { $r=(Invoke-WebRequest -Uri "$TSP/api/custom-signals" -UseBasicParsing -WebSession $sess -TimeoutSec 60).Content|ConvertFrom-Json; @($r.signals).Count }

Write-Host "`n$('=' * 58)"
Write-Host ("  PASS={0}  WARN={1}  FAIL={2}" -f $script:pass, $script:warn, $script:fail) -ForegroundColor $(if($script:fail -gt 0){'Red'}else{'Green'})
Write-Host ('=' * 58)
