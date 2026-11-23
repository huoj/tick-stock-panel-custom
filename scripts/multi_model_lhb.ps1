# 多模型分析：龙虎榜 + 市场环境（不使用 DeepSeek-V4-Pro）
$ErrorActionPreference = 'Continue'
$key = (Get-Content "D:\deepseek_path\tick-stock-panel\data\user_data\secrets.json" -Raw | ConvertFrom-Json).ai_api_key

$prompt = @'
请基于下面两组真实数据做投资分析。不要复述数据，直接给结论。

【数据一：2026-11-03 A股市场环境】
指数：上证 -0.95% / 深证成指 -1.19% / 创业板指 -2.75% / 科创综指 -4.92%
涨跌家数：涨 2134 / 跌 3285 / 平 150（上涨占比 38.3%）
涨停 45 / 跌停 12 / 破板 148 / 封板率仅 23.3% / 最高连板 5 板
站上 MA20 的股票仅 20.3%
创60日新高 139 只 / 创60日新低 1099 只
情绪评分 33 分（偏冷）
系统判定市场阶段：修复期 / 状态：弱势 / 评分 18

【数据二：2026-11-03 龙虎榜 机构 vs 游资 净额（万元）】
机构净买入 TOP：
  传智教育 -6.06%   机构+10702  游资0
  慈文传媒 +5.60%   机构+7927   游资-2040
  澳洋健康 +4.62%   机构+6449   游资-5276
  杰克科技 -7.44%   机构+4356   游资+14049
  奥飞娱乐 +9.99%   机构+2890   游资+1891
  德明利   -10.0%   机构+1858   游资+855

游资净买入 TOP：
  生益科技 -10.0%   游资+130745  机构-257673
  生益科技 -10.0%   游资+38404   机构-85491
  杰克科技 -7.44%   游资+14049   机构+4356
  景旺电子 -10.0%   游资+13401   机构-21093
  华正新材 -10.0%   游资+11028   机构-8021
  皇冠新材 +397.94% 游资+9936    机构0
  东方海洋 +9.97%   游资+5422    机构0
  太阳能   +10.07%  游资+5155    机构-1343

【任务】直接回答这4个问题，总长度600字以内：
1. 当前市场的核心特征是什么？（3句话内）
2. "机构与游资对立"（如生益科技游资买13亿、机构卖25.8亿）说明什么？
3. 这种环境下普通投资者最该避免做什么？（3条，要具体）
4. 有没有值得关注的机会类型？如果有请说明；如果没有请直说没有。

【输出格式】
核心特征：...
机构vs游资：...
最该避免：1)... 2)... 3)...
机会：...
'@

$models = @(
  'trae/kimi-k3',
  'trae/glm-5.3',
  'trae/qwen3.8-max',
  'minimax/MiniMax-M3',
  'raccoon/sn-sensenova-6-8-flash'
)

$results = @()
foreach ($m in $models) {
  $body = @{ model=$m; messages=@(@{role='user';content=$prompt}); max_tokens=4000 } | ConvertTo-Json -Depth 6
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8326/v1/chat/completions' -Method Post -ContentType 'application/json' -Headers @{ Authorization = "Bearer $key" } -Body $body -TimeoutSec 400
    $sw.Stop()
    $txt = [string]$r.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($txt)) {
      $txt = "[正文为空] " + [string]$r.choices[0].message.reasoning_content
    }
    $results += [pscustomobject]@{ model=$m; ok=$true; sec=[math]::Round($sw.Elapsed.TotalSeconds,1); answer=$txt }
    Write-Host "[OK] $m  $([math]::Round($sw.Elapsed.TotalSeconds,1))s  $($txt.Length)字"
  } catch {
    $sw.Stop()
    $results += [pscustomobject]@{ model=$m; ok=$false; sec=[math]::Round($sw.Elapsed.TotalSeconds,1); answer="ERR: $($_.Exception.Message)" }
    Write-Host "[ERR] $m  $($_.Exception.Message)"
  }
}
$results | Export-Csv "D:\deepseek_path\tick-stock-panel\data\logs\multi_model_lhb.csv" -NoTypeInformation -Encoding UTF8

Write-Host ''
Write-Host '################ 各模型回答 ################'
foreach ($x in $results) {
  Write-Host ''
  Write-Host "========== $($x.model) =========="
  Write-Host $x.answer
}
