# Claude Proxy

一键启动 Claude 桌面版，自动挂载 HTTP/HTTPS 代理并启动 API 转发代理。核心场景：**让 Claude Desktop 客户端使用第三方 API 后端**（DeepSeek、OpenAI 等），无需修改 Claude 自身代码。

## 功能概览

1. **WSL2 VM 保活** — 自动检测并保持 WSL2 运行（Claude 沙盒 VM 的前提）
2. **自动启动代理软件** — 启动额外 EXE（VPN 等），等待端口就绪
3. **挂载系统代理** — 设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量
4. **API 转发代理** — 本地 HTTP 服务器拦截 Claude 的 API 请求，完成模型名映射 + effort 映射后**流式透传**到第三方后端
5. **启动 Claude 桌面版** — 所有组件就绪后自动拉起 Claude
6. **优雅退出** — Ctrl+C 后逆序停止所有进程（含 WSL VM）

## 架构

```
start_claude.cmd (Windows 入口)
  └─ launch.py  (主启动器)
        ├─ (0) 确保 WSL2 VM 后台运行 (Claude 沙盒需要)
        ├─ (1) 启动额外 EXE (VPN 等)，等待端口就绪
        ├─ (2) 设置 HTTP_PROXY / HTTPS_PROXY 环境变量
        ├─ (3) 启动 src/local_proxy.py (API 转发代理，:8899)
        └─ (4) 启动 Claude 桌面版

src/local_proxy.py  (运行在 127.0.0.1:8899)
  ├─ 接收 Claude 的 API 请求
  ├─ 模型名称映射 (haiku/sonnet/opus → 目标模型)
  ├─ effort 映射 (按模型等级覆写 reasoning effort)
  ├─ GET / 健康检查，GET /reload 热加载配置
  ├─ 流式透传响应到目标 API (DeepSeek Anthropic 兼容端点 / OpenAI)
  └─ 启动期端口独占 + 请求体上限/干净拒绝（单实例 & 抗 ECONNRESET）

src/common.py  (共享工具模块)
  ├─ 统一日志配置（双输出 + 按日轮转 + 自动清理）
  ├─ 端口探测与等待
  ├─ 进程管理（启动、优雅终止、强制终止）
  ├─ 配置验证工具
  └─ 常量定义（模型 tier、超时等）
```

`launch.py` 和 `local_proxy.py` 均通过 `common.py` 共享日志、端口检查、进程管理等基础设施。

## 文件结构

| 文件 | 用途 |
|---|---|
| `launch.py` | 主启动器 — 编排 WSL、VPN、代理、Claude 的启动顺序 |
| `src/keep_wsl.py` | WSL2 VM 保活模块 — 已从 launch.py 解耦；由 launch.py 调用，也可 `python src/keep_wsl.py` 单独运行 |
| `src/local_proxy.py` | API 转发代理 — HTTP 服务器，模型名映射 + effort 映射 + 流式转发 |
| `src/common.py` | 共享工具模块 — 日志、端口检查、进程管理、常量 |
| `config/config.yaml` | 启动器配置 — Python 路径、端口、额外 EXE（从 `config/sample_config.yaml` 复制） |
| `config/model_config.yaml` | API 转发配置 — 后端选择、API Key、模型映射、effort 映射（从 `config/sample_model_config.yaml` 复制） |
| `start_claude.cmd` | Windows 批处理入口 — 校验环境后启动 `launch.py` |
| `config/sample_config.yaml` | 启动器配置模板 |
| `config/sample_model_config.yaml` | 模型配置模板 |

## 前置条件

- Windows 系统（依赖 `subprocess.CREATE_NO_WINDOW` 等 Windows API）
- Python 3（建议 Anaconda 或其他发行版）
- Python 包：`pyyaml`、`requests`

```bash
pip install pyyaml requests
```

## 配置方法

### 1. 修改 `config/config.yaml`

```yaml
paths:
  python: "D:/anaconda3/python.exe"          # Python 可执行文件路径
  proxy_script: "D:/claude-proxy/src/local_proxy.py"  # 转发代理脚本路径
  claude_exe: "..."                           # Claude EXE 路径（可选）

ports:
  proxy_port: 8899                            # 转发代理端口

app:
  app_id: "Claude_pzs8sxrjxfjjc!Claude"      # Claude 的 App User Model ID（可选）

proxy_settings:
  enabled: true
  http_proxy: "http://127.0.0.1:10809"        # 设置 HTTP_PROXY
  https_proxy: "http://127.0.0.1:10809"       # 设置 HTTPS_PROXY

extra_exes:
  - name: "VPN"
    path: "C:/path/to/vpn.exe"
    args: ""
    wait_for_port: 10809                       # 等待此端口就绪
    description: "代理软件"
    required: true                             # true=启动失败则退出
```

**关键参数说明：**

- `paths.python` — 改为你本机实际的 Python 路径
- `paths.proxy_script` — 保持指向 `src/local_proxy.py` 即可
- `extra_exes` — 填入需要预启动的代理软件。可配置多条；`required: true` 表示该程序必须启动成功，否则退出
- `proxy_settings` — 设置后将注入 `HTTP_PROXY` 和 `HTTPS_PROXY` 环境变量，使 Claude 的请求经过此代理
- 如果不需要启动额外 EXE，将 `extra_exes` 设为空列表 `[]`

### 2. 修改 `config/model_config.yaml`（API 转发配置）

```yaml
current_setting: "DeepSeek"    # 当前启用的配置名称

settings:
  DeepSeek:
    name: "DeepSeek"
    debug_mode: true            # 开启后打印详细请求/响应日志
    full_body_log: false        # 仅深度排查设 true：记完整响应体；平时只记首 4KiB（防 SSE 刷爆日志）
    api_base_url: "https://api.deepseek.com/anthropic"   # 目标 API 地址
    api_key: "sk-..."                                     # API Key
    model_mapping:
      haiku: "deepseek-v4-flash"       # Claude 模型 → 目标模型
      sonnet: "deepseek-v4-pro"
      opus: "deepseek-v4-pro"

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

Claude 客户端请求时携带的模型名（如 `claude-sonnet-4-20250514`）会被 `local_proxy.py` 提取 tier 关键字（`opus`/`sonnet`/`haiku`），然后根据 `model_mapping` 映射为目标 API 的模型名。

**effort 映射：**

当原请求包含 `output_config` 字段时，代理会根据模型等级自动覆写 `reasoning.effort` 值。这对于 DeepSeek 等支持 reasoning effort 的后端特别有用 —— 可以根据任务复杂度分配合适的推理深度：

```yaml
effort_mapping:
  haiku: "high"     # 轻量任务用 high
  sonnet: "xhigh"   # 中等任务用 xhigh
  opus: "max"       # 复杂任务用 max
```

如果原请求没有 `output_config`，则跳过 effort 映射，不做任何修改。

**热切换：**

- `local_proxy.py` 独立运行时，在终端按 `r` 键即可热加载 `model_config.yaml`，无需重启；按 `q` 键退出程序。
- 通过 `launch.py` 运行时，在启动器终端按 `r` 键，或通过 HTTP 请求 `GET /reload` 触发配置重载。

`debug_mode` 与 `full_body_log` 的关系：`debug_mode: true` 记录请求/响应概要；`full_body_log` 默认 `false`，此时响应体只记前 4 KiB（足够看到流是否正常），仅在需要逐字节排查时临时设为 `true` 记录完整 SSE，避免日志被长回答刷爆。

> ⚠️ 安全提示：`debug_mode: true` 会把**完整请求体（含全部对话内容，可能含你粘贴的凭据/隐私）以明文**写入 `logs/proxy_*.log` 并保留 7 天。模板默认开启仅为方便排障——日常使用建议设为 `false`，且 `logs/` 不应随仓库或备份外泄（已 `.gitignore`）。

### 3. 在 Claude Desktop 启用第三方推理（关键前提）

> **这是让 Claude 走本代理的真正开关 —— 本项目其余组件都只是为它铺路。**

`launch.py` 负责拉起代理、WSL、VPN 等，但**不负责把 Claude 指向代理**；该重定向在 Claude Desktop 自身设置里完成：

1. 打开 Claude Desktop → **Settings / 设置**
2. 进入 **Developer**（开发者）
3. 选择 **Configure third-party inference**（配置第三方推理）
4. 将推理端点指向本地代理 **`http://127.0.0.1:8899`**

配置完成后，Claude 的 API 请求会发到 `127.0.0.1:8899`，由 `local_proxy.py` 完成模型映射并流式转发到后端。若此项未配置，Claude 仍直连官方端点，代理不会收到任何请求。

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
启动: VPN
   路径: ...
✅ VPN 启动成功 (PID: 12345)
等待端口 10809 启动...
✅ 端口 10809 已启动
✅ 所有额外EXE程序启动完成
启动透明代理...
✅ 代理已在端口 8899 启动
✅ 所有组件已成功启动！
   按 Ctrl+C 停止所有程序
==================================================
```

### 停止

**通过 `launch.py` 运行时：** 在终端按 **Ctrl+C**，程序会自动按以下顺序清理：
1. 停止转发代理（`local_proxy.py` 子进程）
2. 关闭 WSL VM（`wsl --shutdown`）
3. 停止所有额外 EXE

**单独运行 `local_proxy.py` 时：** 按 `q` 退出。

## 详细组件说明

### launch.py — 主启动器

`ClaudeLauncher` 类按序执行以下步骤：

1. **确保 WSL2 运行** — 调用 `keep_wsl` 模块：检测 WSL 可用性，设置 WSL2 为默认版本，如未安装 WSL 发行版，请手动运行 `wsl --install -d Ubuntu`，通过 VBScript 后台保活 WSL VM（Claude 沙盒功能依赖此 VM）
2. **加载配置** — 读取 `config/config.yaml`
3. **设置代理环境变量** — 将 `HTTP_PROXY` / `HTTPS_PROXY` 注入当前进程环境
4. **启动额外 EXE** — `ExtraExeManager` 逐一启动 EXE，等待端口就绪
5. **启动转发代理** — 以子进程运行 `local_proxy.py`，等待端口 8899 就绪
6. **启动 Claude** — 通过 App User Model ID 或 EXE 路径启动 Claude
7. **保持运行** — 等待 Ctrl+C，逆序清理：停止转发代理 → 关闭 WSL VM → 停止额外 EXE

### src/local_proxy.py — API 转发代理

一个轻量级多线程 HTTP 服务器（基于 `http.server` + `ThreadingMixIn`），运行在 `127.0.0.1:8899`：

- **GET /** — 健康检查，返回 `{"status": "ok"}`
- **GET /reload** — 热加载 `model_config.yaml`，返回 `{"reload": "ok"}` 或 `{"reload": "failed"}`
- **POST /*** — 接收 Claude 的 API 请求，按 `model_mapping` 替换模型名，按 `effort_mapping` 覆写 reasoning effort，**流式透传**到 `api_base_url`（详见下方「请求转发的关键设计」）
- Debug 模式（`debug_mode: true`）将完整请求/响应写入日志文件 `logs/proxy_YYYYMMDD.log`
- 独立运行时按 `r` 热加载配置，按 `q` 退出
- 被 `launch.py` 以子进程方式启动时，键盘监听自动禁用，改由 `launch.py` 通过 `GET /reload` 远程触发热加载

### 请求转发的关键设计（流式 / 大请求体 / 端口独占）

代理在转发链路上有三处刻意为之的设计，用于根除"客户端莫名 `ECONNRESET` / 会话卡死"一类问题：

**1. 真·流式透传（stream passthrough）**

代理对上游请求使用 `stream=True`，**响应头一到就边收边 `flush`** 把字节推给客户端，并**原样透传上游的 `Content-Type`**（SSE 为 `text/event-stream`，不再硬编码 `application/json`）。

- 为什么重要：若整段缓冲后再一次性返回，长回答（大上下文 + 高 effort 推理可达数十秒）期间客户端**一个字节都收不到**，会触发其自身读超时而主动重置连接，表现为 `ECONNRESET`、会话"卡死"。流式透传后**首字节通常 < 0.5s 到达、SSE 事件全程分散流淌**，连接永不在静默中超时。
- 上游读超时设为 `(连接 10s, 读 300s)`，其中读超时是"相邻两个数据块之间"的最大间隔，对长生成足够宽松。

**2. 大请求体适配 + 干净拒绝（抗 RST）**

请求体上限为 **32 MiB**（`common.py: MAX_REQUEST_BODY_BYTES`，对齐 Anthropic 32MB 上限，可容纳内联 PDF 等 base64 附件）。超限或畸形请求一律**干净拒绝**而非重置连接：

- 体积 > 上限 → **先排空客户端仍在上传的请求体**，再返回 `413`。若不排空就直接关连接，未读的请求体会触发 TCP RST，客户端看到的将是 `ECONNRESET` 而非 `413`。
- `Content-Length` 缺失/非法/为负 → 干净 `400`（避免解析异常冲垮处理线程而导致连接被重置）。
- 上述拒绝均写入日志（含真实体积），不再静默。

> 注意：DeepSeek 等后端通常**不支持** Anthropic 的 `document`/PDF 内联块。需要让模型读 PDF 时，应由客户端工具/技能把 PDF **抽成文本**再发送，而非直接内联超大 base64 附件。

**3. 启动期端口独占（单实例保证）**

`local_proxy.py` 绑定端口前会执行 `ensure_sole_instance`：若 `8899` 已被占用，**仅当占用者是「python 进程且命令行含 `local_proxy.py`」时**才终止它（迁移到别的环境时绝不误杀无关进程；外来进程仅告警并中止启动），清理后**复查端口确实释放**才继续。

- 为什么重要：Windows 的 `SO_REUSEADDR` 会**允许两个进程共占同一端口**——旧代理残留时，新进程本会与其同时监听 `8899`、请求被随机分流，导致"改了代码重启却仍跑旧逻辑"的诡异现象。本项目已**显式 `allow_reuse_address = False`**（绑定被占端口会直接报错、fail-loud），再叠加 `ensure_sole_instance` 主动清理旧实例并复查端口，双重保证任何时刻 `8899` 上只有一个、且是最新代码的实例。

### src/common.py — 共享工具模块

`launch.py` 和 `local_proxy.py` 的公共依赖，提供：

- **统一日志配置** — 双输出日志（控制台 + 文件），按日轮转，文件日志自动去除 ANSI 颜色码和 emoji
- **端口工具** — `is_port_listening()` 和 `wait_for_port()`，用于探测和等待端口就绪
- **进程管理** — `start_process()` 启动子进程（支持隐藏窗口），`terminate_process()` 优雅终止（terminate → wait → kill）
- **配置验证** — `validate_config_schema()` 检查必需字段是否存在
- **常量** — 模型 tier 关键字（`opus` / `sonnet` / `haiku`）、请求体上限（`MAX_REQUEST_BODY_BYTES`，32 MiB）、网络超时、WSL 重试参数等

### 日志

两个程序均输出日志到 `logs/` 目录，按日轮转：

- `logs/launcher_YYYYMMDD.log` — launch.py 的完整日志
- `logs/proxy_YYYYMMDD.log` — local_proxy.py 的完整日志

文件日志自动去除 ANSI 颜色码和 emoji；控制台输出保留。`debug_mode: true` 时，API 请求/响应的完整内容会写入代理日志。

**日志自动清理：** `logs/` 目录保留最近 7 天的日志，超出此期限的文件在每次启动时自动删除。

### WSL2 VM 保活机制

WSL2 保活逻辑独立在 **`keep_wsl.py`** 模块中（与"换 API 后端"正交，故从 launch.py 解耦；launch.py 启动时 `import keep_wsl` 调用，也可 `python src/keep_wsl.py` 单独运行只保活 WSL）。`wsl -e sleep infinity` 可以维持 VM 存活，但 `wsl.exe` 是控制台子系统程序，即使使用 `CREATE_NO_WINDOW` 也无法隐藏其窗口。解决方案：通过 VBScript 的 `WScript.Shell.Run(hidden)` 启动 WSL，再调用 `cscript.exe` 执行该 VBS。

### 指定 Claude 启动方式

`config.yaml` 中支持两种方式，按优先级：

1. `app.app_id` — Windows App User Model ID（推荐，稳定）
2. `paths.claude_exe` — Claude EXE 的绝对路径（不推荐，可能因更新导致路径失效）

### 查询 Claude 的 App User Model ID

Claude 桌面版通常通过 Windows 应用商店／Sparse 包安装，路径会随版本变化。推荐使用 App User Model ID 启动，不受更新影响。

在 **PowerShell** 中执行以下命令查询：

```powershell
Get-StartApps | Where-Object { $_.Name -like "*Claude*" }
```

输出示例：

```
AppID                                    Name
-----                                    ----
Claude_pzs8sxrjxfjjc!Claude             Claude
```

将输出的 `AppID`（如 `Claude_pzs8sxrjxfjjc!Claude`）填入 `config.yaml` 的 `app.app_id` 字段即可。

如果未查到结果，可尝试模糊搜索：

```powershell
Get-StartApps | Where-Object { $_.Name -match "Claude|claude" } | Format-List
```

或列出全部已安装应用，手工查找：

```powershell
Get-StartApps | Out-GridView
```
