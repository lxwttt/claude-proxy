<#
  sync_api_sources.ps1 — 从 api_sources.yaml 同步到 claude-switch + model_config.yaml
  =====================================================================================
  读取 config/api_sources.yaml（唯一权威源）→ 生成:
    1. ~/.claude/envs/{alias}.json     (claude-switch 环境变量)
    2. config/model_config.yaml        (proxy 模型映射)

  规则:
    - haiku → family 中 tier=low 模型 (不加后缀)
    - sonnet/opus → family 中 tier=high 模型
    - CLAUDE_CODE_SUBAGENT_MODEL 仅当 model_override.subagent 显式给出才写入 env JSON (默认不设)
    - "[1m]" 后缀仅当该模型登记 max_context ≥ 1M 时追加 (Claude Code 无后缀默认按 200K 上下文)
    - provider 设了 model_override 则覆盖自动检测
    - provider 可选 env: 键值对原样并入该 provider 的 env JSON (后写覆盖同名默认键)

  用法:
    .\sync_api_sources.ps1              # 预览变更 (WhatIf)
    .\sync_api_sources.ps1 -Apply       # 实际写入 (含自动清理旧 env JSON)
#>

param(
    [switch]$Apply           # 实际写入文件 (默认仅预览)
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $PSCommandPath -Parent

# ============================================================
# 路径
# ============================================================
$ApiSourcesYaml  = Join-Path $ScriptDir "api_sources.yaml"
$ModelConfigYaml = Join-Path $ScriptDir "claude-proxy\config\model_config.yaml"
$ClaudeEnvsDir   = Join-Path $env:USERPROFILE ".claude\envs"
$ConfigYaml      = Join-Path $ScriptDir "claude-proxy\config\config.yaml"

# ============================================================
# YAML → JSON (通过 Python)
# ============================================================
function ConvertFrom-YamlViaPython {
    param([string]$YamlPath)
    $python = "python"
    if (Test-Path $ConfigYaml) {
        try {
            $cfg = Get-Content $ConfigYaml -Raw | ConvertFrom-Yaml -ErrorAction Stop
            if ($cfg.paths.python) { $python = $cfg.paths.python }
        } catch {}
    }
    $yamlCode = @"
import yaml, json, sys
with open(r"$YamlPath", "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)
print(json.dumps(data, indent=2, ensure_ascii=False))
"@
    $json = & $python -c $yamlCode 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "YAML 解析失败 (Python=$python): $json"
        exit 1
    }
    return $json | ConvertFrom-Json
}

# ============================================================
# 从 family 的 models 列表中选模型
# ============================================================
function Select-Model {
    param([array]$Models, [string]$Tier)
    $candidates = $Models | Where-Object { $_.tier -eq $Tier }
    if (-not $candidates) {
        # fallback: 选任意模型
        $candidates = $Models
    }
    if (-not $candidates) { return $null }
    return $candidates[0].id
}

function Get-ProviderModels {
    param($Data, $Provider)
    $srcName = $Provider.source
    $famName = $Provider.family
    $src = $Data.sources.PSObject.Properties | Where-Object { $_.Name -eq $srcName }
    if (-not $src) { Write-Error "源 '$srcName' 不存在 (provider: $($Provider.Alias))"; return $null }
    $family = $src.Value.models.PSObject.Properties | Where-Object { $_.Name -eq $famName }
    if (-not $family) { Write-Error "族 '$famName' 在源 '$srcName' 中不存在"; return $null }
    return @($family.Value)
}

# ============================================================
# 模型上下文后缀: 仅当该模型登记 max_context ≥ 1M 才声明 "[1m]"
# (Claude Code 对无后缀模型默认按 200K 上下文)
# ============================================================
function Get-ModelSuffix {
    param([array]$Models, [string]$ModelId)
    $m = $Models | Where-Object { $_.id -eq $ModelId } | Select-Object -First 1
    if ($m -and $m.max_context -ge 1000000) { return "[1m]" }
    return ""
}

# ============================================================
# 生成 model_config.yaml 的单个 setting 块 (YAML 字符串)
# ============================================================
function New-ModelConfigSetting {
    param($Alias, $Label, $Source, $HighModel, $LowModel, $SonnetModel, $Effort)

    $lines = @()
    $lines += "  $(Escape-YamlKey $Alias):"
    $lines += "    name: ""$($Label -replace '"','\"')"""
    $lines += "    debug_mode: false"
    $lines += "    full_body_log: false"
    $lines += "    api_base_url: ""$($Source.base_url)"""
    $lines += "    api_key: ""$($Source.api_key)"""
    $lines += "    model_mapping:"
    $lines += "      haiku: ""$LowModel"""
    $lines += "      sonnet: ""$SonnetModel"""
    $lines += "      opus: ""$HighModel"""
    $lines += "    effort_mapping:"
    $lines += "      haiku: ""$Effort"""
    $lines += "      sonnet: ""$Effort"""
    $lines += "      opus: ""$Effort"""
    return ($lines -join "`n")
}

function Escape-YamlKey {
    param([string]$Key)
    if ($Key -match '[:\s"''{}[\],&*?|<>!%@`#()]') {
        return '"' + $Key.Replace('"', '\"') + '"'
    }
    return $Key
}

# ============================================================
# 主流程
# ============================================================
Write-Host "=== sync_api_sources ===" -ForegroundColor Cyan
Write-Host "数据源: $ApiSourcesYaml" -ForegroundColor DarkGray
Write-Host "模式: $(if ($Apply) {'写入'} else {'预览 (WhatIf)'})" -ForegroundColor $(if ($Apply) {'Yellow'} else {'DarkGray'})

# 1. 解析 YAML
Write-Host "`n[1/4] 解析 api_sources.yaml..." -ForegroundColor White
$Data = ConvertFrom-YamlViaPython $ApiSourcesYaml

# 2. 遍历 providers, 构建输出
Write-Host "[2/4] 处理提供者映射..." -ForegroundColor White

$EnvResults  = @()  # @{ Alias, Json, Path }
$McResults   = @()  # @{ Label, YamlBlock }
$Aliases     = @()  # 当前活跃的 alias 列表

foreach ($provProp in $Data.providers.PSObject.Properties) {
    $alias = $provProp.Name
    $prov  = $provProp.Value
    $target = if ($prov.target) { $prov.target } else { "both" }

    $toEnv = ($target -eq "both" -or $target -eq "claude_switch")
    $toMc  = ($target -eq "both" -or $target -eq "model_config")

    $targetLabel = if ($toEnv -and $toMc) { "env+mc" } elseif ($toEnv) { "env only" } else { "mc only" }
    Write-Host "  [$alias] $($prov.label) ← $($prov.source)/$($prov.family)  [$targetLabel]" -ForegroundColor Gray

    # 跟踪活跃的 claude-switch alias
    if ($toEnv) { $Aliases += $alias }

    # 查找源 + 族
    $src = $Data.sources.PSObject.Properties | Where-Object { $_.Name -eq $prov.source }
    if (-not $src) { Write-Warning "  源 '$($prov.source)' 不存在，跳过"; continue }
    $srcObj = $src.Value
    $familyProp = $srcObj.models.PSObject.Properties | Where-Object { $_.Name -eq $prov.family }
    if (-not $familyProp) { Write-Warning "  族 '$($prov.family)' 在源 '$($prov.source)' 中不存在，跳过"; continue }
    $models = @($familyProp.Value)

    # 选模型
    $override = $prov.model_override
    $highModel = if ($override -and $override.opus)    { $override.opus }    else { Select-Model $models "high" }
    $lowModel  = if ($override -and $override.haiku)   { $override.haiku }   else { Select-Model $models "low" }
    $sonnetModel = if ($override -and $override.sonnet) { $override.sonnet } else { $highModel }
    # subagent 不设默认: 仅显式 override 才下发 (未设时 Claude Code 按自身逻辑选)
    $subagentModel = if ($override -and $override.subagent) { $override.subagent } else { $null }

    if (-not $highModel -or -not $lowModel) {
        Write-Warning "  模型选择失败 (high=$highModel, low=$lowModel)，跳过"; continue
    }

    $effort = if ($prov.effort) { $prov.effort } else { "max" }

    # 预览模型选择
    $subagentLabel = if ($subagentModel) { $subagentModel } else { "(不设)" }
    Write-Host "    haiku=$lowModel  sonnet=$sonnetModel  opus=$highModel  subagent=$subagentLabel  effort=$effort" -ForegroundColor DarkGray

    # claude-switch env JSON (仅当 target 包含 claude_switch)
    if ($toEnv) {
        $highSfx     = Get-ModelSuffix $models $highModel
        $sonnetSfx   = Get-ModelSuffix $models $sonnetModel
        $envData = @{
            ANTHROPIC_BASE_URL             = $srcObj.base_url
            ANTHROPIC_AUTH_TOKEN           = $srcObj.api_key
            ANTHROPIC_MODEL                = "$highModel$highSfx"
            ANTHROPIC_DEFAULT_OPUS_MODEL   = "$highModel$highSfx"
            ANTHROPIC_DEFAULT_SONNET_MODEL = "$sonnetModel$sonnetSfx"
            ANTHROPIC_DEFAULT_HAIKU_MODEL  = $lowModel
            CLAUDE_CODE_EFFORT_LEVEL       = $effort
        }
        if ($subagentModel) {
            $subagentSfx = Get-ModelSuffix $models $subagentModel
            $envData.CLAUDE_CODE_SUBAGENT_MODEL = "$subagentModel$subagentSfx"
        }
        # provider 级额外 env 原样并入 (后写覆盖同名默认键)
        if ($prov.env) {
            foreach ($extra in $prov.env.PSObject.Properties) { $envData[$extra.Name] = "$($extra.Value)" }
        }
        $envJson = @{ env = $envData } | ConvertTo-Json -Depth 4
        $envPath = Join-Path $ClaudeEnvsDir "$alias.json"
        $color = if ($prov.color) { $prov.color } else { "Gray" }
        $EnvResults += @{ Alias = $alias; Json = $envJson; Path = $envPath; Label = $prov.label; Color = $color }
        Write-Host "    → envs/$alias.json" -ForegroundColor DarkGray
    }

    # model_config.yaml setting (仅当 target 包含 model_config)
    if ($toMc) {
        $mcBlock = New-ModelConfigSetting -Alias $alias -Label $prov.label -Source $srcObj -HighModel $highModel -LowModel $lowModel -SonnetModel $sonnetModel -Effort $effort
        $McResults += @{ Alias = $alias; YamlBlock = $mcBlock }
        Write-Host "    → model_config: $($prov.label)" -ForegroundColor DarkGray
    }
}

# 3. 写文件 (或预览)
Write-Host "`n[3/4] $(if ($Apply) {'写入'} else {'将写入'}) 文件..." -ForegroundColor White

# --- claude-switch env JSONs ---
if (-not (Test-Path $ClaudeEnvsDir)) {
    if ($Apply) { New-Item -ItemType Directory $ClaudeEnvsDir -Force | Out-Null }
}
foreach ($r in $EnvResults) {
    Write-Host "  ~/.claude/envs/$($r.Alias).json" -ForegroundColor Gray
    if ($Apply) {
        $r.Json | Set-Content $r.Path -Encoding UTF8
    }
}

# --- _providers.json (claude-switch provider 注册表) ---
$providersRegistry = @(
    # 固定条目: Anthropic Official (OAuth，不在 api_sources 管理范围)
    @{ alias = "opus"; template = "envs\opus.json"; label = "Anthropic Official"; color = "Green" }
)
foreach ($r in $EnvResults) {
    $providersRegistry += @{ alias = $r.Alias; template = "envs\$($r.Alias).json"; label = $r.Label; color = $r.Color }
}
$registryPath = Join-Path $ClaudeEnvsDir "_providers.json"
$registryJson = $providersRegistry | ConvertTo-Json -Depth 3
Write-Host "  ~/.claude/envs/_providers.json ($($providersRegistry.Count) providers)" -ForegroundColor Gray
if ($Apply) {
    $registryJson | Set-Content $registryPath -Encoding UTF8
}

# --- model_config.yaml ---
# 映射旧 setting key → 新 alias (用于更新 current_setting)
$oldKeyToAlias = @{
    "DeepSeek" = "ds"
    "Doubao-Pro(GTW)" = "doubao-gtw"
    "Qwen37-Max(GTW)" = $null  # 已移除 → 回退到第一个 provider
}

$currentSetting = if (Test-Path $ModelConfigYaml) {
    $existing = Get-Content $ModelConfigYaml -Raw
    if ($existing -match 'current_setting:\s*"([^"]*)"') {
        $oldKey = $matches[1]
        if ($oldKeyToAlias.ContainsKey($oldKey)) { $oldKeyToAlias[$oldKey] } else { $oldKey }
    } else { $Aliases[0] }
} else { $Aliases[0] }

# 构建新的 settings 块 (仅 API-key 提供者; OAuth 已存在于原文件则保留)
$mcLines = @()
$mcLines += "current_setting: ""$currentSetting"""
$mcLines += ""
$mcLines += "settings:"

foreach ($r in $McResults) {
    $mcLines += $r.YamlBlock
}

# 追加 OAuth/Claude setting (从原文件提取，如果存在)
if (Test-Path $ModelConfigYaml) {
    $existing = Get-Content $ModelConfigYaml -Raw
    # 简单策略: 提取 "Claude:" (OAuth) 块
    if ($existing -match '(?ms)(  Claude:.*?)(?=\n  \w|\z)') {
        $oauthBlock = $matches[1].TrimEnd()
        $mcLines += ""
        $mcLines += $oauthBlock
    }
}

$mcContent = ($mcLines -join "`n") + "`n"

Write-Host "  claude-proxy/config/model_config.yaml" -ForegroundColor Gray
if ($Apply) {
    $mcContent | Set-Content $ModelConfigYaml -Encoding UTF8 -NoNewline
}

# 4. 清理旧 env JSON（不在当前 provider 列表中的）
Write-Host "`n[4/4] 清理旧文件..." -ForegroundColor White
# 当前应存在的 alias 集合
$activeEnvAliases = @($EnvResults | ForEach-Object { $_.Alias })
# 额外清理: 旧 provider 残留（已删除或改名的旧 alias）
$knownStale = @("qwen-lm", "kimi", "qwen", "doubao", "qoder", "deepseek-qoder", "qoder-ds", "qoder-glm", "qoder-kimi")
$staleRemoved = $false
foreach ($alias in $knownStale) {
    if ($alias -in $activeEnvAliases) { continue }  # 仍在活跃列表中，不删
    $p = Join-Path $ClaudeEnvsDir "$alias.json"
    if (Test-Path $p) {
        if ($Apply) {
            Remove-Item $p -Force
            Write-Host "  已删除: ~/.claude/envs/$alias.json" -ForegroundColor Yellow
            $staleRemoved = $true
        } else {
            Write-Host "  (WhatIf) 将删除: ~/.claude/envs/$alias.json" -ForegroundColor DarkYellow
        }
    }
}
if (-not $staleRemoved -and -not $Apply) {
    Write-Host "  (无旧文件需清理)" -ForegroundColor DarkGray
}

# ============================================================
# 摘要
# ============================================================
Write-Host "`n=== 摘要 ===" -ForegroundColor Cyan
Write-Host "提供者: $($EnvResults.Count) 个" -ForegroundColor White
foreach ($r in $EnvResults) {
    Write-Host "  $($r.Alias) → $($r.Label)" -ForegroundColor Gray
}
Write-Host "env JSON: ~/.claude/envs/{$($Aliases -join ',')}.json" -ForegroundColor DarkGray
Write-Host "model_config: $ModelConfigYaml" -ForegroundColor DarkGray

if (-not $Apply) {
    Write-Host "`n⚠ 预览模式 — 加 -Apply 实际写入" -ForegroundColor Yellow
} else {
    Write-Host "`n✓ 同步完成" -ForegroundColor Green
    if ($staleRemoved) { Write-Host "✓ 旧 env 文件已清理" -ForegroundColor Green }
    Write-Host "`n✓ claude-switch providers 已更新 (_providers.json)" -ForegroundColor Green
}
