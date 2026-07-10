# ============================================================
# probe_models.ps1 — 通用 model list 探针
# ============================================================
# 查询任意 API 源的可用模型清单（OpenAI /v1/models 兼容 + Anthropic /v1/models）。
#
# 用法:
#   .\probe_models.ps1 -Source gtw                      # 从同目录 api_sources.yaml 读 base_url/api_key
#   .\probe_models.ps1 -Source gtw -Test                # 每个模型实测 anthropic+openai 两端点是否支持
#   .\probe_models.ps1 -Source gtw -Test -Model glm-5,kimi-k2.5   # 只测指定模型
#   .\probe_models.ps1 -BaseUrl https://api.x.com -ApiKey sk-xxx
#   .\probe_models.ps1 -Source arkvoice -Raw            # 输出原始 JSON（看 context window 等扩展字段）
#
# 端点候选: base 带路径(如 /api/v3、/anthropic)先直拼、再补 /v1；裸 host 反序。失败则试下一个。
# -Test 判定（max_tokens=8 最小请求，会产生极少量真实计费）:
#   anthropic = POST …/messages 返回 type=message；openai = POST …/chat/completions 返回 choices。
# 认证同时带 Authorization: Bearer 与 x-api-key + anthropic-version（两类后端通吃）。
# 依赖: -Source 模式需 python + pyyaml（与 sync_api_sources.ps1 相同）。
# ============================================================
param(
    [string]$Source,
    [string]$BaseUrl,
    [string]$ApiKey,
    [switch]$Raw,
    [switch]$Test,
    [string[]]$Model
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Source) {
    $ApiSourcesYaml = Join-Path $ScriptDir "api_sources.yaml"
    if (-not (Test-Path $ApiSourcesYaml)) { throw "api_sources.yaml not found: $ApiSourcesYaml" }
    $json = python -c "import yaml, json, io; print(json.dumps(yaml.safe_load(io.open(r'$ApiSourcesYaml', encoding='utf-8').read())))"
    if ($LASTEXITCODE -ne 0) { throw "YAML parse failed (need python + pyyaml)" }
    $cfg = $json | ConvertFrom-Json
    $src = $cfg.sources.$Source
    if (-not $src) {
        $names = ($cfg.sources.PSObject.Properties.Name) -join ', '
        throw "Unknown source '$Source'. Available: $names"
    }
    $BaseUrl = $src.base_url
    $ApiKey  = $src.api_key
    Write-Host "Source: $Source ($($src.name)) -> $BaseUrl" -ForegroundColor Cyan
}

if (-not $BaseUrl) { throw "Either -Source or -BaseUrl is required" }

$base = $BaseUrl.TrimEnd('/')

# base 带路径（如 /api/v3、/anthropic）时端点多为直拼；裸 host 时标准路径在 /v1 下
function Get-Candidates([string]$base, [string]$suffix) {
    $hasPath = ([Uri]$base).AbsolutePath.TrimEnd('/') -ne ''
    $list = if ($hasPath) { @("$base/$suffix", "$base/v1/$suffix") }
            else          { @("$base/v1/$suffix", "$base/$suffix") }
    $list | Select-Object -Unique
}
$candidates = Get-Candidates $base 'models'

$headers = @{ 'Accept' = 'application/json'; 'anthropic-version' = '2023-06-01' }
if ($ApiKey) {
    $headers['Authorization'] = "Bearer $ApiKey"
    $headers['x-api-key']     = $ApiKey
}

# OpenAI: {data:[{id,owned_by,created}]} | Anthropic: {data:[{id,display_name,created_at}]} | 变体: {models:[...]} 或裸数组
function Get-ModelArray($resp) {
    if ($resp -is [string]) { return $null }   # HTML/文本页（网关前端撞路径）不算成功
    if ($resp.data)         { return $resp.data }
    if ($resp.models)       { return $resp.models }
    if ($resp -is [array])  { return $resp }
    return $null
}

# 成功判定 = 解析出模型数组（HTTP 200 可能是网关前端页面，不作数）
$resp = $null; $models = $null
foreach ($url in $candidates) {
    Write-Host "GET $url" -ForegroundColor DarkGray
    try {
        $resp = Invoke-RestMethod -Uri $url -Headers $headers -Method Get -TimeoutSec 30
        $models = Get-ModelArray $resp
        if ($models) { break }
        Write-Host "  -> 200 but no model array (not an API endpoint)" -ForegroundColor DarkYellow
    } catch {
        $status = try { [int]$_.Exception.Response.StatusCode } catch { $null }
        Write-Host "  -> failed ($(if ($status) { "HTTP $status" } else { $_.Exception.Message }))" -ForegroundColor DarkYellow
    }
}
if (-not $models) { throw "No model list at any of: $($candidates -join ', ')" }

if ($Raw) {
    $resp | ConvertTo-Json -Depth 10
    return
}

$models | ForEach-Object {
    [pscustomobject]@{
        id       = $_.id
        owner    = if ($_.owned_by) { $_.owned_by } else { $_.display_name }
        created  = if ($_.created) {
                       [DateTimeOffset]::FromUnixTimeSeconds($_.created).ToString('yyyy-MM-dd')
                   } elseif ($_.created_at) { $_.created_at } else { $null }
    }
} | Sort-Object id | Format-Table -AutoSize

Write-Host "Total: $($models.Count) models" -ForegroundColor Green

if (-not $Test) { return }

# ---- 双端点实测: anthropic(…/messages) + openai(…/chat/completions) ----
$ids = @($models | ForEach-Object { $_.id } | Sort-Object)
if ($Model) { $ids = @($ids | Where-Object { $Model -contains $_ }) }
if (-not $ids) { throw "No models match -Model filter" }

$endpointCache = @{}   # kind -> 首个验证可用的 URL，后续模型不再重复探测候选

function Invoke-ChatProbe([string]$kind, [string]$modelId) {
    $suffix = if ($kind -eq 'anthropic') { 'messages' } else { 'chat/completions' }
    $urls = if ($endpointCache[$kind]) { @($endpointCache[$kind]) } else { Get-Candidates $base $suffix }
    $json = @{ model = $modelId; max_tokens = 8
               messages = @(@{ role = 'user'; content = 'hi' }) } | ConvertTo-Json -Depth 5
    $lastErr = $null
    foreach ($u in $urls) {
        try {
            $r = Invoke-RestMethod -Uri $u -Headers $headers -Method Post -Body $json `
                     -ContentType 'application/json' -TimeoutSec 60
            $ok = if ($kind -eq 'anthropic') { $r.type -eq 'message' } else { [bool]$r.choices }
            if ($ok) { $endpointCache[$kind] = $u; return 'OK' }
            $lastErr = 'HTTP 200, unexpected shape'
        } catch {
            $status = try { [int]$_.Exception.Response.StatusCode } catch { 0 }
            $detail = $_.ErrorDetails.Message
            if ($detail) { $detail = ($detail -replace '\s+', ' ').Trim() }
            if ($detail -and $detail.Length -gt 70) { $detail = $detail.Substring(0, 70) + '...' }
            $lastErr = if ($status) { "HTTP ${status}: $detail" } else { $_.Exception.Message }
        }
    }
    return $lastErr
}

Write-Host "`n-- endpoint test: $($ids.Count) models x anthropic/openai (max_tokens=8) --" -ForegroundColor Cyan
$results = foreach ($id in $ids) {
    Write-Host "  testing $id ..." -ForegroundColor DarkGray
    [pscustomobject]@{
        id        = $id
        anthropic = Invoke-ChatProbe 'anthropic' $id
        openai    = Invoke-ChatProbe 'openai' $id
    }
}
$results | Format-Table -AutoSize -Wrap
