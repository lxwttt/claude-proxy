# Speedtest — 给 Claude 挑节点

对订阅节点发真实 `/v1/messages` 请求排名，挑最稳的喂给 Claude。

## 前置

- `clash-speedtest` 在 PATH
- 已装 Clash Verge（用 `verge-mihomo.exe`）
- 登录过 Claude Code（复用 `~/.claude/.credentials.json`）

## 快速开始

### 配置

```powershell
# 1. 本机私有配置
cp config/sample_local.psd1 config/local.psd1
# 编辑 config/local.psd1，填 profile 路径、mihomo 位置、网卡名

# 2. 节点筛选（可选）
# 编辑 in/filter.txt，写节点名正则
```

### 运行

```powershell
.\claude-speedtest.ps1                  # 延迟 + 下载全测
.\claude-speedtest.ps1 -Mode latency    # 只测延迟
.\claude-speedtest.ps1 -Mode download   # 只测下载
.\claude-speedtest.ps1 -Samples 5       # 每节点 5 次采样
.\claude-speedtest.ps1 -Reset all       # 清历史重来
```

跑完看 `result_combined.md` —— 越靠前越推荐。

## 文件

| 路径 | 用途 | 入库 |
|---|---|---|
| `claude-speedtest.ps1` | 主脚本 | ✅ |
| `merge.ps1` | 跨轮汇总排名 | ✅ |
| `config/test.psd1` | 测试参数 | ✅ |
| `config/sample_local.psd1` | 私有配置模板 | ✅ |
| `config/local.psd1` | 私有配置 | 🚫 |
| `in/input.yaml` | 节点列表 | 🚫 |
| `out/` | 中间产物 | 🚫 |
| `result_*.md` | 排名报告 | 🚫 |
