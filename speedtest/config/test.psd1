# test.psd1 — 测速参数（可入库，非隐私）。改这里调测试行为，不必动脚本。
@{
    # ---- 延迟测试 ----
    HandshakeUrl   = 'https://api.anthropic.com/v1/models'    # 阶段1 fast 握手探活 URL（必须带路径）
    ApiUrl         = 'https://api.anthropic.com/v1/messages'  # 阶段2 真实消息端点（OAuth 流式）
    Model          = 'claude-haiku-4-5'                       # 阶段2 用的模型
    MaxTokens      = 16                                       # 阶段2 max_tokens（够测首字节即可）

    # 隔离临时 mihomo 内核端口（不碰线上 Verge 的 7897/9097）
    MixedPort      = 18897
    CtrlPort       = 18898

    # 429 账号级限流：指数退避重试 + AIMD 自适应降频
    Max429Retry    = 3      # 单次探测遇 429 的最大重试次数
    Backoff429Base = 5      # 退避基数秒（指数 5→10→20）
    Backoff429Max  = 30     # 单次退避上限秒
    ProbeDelayMs   = 300    # 探测间隔基线（自适应起点）
    PaceStep       = 500    # AIMD：遇 429 探测间隔增量(ms)
    PaceMax        = 3000   # 探测间隔上限(ms)

    # ---- 下载测试 ----
    DownloadUrl    = 'https://downloads.claude.ai/vms/linux/x64/c9b42670eaedf20c7035b018c904a0c6a3cb864f/rootfs.vhdx.zst'
    DownloadSize   = 104857600   # 100 MB
    Timeout        = '30s'
    Concurrent     = 4
}
