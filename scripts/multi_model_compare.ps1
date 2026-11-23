# 多模型对比：让不同厂商的大模型分析同一批股票数据
$ErrorActionPreference = 'Continue'
$key = (Get-Content "D:\deepseek_path\tick-stock-panel\data\user_data\secrets.json" -Raw | ConvertFrom-Json).ai_api_key

$prompt = @'
以下是7只A股的全部客观数据（2026-11-03收盘）。请不要问我要更多数据，直接基于这些数据判断。

【财务】毛利率 / ROE / 净利润同比 / 经营现金流
【技术】均线位置 / RSI14 / 距MA20乖离 / 换手率全市场分位 / 近期涨停次数

1. 长城汽车(601633) 价14.35
   财务：毛利率17.98% ROE4.32% 净利同比-55.69% 现金流+62亿
   技术：站上MA20 RSI54.5 距MA20+2.7% 换手率分位1.3% 近期无涨停

2. 东方财富(300059) 价18.88
   财务：毛利率86% ROE12.28% 净利同比+30.89% 现金流+222亿
   技术：站上MA20 RSI58.3 距MA20+4.5% 换手率分位60% 成交额分位99.7%

3. 水井坊(600779) 价28.81
   财务：毛利率78% ROE4.14% 净利同比-34.3% 现金流+5.9亿
   技术：站上MA20 RSI66.1 距MA20+9.8% 换手率分位79% 20日涨停2次 量比5.05

4. 美的集团(000333) 价89.40
   财务：毛利率25.46% ROE18.06% 净利同比+9.27% 现金流+402亿
   技术：站上MA20 RSI64.0 距MA20+5.2% 换手率分位4.3% 近期无涨停

5. 格力电器(000651) 价39.17
   财务：毛利率27% ROE11.43% 净利同比-19.5% 现金流+241亿
   技术：站上MA20 RSI57.8 距MA20+1.9% 换手率分位13.5% 近期无涨停

6. 宏柏新材(605366) 价6.71
   财务：毛利率3.42% ROE-3.23% 净利同比-10% 现金流-0.0亿
   技术：跌破全部均线 RSI35.6 距MA20-18.8% 换手率分位63% 60日涨停3次

7. 中国移动(600941) 价104.00
   财务：毛利率29.1% ROE7.5% 净利同比-7.94% 现金流+1853亿
   技术：站上MA20 RSI65.9 距MA20+3.8% 换手率分位45% 近期无涨停

【任务】
按"未来3个月投资吸引力"从高到低排序这7只股票。
对每只给一句判断（20字以内）。
最后指出你认为哪只风险最大。

【输出格式】严格按此格式，不要额外解释：
排序：1.名称 2.名称 3.名称 ... 7.名称
判断：
- 名称：一句话
（7行）
风险最大：名称 + 理由（一句话）
'@

$models = @(
  'deepseek-account/deepseek-v4-pro',
  'trae/kimi-k3',
  'trae/glm-5.3',
  'trae/qwen3.8-max',
  'minimax/MiniMax-M3',
  'gemini/gemini-3.8-flash'
)

$results = @()
foreach ($m in $models) {
  $body = @{ model=$m; messages=@(@{role='user';content=$prompt}); max_tokens=3000 } | ConvertTo-Json -Depth 6
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8326/v1/chat/completions' -Method Post -ContentType 'application/json' -Headers @{ Authorization = "Bearer $key" } -Body $body -TimeoutSec 300
    $sw.Stop()
    $content = [string]$r.choices[0].message.content
    $results += [pscustomobject]@{ model=$m; ms=$sw.ElapsedMilliseconds; ok=$true; answer=$content }
    Write-Host "[OK] $m  ($([math]::Round($sw.Elapsed.TotalSeconds,1))s, $($content.Length) 字)"
  } catch {
    $sw.Stop()
    $results += [pscustomobject]@{ model=$m; ms=$sw.ElapsedMilliseconds; ok=$false; answer="ERR: $($_.Exception.Message)" }
    Write-Host "[ERR] $m : $($_.Exception.Message)"
  }
}
$results | Export-Csv "D:\deepseek_path\tick-stock-panel\data\logs\multi_model.csv" -NoTypeInformation -Encoding UTF8
Write-Host ''
Write-Host '===== 各模型回答 ====='
foreach ($x in $results) {
  Write-Host ''
  Write-Host "########## $($x.model) ##########"
  Write-Host $x.answer
}
