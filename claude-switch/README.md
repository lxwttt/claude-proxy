# Claude Switch

一键切换 Claude Code 的 API 后端。Provider 注册表由顶层 `sync_api_sources.ps1` 从 `api_sources.yaml` 自动生成。

## 快速开始

### 加载

```powershell
. claude-switch.ps1    # dot-source，注册快捷命令
```

### 命令

```powershell
ds                 # 切到 DeepSeek Relay
gpt                # 切到 GPT-5.6 Sol (Codex 订阅, 需先跑仓根 start_cliproxy.ps1)
qwen-qoder         # 切到 Qwen3.7-Max (Qoder)
opus               # 切到 Anthropic Official (OAuth)

claude-status      # 查看当前状态
claude-capture 20x # 保存当前登录态为账户 "20x"
20x                # 切换订阅账户

claude-switch status      # 或直接调脚本
claude-switch ds          #
```

### 添加新 Provider

编辑顶层 `api_sources.yaml` → 跑 `..\sync_api_sources.ps1 -Apply`。`_providers.json` 自动更新，无需改本脚本。
