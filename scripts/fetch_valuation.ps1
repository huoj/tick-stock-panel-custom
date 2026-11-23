# 从腾讯行情接口拉取全市场估值数据(PE/PB)，写入 TSP 扩展表 ext_valuation
$ErrorActionPreference = 'Continue'
$env:no_proxy = '127.0.0.1,localhost,::1'
$env:NO_PROXY = '127.0.0.1,localhost,::1'

# 登录
$null = Invoke-WebRequest -Uri 'http://127.0.0.1:3018/api/auth/login' -Method Post -ContentType 'application/json' -Body '{"password":"tsp3018"}' -UseBasicParsing -SessionVariable sess -TimeoutSec 30

$codes = Get-Content "D:\deepseek_path\tick-stock-panel\data\logs\tx_codes.txt" | Where-Object { $_ }
Write-Host "待拉取 $($codes.Count) 只"

function Convert-Symbol($txCode) {
  # sz000001 -> 000001.SZ ; sh600519 -> 600519.SH ; bj920005 -> 920005.BJ
  $mkt = $txCode.Substring(0,2)
  $num = $txCode.Substring(2)
  $sfx = switch ($mkt) { 'sh' { 'SH' } 'sz' { 'SZ' } 'bj' { 'BJ' } default { 'SH' } }
  return "$num.$sfx"
}

$allRows = @()
$batchSize = 60
$total = $codes.Count
$sw = [System.Diagnostics.Stopwatch]::StartNew()

for ($i = 0; $i -lt $total; $i += $batchSize) {
  $end = [Math]::Min($i + $batchSize - 1, $total - 1)
  $batch = $codes[$i..$end] -join ','
  try {
    $r = Invoke-WebRequest -Uri "https://qt.gtimg.cn/q=$batch" -UseBasicParsing -TimeoutSec 40
    $bytes = $r.RawContentStream.ToArray()
    $txt = [System.Text.Encoding]::GetEncoding('GB18030').GetString($bytes)
    foreach ($line in ($txt -split "`n")) {
      if ($line -notmatch '=') { continue }
      $m = [regex]::Match($line, '^v_([a-z]{2}\d+)=')
      if (-not $m.Success) { continue }
      $txCode = $m.Groups[1].Value
      $parts = ($line -split '"')
      if ($parts.Count -lt 2) { continue }
      $f = $parts[1] -split '~'
      if ($f.Count -lt 47) { continue }
      $row = @{
        symbol   = Convert-Symbol $txCode
        name     = $f[1]
        price    = if ($f[3])  { [double]$f[3] }  else { $null }
        pe_ttm   = if ($f[39]) { [double]$f[39] } else { $null }
        pb       = if ($f[46]) { [double]$f[46] } else { $null }
        total_mv = if ($f[45]) { [double]$f[45] } else { $null }
        circ_mv  = if ($f[44]) { [double]$f[44] } else { $null }
        turnover = if ($f[38]) { [double]$f[38] } else { $null }
        amount   = if ($f[37]) { [double]$f[37] } else { $null }
      }
      $allRows += $row
    }
  } catch {
    Write-Host "  批次 $i 失败: $($_.Exception.Message)"
  }
  if (($i % 600) -eq 0) { Write-Host "  已拉取 $($allRows.Count) / $total" }
}
$sw.Stop()
Write-Host "拉取完成: $($allRows.Count) 条, 耗时 $([math]::Round($sw.Elapsed.TotalSeconds,1))s"

# 分批写入扩展表
$ingestBatch = 500
$written = 0
for ($i = 0; $i -lt $allRows.Count; $i += $ingestBatch) {
  $end = [Math]::Min($i + $ingestBatch - 1, $allRows.Count - 1)
  $chunk = @($allRows[$i..$end])
  $body = @{ date = (Get-Date -Format 'yyyy-MM-dd'); rows = $chunk } | ConvertTo-Json -Depth 5 -Compress
  try {
    $null = Invoke-WebRequest -Uri 'http://127.0.0.1:3018/api/ext-data/ext_valuation/ingest' -Method Post -ContentType 'application/json' -Body $body -UseBasicParsing -WebSession $sess -TimeoutSec 120
    $written += $chunk.Count
  } catch {
    Write-Host "  写入批次 $i 失败: $($_.Exception.Message)"
  }
}
Write-Host "写入完成: $written 条"
