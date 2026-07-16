# ============================================================
# start_cliproxy.ps1 — 启动 CLIProxyAPI（claude-switch `gpt` provider 的后端）
# ============================================================
# CLIProxyAPI 把 ChatGPT Codex 订阅的 OAuth 凭证包装成 Anthropic 兼容 API，
# 本地 127.0.0.1:8317。源码与配置在 D:\Tools\CLIProxyAPI（git clone 自建）。
#
# 用法:
#   .\start_cliproxy.ps1          # 前台启动，日志打到控制台，Ctrl+C 停止
#
# 相关:
#   重新登录:  D:\Tools\CLIProxyAPI\cli-proxy-api.exe -config D:\Tools\CLIProxyAPI\config.yaml -codex-login
#   升级:      cd D:\Tools\CLIProxyAPI && git pull && go build -o cli-proxy-api.exe ./cmd/server
# ============================================================

$Exe = 'D:\Tools\CLIProxyAPI\cli-proxy-api.exe'
$Cfg = 'D:\Tools\CLIProxyAPI\config.yaml'

if (-not (Test-Path $Exe)) { throw "未找到 $Exe — 先在 D:\Tools\CLIProxyAPI 编译: go build -o cli-proxy-api.exe ./cmd/server" }
if (-not (Test-Path $Cfg)) { throw "未找到 $Cfg" }

if (Get-NetTCPConnection -LocalPort 8317 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "CLIProxyAPI 已在 127.0.0.1:8317 运行，无需重复启动" -ForegroundColor Green
    return
}

& $Exe -config $Cfg
