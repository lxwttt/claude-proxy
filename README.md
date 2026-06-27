# Claude Proxy

让 Claude 桌面版使用**第三方 / 订阅 API 后端**（DeepSeek、OpenAI，或复用你的 Claude 订阅）的一键启动工具。运行 `start_claude.cmd` 即自动备好一切并打开 Claude，退出时自动清理。

> 仅支持 **Windows**。

## 安装

需要 Python 3，然后装依赖：

```bash
pip install -r requirements.txt
```

## 配置（三步）

### 1. 启动器 — `config/config.yaml`

复制 `config/sample_config.yaml` 为 `config/config.yaml`，按本机填写：

```yaml
paths:
  python: "D:/anaconda3/python.exe"        # 你的 Python 路径
  proxy_script: "src/local_proxy.py"       # 保持不变
ports:
  proxy_port: 8899
app:
  app_id: "Claude_pzs8sxrjxfjjc!Claude"    # Claude 的 App User Model ID（见文末）
proxy_settings:
  enabled: true
  http_proxy: "http://127.0.0.1:10809"     # 让 Claude 走你的代理
  https_proxy: "http://127.0.0.1:10809"
extra_exes:                                # 要预启动的代理软件，不需要就设为 []
  - name: "VPN"
    path: "C:/path/to/vpn.exe"
    wait_for_port: 10809
    required: true
```

### 2. 后端 — `config/model_config.yaml`

复制 `config/sample_model_config.yaml` 为 `config/model_config.yaml`。`current_setting` 选用哪套，可放多套随时切换：

```yaml
current_setting: "DeepSeek"
settings:
  DeepSeek:
    api_base_url: "https://api.deepseek.com/anthropic"
    api_key: "sk-..."
    model_mapping:                 # Claude 模型 → 目标模型
      haiku: "deepseek-v4-flash"
      sonnet: "deepseek-v4-pro"
      opus: "deepseek-v4-pro"
```

也支持**订阅模式**（在该套配置里加 `auth_mode: "oauth"`）：直连官方 `api.anthropic.com`，复用本机 Claude Code 的订阅凭证，无需第三方 Key。

> ⚠️ 订阅模式属于绕过官方预期用法，**有触发风控/封号风险**，自行权衡。

### 3. 在 Claude Desktop 指向本代理 ← 关键开关

这一步决定 Claude 是否走代理，必须手动开：

**Claude Desktop → Settings/设置 → Developer/开发者 → Configure third-party inference/配置第三方推理**，把端点填成 **`http://127.0.0.1:8899`**（与 `proxy_port` 一致）。

不配这步，Claude 仍直连官方，代理收不到请求。

## 运行

双击 **`start_claude.cmd`**（或命令行 `python launch.py`）。保持窗口开着，运行中可按键：

| 按键 | 作用 |
|---|---|
| `r` | 改了 `model_config.yaml` 后热加载，不用重启 |
| `n` | 切换到下一套配置 |
| `q` | 正常退出并清理所有进程 |

## 提示

- `model_config.yaml` 里 `debug_mode: true` 会把**完整请求（含对话内容）明文**写进 `logs/`，日常建议设 `false`。
- 附带一个给 Claude 挑节点的测速工具，见 [`speedtest/README.md`](speedtest/README.md)。

## 附：查 App User Model ID

在 PowerShell 执行，把输出的 `AppID` 填进 `config.yaml` 的 `app.app_id`：

```powershell
Get-StartApps | Where-Object { $_.Name -like "*Claude*" }
```
