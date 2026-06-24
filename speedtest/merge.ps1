<#
.SYNOPSIS
  Merge latency & download results into a weighted Markdown ranking,
  with exponentially-forgetting history (EWMA) across runs.
.DESCRIPTION
  每次运行把本轮结果累积进 out\history.json:
  对每个节点的【原始物理量】(延迟 ms / 下载 MB/s) 做指数加权移动平均(EWMA),
  新一轮权重 (1-Alpha), 历史权重 Alpha, 越久越淡忘。
  - 排名用"平滑后的延迟", 不直接平滑名次/归一化分 —— 那是同批相对量, 跨批不可比
    (rank 5/30 != 5/75; min-max 每轮重置), EMA 这种量会混淆尺度。
  - 额外给出 EWMA 波动 σ(稳定性): 时好时坏的节点(如沪港IEPL)会被 σ 暴露。
  - 群体漂移: 本轮未出现的节点【冻结】其 EMA(不因一次线路被掐而打成 0),
    只记"距今几轮"陈旧度, 单列到次表; 主名次仅取本轮存活节点。
  - 幂等: 源文件 mtime 未变则不二次计入, 重渲染报告不污染统计。
#>
param(
    # 仅重渲染报告, 不把本次结果计入历史(调权重/补看时用)
    [switch]$NoAccumulate,
    # 清历史(可分维度): latency 只清延迟 / download 只清下载 / all 全清。一般由 speedtest -Reset 转发
    [ValidateSet('latency', 'download', 'all')]
    [string]$Reset
)
$ErrorActionPreference = "Stop"

# ================= 调参常量(改这里, 不再走 flag) =================
$Alpha                = 0.7   # 延迟遗忘因子(历史权重, 越大越长记性; 半衰期 ln.5/ln α)
$AlphaDownload        = 0.5   # 下载遗忘更快(带宽时变, 只信最近)
$LatencyWeight        = 1.0
$DownloadWeight       = 0.0   # 0 = 下载只展示不入排名
$SigmaPenalty         = 1.0   # 延迟 波动惩罚 k: 有效值 + k·σ
$SigmaPenaltyDownload = 1.0   # 下载 k
$AvailPenalty         = 1.0   # 延迟 可用率惩罚 p: ÷ avail^p
$AvailPenaltyDownload = 2.0   # 下载 p (不通惩罚更高)

$LatencyFile = "$PSScriptRoot\out\result_latency.txt"
$DownloadFile = "$PSScriptRoot\out\result_download.txt"
$HistoryFile = "$PSScriptRoot\out\history.json"
$OutputFile = "$PSScriptRoot\result_combined.md"

# clash-speedtest 管道输出为 TSV: 表头含 序号/节点名称/类型/延迟/(下载速度...)
# 按表头定位列, 不依赖列顺序
function Read-Tsv {
    param($Path)
    if (-not (Test-Path $Path)) { return @() }
    $lines = @(Get-Content $Path -Encoding UTF8 | Where-Object { $_ -match "`t" })
    if ($lines.Count -lt 2) { return @() }
    $header = $lines[0] -split "`t" | ForEach-Object { $_.Trim() }
    foreach ($line in $lines | Select-Object -Skip 1) {
        $cells = $line -split "`t"
        $row = @{}
        for ($i = 0; $i -lt $header.Count -and $i -lt $cells.Count; $i++) {
            $row[$header[$i]] = $cells[$i].Trim()
        }
        $row
    }
}

function Get-Cell {
    param($Row, $HeaderPattern)
    $key = @($Row.Keys | Where-Object { $_ -match $HeaderPattern })
    if ($key) { return $Row[$key[0]] }
    return $null
}

# EWMA 均值+增量方差(West/Finch 指数加权形式) 就地更新一个节点记录的某指标
#   $Prefix: 'lat' 或 'dl'; 首次观测用首值初始化(无零偏), σ²=0
function Update-Ewma($Rec, [string]$Prefix, [double]$X, [double]$A, [int]$RunIdx) {
    $eK = "${Prefix}_ema"; $vK = "${Prefix}_var"; $nK = "${Prefix}_n"
    $lK = "${Prefix}_last"; $sK = "${Prefix}_seen"; $fK = "${Prefix}_first"
    if (-not $Rec.ContainsKey($nK) -or [int]$Rec[$nK] -lt 1) {
        $Rec[$eK] = $X; $Rec[$vK] = 0.0; $Rec[$nK] = 1
        $Rec[$fK] = $RunIdx                       # 首见轮号(算可用率分母用)
    }
    else {
        # 迁移: 老历史缺 first, 按"自首见起每轮都在"回填(无 gap 史 -> 暂记满可用)
        if (-not $Rec.ContainsKey($fK)) { $Rec[$fK] = $RunIdx - [int]$Rec[$nK] }
        $mu = [double]$Rec[$eK]
        $delta = $X - $mu
        $incr = (1 - $A) * $delta
        $Rec[$eK] = $mu + $incr
        $Rec[$vK] = $A * ([double]$Rec[$vK] + $delta * $incr)
        $Rec[$nK] = [int]$Rec[$nK] + 1
    }
    $Rec[$lK] = $X
    $Rec[$sK] = $RunIdx
}

# 可用率 = 成功计入轮数 / 自首见起的生命期轮数(按各维度自己的 run 计数, 非总轮数)
function Get-Avail($Rec, [string]$Prefix, [int]$RunNow) {
    $nK = "${Prefix}_n"; $fK = "${Prefix}_first"
    if (-not $Rec.ContainsKey($nK) -or [int]$Rec[$nK] -lt 1) { return $null }
    $n = [int]$Rec[$nK]
    $first = if ($Rec.ContainsKey($fK)) { [int]$Rec[$fK] } else { $RunNow - $n + 1 }
    $life = [math]::Max(1, $RunNow - $first + 1)
    [math]::Min(1.0, $n / $life)
}

# 有效值: 把 EMA + σ波动 + 可用率 合成一个"惩罚后"的分; 高/低优两个方向通用
#   低优(延迟): (EMA + k·σ) ÷ avail^p  —— σ大/常断 → 值变大 → 更差
#   高优(下载): (EMA − k·σ) × avail^p  —— σ大/常断 → 值变小 → 更差
function Get-Eff([double]$Ema, $Sigma, $Avail, [double]$K, [double]$P, [bool]$HigherBetter) {
    $s = if ($null -eq $Sigma) { 0.0 } else { [double]$Sigma }
    $a = if ($null -eq $Avail) { 1.0 } else { [double]$Avail }
    if ($HigherBetter) {
        $v = [math]::Max(0.0, $Ema - $K * $s)
        if ($P -gt 0 -and $a -gt 0) { $v = $v * [math]::Pow($a, $P) }
    }
    else {
        $v = $Ema + $K * $s
        if ($P -gt 0 -and $a -gt 0) { $v = $v / [math]::Pow($a, $P) }
    }
    $v
}

# ================= 读取本轮原始测量 =================
$latencyMap = @{}
foreach ($row in Read-Tsv $LatencyFile) {
    $name = Get-Cell $row '节点'
    $lat = Get-Cell $row '延迟'
    if ($name -and $lat -match '(\d+(?:\.\d+)?)\s*ms') {
        $latencyMap[$name] = [double]$Matches[1]
    }
}

$downloadMap = @{}
foreach ($row in Read-Tsv $DownloadFile) {
    $name = Get-Cell $row '节点'
    $speed = Get-Cell $row '下载'
    if ($name -and $speed -match '(\d+(?:\.\d+)?)\s*(GB|MB|KB)/s') {
        $mbps = [double]$Matches[1]
        switch ($Matches[2]) {
            'GB' { $mbps *= 1024 }
            'KB' { $mbps /= 1024 }
        }
        $downloadMap[$name] = $mbps
    }
}

# ================= 载入 / 更新 EWMA 历史 =================
$meta = @{ run = 0; dl_run = 0; lat_mtime = 0; dl_mtime = 0 }
$hist = @{}
if (Test-Path $HistoryFile) {
    try {
        $h = Get-Content $HistoryFile -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
        if ($h.meta) { $meta = $h.meta }
        if ($h.nodes) { $hist = $h.nodes }
    }
    catch {
        Write-Warning "history.json 解析失败, 重新开始累积: $($_.Exception.Message)"
    }
}
# 迁移: 老历史无 dl_run。已计过下载则视作已有 1 轮, 否则 0
if (-not $meta.ContainsKey('dl_run')) {
    $meta.dl_run = if ([long]$meta.dl_mtime -ne 0) { 1 } else { 0 }
}

# 清历史(可分维度): all 全清 / latency 只清 lat_* / download 只清 dl_*
if ($Reset -eq 'all') {
    $meta = @{ run = 0; dl_run = 0; lat_mtime = 0; dl_mtime = 0 }; $hist = @{}
    Write-Host "[-Reset all] 清空全部历史" -ForegroundColor Yellow
}
elseif ($Reset -in 'latency', 'download') {
    $pfx = if ($Reset -eq 'latency') { 'lat_' } else { 'dl_' }
    foreach ($name in @($hist.Keys)) {
        $rec = $hist[$name]
        @($rec.Keys) | Where-Object { $_ -like "$pfx*" } | ForEach-Object { $rec.Remove($_) }
        if ($rec.Count -eq 0) { $hist.Remove($name) }   # 该节点两维都空 -> 删
    }
    if ($Reset -eq 'latency') { $meta.run = 0; $meta.lat_mtime = 0 }
    else { $meta.dl_run = 0; $meta.dl_mtime = 0 }
    Write-Host "[-Reset $Reset] 清空${Reset}历史(保留另一维)" -ForegroundColor Yellow
}

# 指纹用 mtime 的原始 ticks(int64): JSON 精确往返, 无格式化/时区/Kind 歧义
function Get-Stamp($Path) {
    if (Test-Path $Path) { (Get-Item $Path).LastWriteTimeUtc.Ticks } else { [long]0 }
}
$latStamp = Get-Stamp $LatencyFile
$dlStamp = Get-Stamp $DownloadFile
# mtime 守卫: 同一份测量不重复计入(否则 EMA 会对同一值反复收敛, 虚高置信度)
$latNew = $latencyMap.Count -gt 0 -and $latStamp -ne [long]$meta.lat_mtime
$dlNew = $downloadMap.Count -gt 0 -and $dlStamp -ne [long]$meta.dl_mtime

if (-not $NoAccumulate) {
    if ($latNew) {
        $meta.run = [int]$meta.run + 1          # 一轮延迟测量 = 一个时间步
        foreach ($name in $latencyMap.Keys) {
            if (-not $hist.ContainsKey($name)) { $hist[$name] = @{} }
            Update-Ewma $hist[$name] 'lat' $latencyMap[$name] $Alpha $meta.run
        }
        $meta.lat_mtime = $latStamp
    }
    if ($dlNew) {
        $meta.dl_run = [int]$meta.dl_run + 1      # 一轮下载测量 = 下载维度一个时间步(与延迟轮独立)
        foreach ($name in $downloadMap.Keys) {
            if (-not $hist.ContainsKey($name)) { $hist[$name] = @{} }
            Update-Ewma $hist[$name] 'dl' $downloadMap[$name] $AlphaDownload $meta.dl_run
        }
        $meta.dl_mtime = $dlStamp
    }
    [IO.File]::WriteAllText($HistoryFile,
        (@{ meta = $meta; nodes = $hist } | ConvertTo-Json -Depth 6),
        [System.Text.UTF8Encoding]::new($false))
}

# ================= 排名: 本轮存活节点 =================
# 主名次仅取本轮存活(当前可选)节点; 延迟=排名键。下载并行算同款 EMA/σ/可用率(权重默认0, 仅展示)
$rows = foreach ($name in $latencyMap.Keys) {
    $rec = if ($hist.ContainsKey($name)) { $hist[$name] } else { @{ lat_ema = $latencyMap[$name]; lat_var = 0.0; lat_n = 1 } }
    [pscustomobject]@{
        Name   = $name
        EmaLat = [double]$rec.lat_ema
        SigLat = if ([int]$rec.lat_n -ge 2) { [math]::Sqrt([double]$rec.lat_var) } else { $null }
        AvLat  = Get-Avail $rec 'lat' ([int]$meta.run)
        NLat   = [int]$rec.lat_n
        EmaDl  = if ($rec.ContainsKey('dl_ema')) { [double]$rec.dl_ema } else { $null }
        SigDl  = if ($rec.ContainsKey('dl_n') -and [int]$rec.dl_n -ge 2) { [math]::Sqrt([double]$rec.dl_var) } else { $null }
        AvDl   = Get-Avail $rec 'dl' ([int]$meta.dl_run)
        NDl    = if ($rec.ContainsKey('dl_n')) { [int]$rec.dl_n } else { 0 }
        EffLat = 0.0
        EffDl  = $null
        Score  = 0.0
    }
}
$rows = @($rows)

if ($rows.Count -gt 0) {
    foreach ($r in $rows) {
        $r.EffLat = Get-Eff $r.EmaLat $r.SigLat $r.AvLat $SigmaPenalty $AvailPenalty $false
        if ($null -ne $r.EmaDl) {
            $r.EffDl = Get-Eff $r.EmaDl $r.SigDl $r.AvDl $SigmaPenaltyDownload $AvailPenaltyDownload $true
        }
    }
    # min-max 归一化: 延迟(有效值越低越好) / 下载(有效值越高越好); 缺下载的节点 download 分记 0
    $latMin = ($rows.EffLat | Measure-Object -Minimum).Minimum
    $latMax = ($rows.EffLat | Measure-Object -Maximum).Maximum
    $effDls = @($rows | Where-Object { $null -ne $_.EffDl } | ForEach-Object { $_.EffDl })
    $dlMin = if ($effDls.Count) { ($effDls | Measure-Object -Minimum).Minimum } else { 0 }
    $dlMax = if ($effDls.Count) { ($effDls | Measure-Object -Maximum).Maximum } else { 0 }

    foreach ($r in $rows) {
        $latScore = if ($latMax -eq $latMin) { 1 } else { ($latMax - $r.EffLat) / ($latMax - $latMin) }
        $wD = if ($null -eq $r.EffDl) { 0 } else { $DownloadWeight }
        $dlScore = if ($wD -eq 0 -or $dlMax -eq $dlMin) { 0 } else { ($r.EffDl - $dlMin) / ($dlMax - $dlMin) }
        $denom = [math]::Max(1e-9, $LatencyWeight + $wD)
        $r.Score = [math]::Round(($LatencyWeight * $latScore + $wD * $dlScore) / $denom, 4)
    }
    $rows = @($rows | Sort-Object Score -Descending)
}

# ================= 次表: 历史有记录但本轮未出现(陈旧) =================
$absent = foreach ($name in $hist.Keys) {
    if ($latencyMap.ContainsKey($name)) { continue }
    $rec = $hist[$name]
    if (-not $rec.ContainsKey('lat_ema')) { continue }
    [pscustomobject]@{
        Name   = $name
        EmaLat = [double]$rec.lat_ema
        Nobs   = [int]$rec.lat_n
        Stale  = [int]$meta.run - [int]$rec.lat_seen
    }
}
$absent = @($absent | Sort-Object EmaLat)

# ================= 渲染 Markdown =================
$hlLat = if ($Alpha -le 0) { 0 } else { [math]::Round([math]::Log(0.5) / [math]::Log($Alpha), 1) }
$hlDl = if ($AlphaDownload -le 0) { 0 } else { [math]::Round([math]::Log(0.5) / [math]::Log($AlphaDownload), 1) }
$ingest = @()
if ($latNew -and -not $NoAccumulate) { $ingest += "latency" }
if ($dlNew -and -not $NoAccumulate) { $ingest += "download" }
$ingestNote = if ($NoAccumulate) { "未累积(-NoAccumulate)" }
elseif ($ingest.Count) { "本轮计入: $($ingest -join ', ')" }
else { "源文件未变, 本轮未计入(幂等)" }

# LaTeX 公式线(单引号防插值) + 参数行(用 [char]36='$' 避 backtick-escape 坑: `a=bell)
$gen = if (Test-Path $LatencyFile) { (Get-Item $LatencyFile).LastWriteTime.ToString("yyyy-MM-dd HH:mm") } else { "-" }
$S = [char]36
$Formula = "${S}${S}\mu_t = \alpha\mu_{t-1} + (1-\alpha)\ell_t\qquad L_{\text{ref}} = \frac{\mu + k\sigma}{a^p}${S}${S}"

$md = @"
# Claude Proxy Speedtest Result
**$($meta.run) 轮延迟** · **$($meta.dl_run) 轮下载** · **$($latencyMap.Count) 存活** · **$($hist.Count) 历史** · $gen　　　　 *v1/messages* (haiku) · *downloads.claude.ai* 100MB

$Formula

| # | Node | L_ref | μ | σ | a% | D_eff | μ_D | σ_D | a_D% | n | Score |
|--:|------|-----:|--:|--:|---:|-----:|----:|----:|----:|:-:|------:|
"@

$rank = 1
foreach ($r in $rows) {
    $sigL = if ($null -eq $r.SigLat) { "-" } else { "{0:N0}" -f $r.SigLat }
    $avL = if ($null -eq $r.AvLat) { "-" } else { "{0:P0}" -f $r.AvLat }
    $effD = if ($null -eq $r.EffDl) { "-" } else { "{0:N2}" -f $r.EffDl }
    $emaD = if ($null -eq $r.EmaDl) { "-" } else { "{0:N2}" -f $r.EmaDl }
    $sigD = if ($null -eq $r.SigDl) { "-" } else { "{0:N2}" -f $r.SigDl }
    $avD = if ($null -eq $r.AvDl) { "-" } else { "{0:P0}" -f $r.AvDl }
    $md += "`n| $rank | $($r.Name) | $("{0:N0}ms" -f $r.EffLat) | $("{0:N0}" -f $r.EmaLat) | $sigL | $avL | $effD | $emaD | $sigD | $avD | $($r.NLat)/$($r.NDl) | $("{0:N4}" -f $r.Score) |"
    $rank++
}
if ($rows.Count -eq 0) {
    $md += "`n| - | (no alive node this run) |" + (" - |" * 10)
}

if ($absent.Count -gt 0) {
    $md += "`n`n### 历史良好但本轮未出现 (frozen EMA)`n`n"
    $md += "| Node | EMA延迟 | 样本 | 距今 |`n"
    $md += "|------|-------:|----:|-----:|"
    foreach ($a in $absent) {
        $md += "`n| $($a.Name) | $("{0:N0} ms" -f $a.EmaLat) | $($a.Nobs) | $($a.Stale) 轮前 |"
    }
}

[IO.File]::WriteAllText($OutputFile, $md + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "Merged -> $OutputFile  (run #$($meta.run), $($rows.Count) ranked, $($absent.Count) stale)  [$ingestNote]" -ForegroundColor Green
