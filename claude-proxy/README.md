# Claude Proxy

让 Claude Desktop 使用第三方 API 后端的本地代理。

> Windows only。配置文件含 Key，已 gitignore。

## 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 配置

1. 复制 `config/sample_config.yaml` → `config/config.yaml`，填 Python 路径和 App ID
2. 复制 `config/sample_model_config.yaml` → `config/model_config.yaml`（或由顶层 `sync_api_sources.ps1` 自动生成）

### 运行

```powershell
# 双击
start_claude.cmd

# 或命令行
python launch.py
```

| 按键 | 作用 |
|---|---|
| `r` | 热加载 model_config.yaml |
| `n` | 切换到下一套配置 |
| `q` | 退出并清理进程 |

### Claude Desktop 设置

**Settings → Developer → Configure third-party inference** → 填 `http://127.0.0.1:8899`。

### 查 App User Model ID

```powershell
Get-StartApps | Where-Object { $_.Name -like "*Claude*" }
```
