# Claude Proxy

三个工具，一套 Claude API 后端方案。

| 项目 | 用途 |
|---|---|
| [`claude-proxy/`](claude-proxy/) | 本地代理：让 Claude Desktop 走第三方 API |
| [`claude-switch/`](claude-switch/) | 一键切换 API 后端 |
| [`speedtest/`](speedtest/) | 节点测速：给 Claude 挑最快的代理节点 |

> 仅支持 **Windows**。敏感配置文件（含 Key）已 gitignore，不会入库。

## 快速开始

### 1. 配 API 源

```powershell
# 编辑 api_sources.yaml（含 API Key，已 gitignore，需自行创建）
# 然后同步到各项目：
.\sync_api_sources.ps1 -Apply
```

### 2. 启动代理

```powershell
# 双击或用命令行：
.\claude-proxy\start_claude.cmd
# 或
python claude-proxy\launch.py
```

运行中按 `r` 热加载配置、`n` 换下一套、`q` 退出。

### 3. Claude Desktop 指向代理

**Settings → Developer → Configure third-party inference**，填 `http://127.0.0.1:8899`。

### 4. 切换后端

```powershell
. claude-switch\claude-switch.ps1    # dot-source 加载
ds                                   # 切到 DeepSeek
qwen-qoder                           # 切到 Qwen (Qoder)
claude-status                        # 查看当前
```

### 5. 测速挑节点（可选）

```powershell
cd speedtest
.\claude-speedtest.ps1               # 详见 speedtest/README.md
```
