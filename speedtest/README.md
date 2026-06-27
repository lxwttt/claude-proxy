# Speedtest — 给 Claude 挑节点

给"订阅节点 → Claude 官方 API"的链路按**真实可用性**排名，帮你挑出最稳的节点喂给 Claude / 本项目代理用。

为什么不直接看 ping：有些节点"探活能通但真实流量被掐"（ping 绿、Claude 却连不上）。本工具会对存活节点再发一次**真实的 `/v1/messages` 请求**测首字节延迟，并跨多轮加权平滑，挑长期稳的。

## 前置

- `clash-speedtest` 在 PATH 中
- 已装 Clash Verge（用到 `verge-mihomo.exe`）
- 登录过 Claude Code（复用其订阅凭证 `~/.claude/.credentials.json` 发真实请求）

## 配置

1. **本机私有配置**：复制 `config/sample_local.psd1` 为 `config/local.psd1`，按本机填写 Clash Verge profile 路径、`verge-mihomo.exe` 位置、出站物理网卡名。（`local.psd1` 已 gitignore，不入库。）
2. **节点筛选**：`in/filter.txt` 写节点名正则（.NET 语法）。默认 `^((?!HK|TW).)*\d+M$` = 排除港台、只要名字带 `<数字>M` 的。
3. **测试参数**（可选微调）：`config/test.psd1` —— 模型、采样端点、隔离端口、限流退避、下载项等。

> 订阅节点列表每次测速前会自动从 Clash Verge 当前 profile 同步，无需手动维护。

## 用法

```powershell
.\claude-speedtest.ps1                    # 延迟 + 下载全测（默认）
.\claude-speedtest.ps1 -Mode latency      # 只测延迟
.\claude-speedtest.ps1 -Mode download     # 只测下载
.\claude-speedtest.ps1 -Samples 5         # 每节点采样 5 次（默认 3，越多越稳但更耗额度）
.\claude-speedtest.ps1 -Reset all         # 清空历史重新开始（也可 -Reset latency/download）
```

跑完自动汇总。**看结果：`result_combined.md`** —— 跨轮加权排名，越靠前越推荐。

## 文件

| 路径 | 用途 |
|---|---|
| `claude-speedtest.ps1` | 主测速脚本 |
| `merge.ps1` | 跨轮汇总 + 加权排名（由主脚本自动调用） |
| `config/test.psd1` | 测试参数（可入库） |
| `config/sample_local.psd1` | 本机私有配置模板 |
| `config/local.psd1` | 本机私有配置（不入库） |
| `in/filter.txt` | 节点筛选正则 |
| `in/input.yaml` | 订阅配置（自动同步，不入库） |
| `out/` | 中间产物（不入库） |
| `result_combined.md` | 最终排名报告（不入库） |
