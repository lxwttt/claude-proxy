param(
    # ================= 默认 Mode 设置处 =================
    # all = 延迟(握手+真实消息) + 下载 | latency = 仅延迟 | download = 仅下载
    [ValidateSet("all", "latency", "download")]
    [string]$Mode = "all",
    # 抛弃历史(可分维度): 删对应结果文件 + 转发 merge 清历史。all=全清, latency/download=只清该维
    [ValidateSet("latency", "download", "all")]
    [string]$Reset,
    # 每节点真实消息采样次数(latency 阶段); 次数越多越稳, 但更耗 OAuth 额度/时间
    [ValidateRange(1, 99)]
    [int]$Samples = 3
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

# ================= 路径 =================
$InputFile = "$PSScriptRoot\in\input.yaml"    # 订阅配置(每次测速前从 Verge profile 同步)
$FilterFile = "$PSScriptRoot\in\filter.txt"    # 节点筛选正则(.NET 语法, 支持 lookahead)
$OutDir = "$PSScriptRoot\out"              # 所有生成物
$TestConfig = "$OutDir\_test.yaml"             # 派生测试配置(筛选+绑网卡)
$OutHandshake = "$OutDir\result_handshake.txt"   # 阶段1: fast 握手探活原始输出
$OutLatency = "$OutDir\result_latency.txt"     # 阶段2: 真实消息结果(merge 以此为延迟)
$OutDownload = "$OutDir\result_download.txt"

# ================= 同步订阅: 每次测速前从 Verge 当前 profile 刷新 input.yaml =================
$ProfileSource = "$env:APPDATA\io.github.clash-verge-rev.clash-verge-rev\profiles\RlbasAQdQ4q5.yaml"
if (Test-Path $ProfileSource) {
    Copy-Item $ProfileSource $InputFile -Force
    Write-Host "Synced input.yaml <- Verge profile ($(Split-Path $ProfileSource -Leaf))" -ForegroundColor DarkGray
}
else {
    Write-Warning "Verge profile 不存在, 沿用现有 input.yaml: $ProfileSource"
}

# 抛弃历史: 删对应维度结果文件(免旧数据被 merge 误计), 累积态由末尾 merge -Reset 清
if ($Reset) {
    $toClear = switch ($Reset) {
        'all' { $OutHandshake, $OutLatency, $OutDownload }
        'latency' { $OutHandshake, $OutLatency }
        'download' { $OutDownload }
    }
    $toClear | Where-Object { Test-Path $_ } | ForEach-Object { Remove-Item $_ -Force }
    Write-Host "[-Reset $Reset] 已删原始结果文件" -ForegroundColor Yellow
}

# ================= 延迟测试配置 =================
# 阶段1 握手探活: fast 模式对 server-url 发 6 次 HEAD 取延迟, 任意 HTTP 状态码均计入。
# 必须带路径: 裸域名会被当作测速服务器模式拼 /__down, 实测全部 N/A。
# 探活通过 != 真实可用(沪港线曾按 TLS 指纹掐 OpenSSL/Schannel 内层握手而 Go 探活全绿),
# 因此存活节点还要过阶段2 真实消息测试, 以其首字节延迟作为最终延迟
$HandshakeUrl = "https://api.anthropic.com/v1/models"

# 阶段2 真实消息: 官方 OAuth 格式流式 /v1/messages, stream 下首字节时间 ≈ 首 token 延迟。
# 订阅 OAuth 必须: Bearer + anthropic-beta: oauth-2025-04-20 + Claude Code 系统提示词首块
$CredFile = "$env:USERPROFILE\.claude\.credentials.json"
$Mihomo = "D:\Program Files\Clash Verge\verge-mihomo.exe"
$MixedPort = 18897    # 隔离端口, 不碰线上 Verge 的 7897/9097
$CtrlPort = 18898
$ApiUrl = "https://api.anthropic.com/v1/messages"
$Model = "claude-haiku-4-5"
$MaxTokens = 16
$MsgSamples = $Samples # 每节点采样次数(由 -Samples 控制)
# 429 限流(账号级: 整测共用一个 OAuth token, 切节点不重置额度): 退避重试 + 自适应降频
$Max429Retry = 3    # 单次探测遇 429 的最大延时重试次数
$Backoff429Base = 5    # 退避基数秒(指数 5→10→20)
$Backoff429Max = 30   # 单次退避上限秒
$ProbeDelayMs = 300  # 探测间隔基线(自适应起点)
$PaceStep = 500  # AIMD: 遇 429 探测间隔的增量(ms)
$PaceMax = 3000 # 探测间隔上限(ms)
# 指标拆分: 排名用"网络延迟"(time_appconnect = 端到端 TLS 建链, 纯路径耗时, 取最小值
# 滤掉服务端排队毛刺); 首token(TTFB)与服务端耗时(TTFB-建链)取中位数仅作参考列。
# 不用 总耗时/token: 逐 token 生成速度是 Anthropic 服务端属性, 节点不可控, 会稀释节点信号

# ================= 下载测试配置 =================
$DownloadUrl = "https://downloads.claude.ai/vms/linux/x64/c9b42670eaedf20c7035b018c904a0c6a3cb864f/rootfs.vhdx.zst"
$DownloadSize = 104857600
$Timeout = "30s"
$Concurrent = 4

# 本机 Clash Verge TUN(Meta 网卡, 默认路由 metric 0)会把测速流量劫持进自己的隧道,
# 节点必须绑定物理网卡直连出站; 置空则不注入
$OutboundInterface = "WLAN"

# ================= Self-check =================
if (-not (Get-Command clash-speedtest -ErrorAction SilentlyContinue)) {
    throw "clash-speedtest not found in PATH"
}
foreach ($f in @($InputFile, $FilterFile)) {
    if (-not (Test-Path $f)) { throw "not found: $f" }
}
if ($Mode -ne "download") {
    foreach ($f in @($Mihomo, $CredFile)) {
        if (-not (Test-Path $f)) { throw "not found: $f" }
    }
    $oauth = (Get-Content $CredFile -Raw | ConvertFrom-Json).claudeAiOauth
    if ([DateTimeOffset]::FromUnixTimeMilliseconds($oauth.expiresAt) -le [DateTimeOffset]::UtcNow) {
        throw "OAuth token expired - open Claude Code once to refresh credentials.json"
    }
}
New-Item -ItemType Directory -Force $OutDir | Out-Null

function Get-Median([double[]]$v) {
    if (-not $v) { return $null }
    $s = $v | Sort-Object; $n = $s.Count
    if ($n % 2) { $s[[int][math]::Floor($n / 2)] } else { ($s[$n / 2 - 1] + $s[$n / 2]) / 2 }
}

# 单行实时进度: 交互式用 \r 原地刷新, 按显示宽度截断/补满(中文等全角算 2 列, 防折行串行);
# 输出重定向(后台任务/日志)时静默, 不污染落盘内容
function Show-Progress([string]$Activity, [int]$Done, [int]$Total, [string]$Name) {
    if ([Console]::IsOutputRedirected) { return }
    $cols = [Console]::WindowWidth - 1
    if ($cols -lt 10) { $cols = 10 }
    $text = "[$Done/$Total] $Activity  $Name"
    $w = 0; $out = [Text.StringBuilder]::new()
    foreach ($ch in $text.ToCharArray()) {
        $code = [int]$ch
        $cw = if (($code -ge 0x1100 -and $code -le 0x115F) -or `
            ($code -ge 0x2E80 -and $code -le 0xA4CF) -or `
            ($code -ge 0xAC00 -and $code -le 0xD7A3) -or `
            ($code -ge 0xF900 -and $code -le 0xFAFF) -or `
            ($code -ge 0xFE30 -and $code -le 0xFE4F) -or `
            ($code -ge 0xFF00 -and $code -le 0xFF60) -or `
            ($code -ge 0xFFE0 -and $code -le 0xFFE6)) { 2 } else { 1 }
        if ($w + $cw -gt $cols) { break }
        [void]$out.Append($ch); $w += $cw
    }
    if ($w -lt $cols) { [void]$out.Append(' ' * ($cols - $w)) }
    [Console]::Write("`r" + $out.ToString())
}

# 收尾: 清掉进度行并把光标归位(替代 Write-Progress -Completed)
function Clear-ProgressLine {
    if ([Console]::IsOutputRedirected) { return }
    $cols = [Console]::WindowWidth - 1
    if ($cols -lt 1) { $cols = 1 }
    [Console]::Write("`r" + (' ' * $cols) + "`r")
}

# ================= Filter & generate test config =================
# filter.txt 是 .NET 正则(支持 lookahead), Go 侧 RE2 不支持, 因此在这里预筛选,
# 把命中的节点写入派生配置 _test.yaml, 不把正则传给 clash-speedtest -f
$filter = (Get-Content $FilterFile -Raw -Encoding UTF8).Trim()
$yaml = Get-Content $InputFile -Raw -Encoding UTF8

$selected = foreach ($m in [regex]::Matches($yaml, '-\s*\{[^}]+\}')) {
    $block = $m.Value
    # profile 用单引号(name: '...'), 老 input.yaml 用双引号 —— 两种都收
    if ($block -match "name:\s*['`"]([^'`"]+)['`"]" -and $Matches[1] -match $filter) {
        if ($OutboundInterface) {
            # flow 式开头插入(逗号正确, 兼容单行/多行); 替行尾 } 在单行上会漏逗号
            $block = $block -replace '^-\s*\{', "- { interface-name: `"$OutboundInterface`","
        }
        $block
    }
}

if (-not $selected) {
    throw "no proxies matched filter: $filter"
}
Write-Host "Found $($selected.Count) proxies matching filter" -ForegroundColor Cyan

$lines = @("proxies:") + ($selected | ForEach-Object { "  $_" })
[IO.File]::WriteAllText($TestConfig, ($lines -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))

# ================= Latency Test =================
if ($Mode -ne "download") {
    # ---- 阶段1: 握手探活 ----
    Write-Host "[Latency 1/2 handshake] $HandshakeUrl" -ForegroundColor Cyan

    # 流式接管 clash-speedtest 输出: 数据行(以"N."开头)只更新单行进度, 全文落盘
    $hsLines = [Collections.Generic.List[string]]::new()
    $hsDone = 0
    clash-speedtest -c $TestConfig --speed-mode fast --server-url $HandshakeUrl --concurrent $Concurrent `
    | ForEach-Object {
        $hsLines.Add($_)
        $c = $_ -split "`t"
        if ($c.Count -ge 4 -and $c[0] -match '^\d') {
            $hsDone++
            Show-Progress "Latency 1/2 handshake" $hsDone $selected.Count $c[1]
        }
    }
    Clear-ProgressLine
    [IO.File]::WriteAllText($OutHandshake, ($hsLines -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))

    if ($LASTEXITCODE -ne 0) {
        Remove-Item $OutHandshake -Force -ErrorAction SilentlyContinue
        throw "handshake test failed (exit $LASTEXITCODE)"
    }

    # TSV: 序号 / 节点名称 / 类型 / 延迟
    $alive = @(foreach ($line in Get-Content $OutHandshake -Encoding UTF8) {
            $c = $line -split "`t"
            if ($c.Count -ge 4 -and $c[3] -match '^([\d.]+)\s*ms$') {
                [pscustomobject]@{ Name = $c[1]; Lat = [double]$Matches[1] }
            }
        }) | Sort-Object Lat
    Write-Host "Handshake alive: $($alive.Count)/$($selected.Count)" -ForegroundColor Green

    if (-not $alive) {
        [IO.File]::WriteAllText($OutLatency, "序号`t节点名称`t延迟`t总耗时`t成功`t状态码`n", [System.Text.UTF8Encoding]::new($false))
        Write-Host "no node alive, skip message test" -ForegroundColor Yellow
    }
    else {
        # ---- 阶段2: 真实消息测试 ----
        Write-Host "[Latency 2/2 messages] $($alive.Count) nodes x $MsgSamples ($Model, max_tokens=$MaxTokens)" -ForegroundColor Cyan

        $tmp = Join-Path $env:TEMP "claude-msgtest"
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        New-Item -ItemType Directory $tmp | Out-Null

        $groupList = ($alive | ForEach-Object { "      - `"$($_.Name)`"" }) -join "`n"
        $cfg = @"
$(Get-Content $TestConfig -Raw -Encoding UTF8)
mixed-port: $MixedPort
allow-lan: false
external-controller: 127.0.0.1:$CtrlPort
log-level: warning
mode: rule
proxy-groups:
  - name: TEST
    type: select
    proxies:
$groupList
rules:
  - MATCH,TEST
"@
        [IO.File]::WriteAllText("$tmp\config.yaml", $cfg, [System.Text.UTF8Encoding]::new($false))

        # token 写入临时头文件而非命令行, 避免进程列表泄露
        [IO.File]::WriteAllText("$tmp\headers.txt", @(
                "Authorization: Bearer $($oauth.accessToken)"
                "anthropic-version: 2023-06-01"
                "anthropic-beta: oauth-2025-04-20"
                "content-type: application/json"
            ) -join "`n", [System.Text.UTF8Encoding]::new($false))

        $bodyJson = @{
            model      = $Model
            max_tokens = $MaxTokens
            stream     = $true
            system     = @(@{ type = "text"; text = "You are Claude Code, Anthropic's official CLI for Claude." })
            messages   = @(@{ role = "user"; content = "hi" })
        } | ConvertTo-Json -Depth 5 -Compress
        [IO.File]::WriteAllText("$tmp\body.json", $bodyJson, [System.Text.UTF8Encoding]::new($false))

        $ctrl = "http://127.0.0.1:$CtrlPort"
        $proc = Start-Process -FilePath $Mihomo -ArgumentList @("-d", $tmp, "-f", "$tmp\config.yaml") -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput "$tmp\mihomo.log" -RedirectStandardError "$tmp\mihomo.err"
        try {
            $up = $false
            foreach ($i in 1..40) {
                try { Invoke-RestMethod "$ctrl/version" -TimeoutSec 1 | Out-Null; $up = $true; break }
                catch { Start-Sleep -Milliseconds 250 }
            }
            if (-not $up) { throw "mihomo controller not responding on :$CtrlPort" }

            $idx = 0
            $pace = $ProbeDelayMs   # 自适应探测间隔(AIMD: 遇429加大, 顺畅则回落), 跨节点累积
            $results = foreach ($node in $alive.Name) {
                $idx++
                Show-Progress "Latency 2/2 messages" $idx $alive.Count $node
                $sel = [Text.Encoding]::UTF8.GetBytes((@{ name = $node } | ConvertTo-Json -Compress))
                Invoke-RestMethod -Method Put -Uri "$ctrl/proxies/TEST" -Body $sel -ContentType "application/json" | Out-Null

                # 注意: PowerShell 变量不区分大小写, 命名避开参数/配置变量
                $saw429 = $false
                $probes = foreach ($n in 1..$MsgSamples) {
                    # 429=账号级限流(非节点问题): 指数退避重试 + 加大全局间隔, 不把它计入样本
                    $attempt = 0
                    do {
                        $retry = $false
                        $out = & curl.exe -s -o NUL -x "http://127.0.0.1:$MixedPort" --max-time 30 `
                            -w "%{http_code} %{time_appconnect} %{time_starttransfer} %{time_total}" `
                            -H "@$tmp\headers.txt" --data-binary "@$tmp\body.json" $ApiUrl
                        if ("$out" -match '(\d{3})\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$') {
                            $code = $Matches[1]
                            if ($code -eq "429" -and $attempt -lt $Max429Retry) {
                                $saw429 = $true
                                $wait = [math]::Min($Backoff429Max, $Backoff429Base * [math]::Pow(2, $attempt))
                                $pace = [math]::Min($PaceMax, $pace + $PaceStep)   # 主动降频
                                Clear-ProgressLine
                                Write-Host ("  [429] {0}  限流退避 {1}s (重试 {2}/{3}, 间隔↑{4}ms)" -f `
                                        $node, $wait, ($attempt + 1), $Max429Retry, $pace) -ForegroundColor DarkYellow
                                Start-Sleep -Seconds $wait
                                $attempt++; $retry = $true
                            }
                            else {
                                [pscustomobject]@{
                                    Code = $code; Net = [double]$Matches[2] * 1000
                                    Ttfb = [double]$Matches[3] * 1000; Total = [double]$Matches[4] * 1000
                                }
                            }
                        }
                        else {
                            [pscustomobject]@{ Code = "000"; Net = $null; Ttfb = $null; Total = $null }
                        }
                    } while ($retry)
                    Start-Sleep -Milliseconds $pace
                }
                if (-not $saw429) { $pace = [math]::Max($ProbeDelayMs, $pace - $PaceStep) }  # 顺畅则回落
                $ok = @($probes | Where-Object Code -eq "200")
                $codes = ($probes | ForEach-Object Code | Select-Object -Unique) -join "/"
                $net = ($ok | Measure-Object Net -Minimum).Minimum
                $ttfb = Get-Median ($ok | ForEach-Object Ttfb)
                $srv = Get-Median ($ok | ForEach-Object { $_.Ttfb - $_.Net })
                $total = Get-Median ($ok | ForEach-Object Total)
                # 每节点明细已落盘 result_latency.txt; 控制台只保留异常节点, 正常节点靠单行进度
                if ($ok.Count -lt $MsgSamples) {
                    $line = "{0}/{1} ok  net={2}  ttfb={3}  srv={4}  total={5}  [{6}]  {7}" -f $ok.Count, $MsgSamples,
                    ($net   ? ("{0:n0}ms" -f $net)   : "-"),
                    ($ttfb  ? ("{0:n0}ms" -f $ttfb)  : "-"),
                    ($srv   ? ("{0:n0}ms" -f $srv)   : "-"),
                    ($total ? ("{0:n0}ms" -f $total) : "-"), $codes, $node
                    # 先擦掉 \r 进度行再打印, 否则红日志会写在进度行中间被搅乱
                    Clear-ProgressLine
                    Write-Host $line -ForegroundColor ($ok.Count ? "Yellow" : "Red")
                }
                [pscustomobject]@{ Node = $node; Ok = $ok.Count; Net = $net; Ttfb = $ttfb; Srv = $srv; Total = $total; Codes = $codes }
            }
            Clear-ProgressLine
        }
        finally {
            if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }

        # 写最终延迟结果(TSV, 延迟列=网络建链最小值, merge.ps1 按表头取 节点/延迟;
        # 首token/服务端/总耗时仅参考, 表头刻意不含"延迟"二字避免歧义匹配)
        $ranked = $results | Sort-Object @{ e = "Ok"; Descending = $true }, @{ e = { $_.Net ?? [double]::MaxValue } }
        $tsv = @("序号`t节点名称`t延迟`t首token`t服务端`t总耗时`t成功`t状态码")
        $rank = 0
        $tsv += foreach ($r in $ranked) {
            $rank++
            $cells = foreach ($v in @($r.Net, $r.Ttfb, $r.Srv, $r.Total)) {
                ($r.Ok -and $null -ne $v) ? ("{0}ms" -f [math]::Round($v)) : "N/A"
            }
            "{0}`t{1}`t{2}`t{3}/{4}`t{5}" -f $rank, $r.Node, ($cells -join "`t"), $r.Ok, $MsgSamples, $r.Codes
        }
        [IO.File]::WriteAllText($OutLatency, ($tsv -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))

        $passed = @($results | Where-Object Ok).Count
        Write-Host "Latency test finished: $passed/$($alive.Count) alive nodes passed real-message test" -ForegroundColor Green
    }
}

# ================= Download Test =================
if ($Mode -ne "latency") {
    Write-Host "[Download] $($DownloadSize / 1MB)MB from downloads.claude.ai" -ForegroundColor Cyan

    $dlLines = [Collections.Generic.List[string]]::new()
    $dlDone = 0
    clash-speedtest -c $TestConfig --speed-mode download --server-url $DownloadUrl `
        --download-size $DownloadSize --timeout $Timeout --concurrent $Concurrent `
    | ForEach-Object {
        $dlLines.Add($_)
        $c = $_ -split "`t"
        if ($c.Count -ge 4 -and $c[0] -match '^\d') {
            $dlDone++
            Show-Progress "Download" $dlDone $selected.Count $c[1]
        }
    }
    Clear-ProgressLine
    [IO.File]::WriteAllText($OutDownload, ($dlLines -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))

    if ($LASTEXITCODE -ne 0) {
        Remove-Item $OutDownload -Force -ErrorAction SilentlyContinue
        throw "Download test failed (exit $LASTEXITCODE)"
    }

    $done = (Select-String -Path $OutDownload -Pattern '\d+(\.\d+)?\s*(MB|KB|GB)/s').Count
    Write-Host "Download test finished: $done/$($selected.Count) proxies measured" -ForegroundColor Green
}

$mergeScript = "$PSScriptRoot\merge.ps1"
if (Test-Path $mergeScript) {
    if ($Reset) { & $mergeScript -Reset $Reset } else { & $mergeScript }
}
else {
    Write-Host "(merge.ps1 not found, skip auto-merge)" -ForegroundColor DarkGray
}
Write-Host "All done." -ForegroundColor Green
