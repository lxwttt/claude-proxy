# Claude Auto-Launcher

一键启动 Claude 桌面版，并自动挂载 HTTP/HTTPS 代理和 API 转发代理。适用于需要代理访问 Claude 服务或通过第三方 API（如 DeepSeek、OpenAI）桥接 Claude 客户端的场景。

## 功能概览

1. **自动启动代理软件** — 启动额外的 EXE 程序（如 VPN、Clash 等），等待其端口就绪
2. **挂载系统代理** — 设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，使 Claude 的流量经过代理
3. **启动 API 转发代理** — 将 Claude 客户端的 API 请求转发到第三方后端（DeepSeek / OpenAI 等），并自动完成模型名称映射
4. **启动 Claude 桌面版** — 以上所有就绪后自动拉起 Claude 应用
5. **优雅退出** — Ctrl+C 后按逆序停止所有进程

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  start_claude.cmd                                        │
│    └─ launch.py  (主启动器)                               │
│          ├─ (1) 启动额外 EXE (WestWorldVPN / Clash 等)    │
│          ├─ (2) 设置 HTTP_PROXY / HTTPS_PROXY             │
│          ├─ (3) 启动 local_proxy.py (API 转发代理)         │
│          └─ (4) 启动 Claude 桌面版                         │
│                                                          │
│  local_proxy.py  (运行在 127.0.0.1:8899)                  │
│    ├─ 接收 Claude 的 API 请求                              │
│    ├─ 模型名称映射 (haiku→deepseek-v4-flash 等)            │
│    └─ 转发到目标 API (DeepSeek / OpenAI)                   │
└──────────────────────────────────────────────────────────┘
```

## 文件结构

| 文件 | 作用 |
|---|---|
| `launch.py` | 主启动器 — 负责启动 EXE、挂代理、启动转发代理、启动 Claude |
| `local_proxy.py` | API 转发代理 — 接收 Claude 请求并转发到第三方 API |
| `config.yaml` | 启动器配置 — Python 路径、代理端口、额外 EXE 等 |
| `model_config.yaml` | 模型映射配置 — 选择后端 API 和模型映射规则 |
| `start_claude.cmd` | Windows 入口批处理文件 |

## 前置条件

- Windows 系统（依赖 `subprocess.CREATE_NO_WINDOW` 等 Windows API）
- Python 3（建议 Anaconda 或其他发行版）
- Python 包：`pyyaml`、`requests`

```bash
pip install pyyaml requests
```

## 配置方法

### 1. 修改 `config.yaml`

```yaml
paths:
  python: "D:/anaconda3/python.exe"          # Python 可执行文件路径
  proxy_script: "D:/claude-proxy/local_proxy.py"  # 转发代理脚本路径
  claude_exe: "..."                           # Claude EXE 路径（可选）

ports:
  proxy_port: 8899                            # 转发代理端口
  clash_proxy_port: 21882                     # 代理软件端口

app:
  app_id: "Claude_pzs8sxrjxfjjc!Claude"      # Claude 的 App User Model ID（可选）

proxy_settings:
  enabled: true
  http_proxy: "http://127.0.0.1:21882"        # 设置 HTTP_PROXY
  https_proxy: "http://127.0.0.1:21882"       # 设置 HTTPS_PROXY

extra_exes:
  - name: "WestWorldVPN"
    path: "C:/path/to/WestWorldVPN.exe"
    args: ""
    wait_for_port: 21882                       # 等待此端口就绪
    description: "代理软件"
    required: true                             # true=启动失败则退出
```

**关键参数说明：**

- `paths.python` — 改为你本机实际的 Python 路径
- `paths.proxy_script` — 保持指向 `local_proxy.py` 即可
- `extra_exes` — 填入需要预启动的代理软件（如 Clash、WestWorldVPN、V2Ray 等）。可配置多条；`required: true` 表示该程序必须启动成功，否则退出
- `proxy_settings` — 设置后将注入 `HTTP_PROXY` 和 `HTTPS_PROXY` 环境变量，使 Claude 的请求经过此代理
- 如果不需要启动额外 EXE，将 `extra_exes` 设为空列表 `[]`

### 2. 修改 `model_config.yaml`（API 转发配置）

```yaml
current_setting: "DeepSeek"    # 当前启用的配置名称

settings:
  DeepSeek:
    name: "DeepSeek"
    debug_mode: true            # 开启后打印详细请求/响应日志
    api_base_url: "https://api.deepseek.com/anthropic"   # 目标 API 地址
    api_key: "sk-..."                                     # API Key
    model_mapping:
      haiku: "deepseek-v4-flash"       # Claude 模型 → 目标模型
      sonnet: "deepseek-v4-pro[1m]"
      opus: "deepseek-v4-pro[1m]"

  OpenAI:
    name: "OpenAI"
    debug_mode: false
    api_base_url: "https://api.openai.com/v1"
    api_key: "sk-..."
    model_mapping:
      haiku: "gpt-4o-mini"
      sonnet: "gpt-4o"
      opus: "gpt-4o"
```

**模型映射说明：**

Claude 客户端请求时携带的模型名（如 `claude-sonnet-4-20250514`）会被 `local_proxy.py` 提取最后的单词 `sonnet`，然后根据 `model_mapping` 映射为目标 API 的模型名。你可以在 `model_mapping` 中添加任意映射规则。

**热切换：**

`local_proxy.py` 运行中时，在终端按 `r` 键即可热加载 `model_config.yaml`，无需重启。按 `q` 键退出程序。

## 使用方法

### 方式一：双击批处理文件

直接双击 `start_claude.cmd`，即可一键启动全部流程。

### 方式二：直接运行 Python

```bash
cd D:\claude-proxy
python launch.py
```

### 启动流程

启动后控制台输出如下：

```
==================================================
   Claude 自动启动器
==================================================
✅ 配置文件加载成功
✅ 设置代理环境变量
==================================================
启动额外EXE程序
==================================================
启动: WestWorldVPN
   路径: ...
✅ WestWorldVPN 启动成功 (PID: 12345)
等待端口 21882 启动...
✅ 端口 21882 已启动
✅ 所有额外EXE程序启动完成
启动透明代理...
✅ 代理已在端口 8899 启动
✅ 所有组件已成功启动！
   按 Ctrl+C 停止所有程序
==================================================
```

### 停止

在运行 `launch.py` 的终端按 **Ctrl+C**，程序会按逆序自动停止：关闭 Claude → 停止转发代理 → 停止额外 EXE。

如果单独运行 `local_proxy.py`，按 `q` 退出。

## 详细组件说明

### launch.py — 主启动器

`ClaudeLauncher` 类按序执行以下步骤：

1. **加载配置** — 读取 `config.yaml`
2. **设置代理环境变量** — 将 `HTTP_PROXY` / `HTTPS_PROXY` 注入当前进程环境
3. **启动额外 EXE** — `ExtraExeManager` 逐一启动 EXE，等待端口就绪
4. **启动转发代理** — 以子进程运行 `local_proxy.py`，等待端口 8899 就绪
5. **启动 Claude** — 通过 App User Model ID 或 EXE 路径启动 Claude
6. **保持运行** — 等待 Ctrl+C，然后逆序清理所有子进程

### local_proxy.py — API 转发代理

一个轻量级 HTTP 服务器（基于 `http.server`），运行在 `127.0.0.1:8899`：

- **GET /** — 健康检查，返回 `{"status": "ok"}`
- **POST /*** — 接收 Claude 发送的 API 请求，按 `model_mapping` 替换模型名，转发到 `api_base_url`
- 支持调试模式（输出请求/响应详情到终端）
- 运行时按 `r` 热加载配置，按 `q` 退出

### 指定 Claude 启动方式

`config.yaml` 中支持两种方式，按优先级：

1. `app.app_id` — Windows App User Model ID（推荐，稳定）
   - 可通过 `Get-StartApps | Where-Object { $_.Name -like "*Claude*" }` 在 PowerShell 中查询
2. `paths.claude_exe` — Claude EXE 的绝对路径
   - 如果只填此项，`app_id` 留空即可
