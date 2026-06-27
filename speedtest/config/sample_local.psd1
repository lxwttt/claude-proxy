# sample_local.psd1 — 机器私有配置模板
# 复制为 local.psd1 后按本机实际填写。local.psd1 已 gitignore、不入库（含订阅/安装路径等隐私）。
# 路径值支持 %ENV% 环境变量：脚本读入后用 [Environment]::ExpandEnvironmentVariables 展开。
@{
    # Clash Verge 当前订阅 profile 路径（含订阅特征，机器/订阅特定）。
    # 订阅重订后文件名会变：去 %APPDATA%\io.github.clash-verge-rev.clash-verge-rev\profiles\ 找最新的 .yaml。
    VergeProfile      = '%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\profiles\YOUR_PROFILE_ID.yaml'

    # mihomo 内核可执行文件（Clash Verge 安装位置，机器特定）。
    MihomoExe         = 'C:\Program Files\Clash Verge\verge-mihomo.exe'

    # 节点出站绑定的物理网卡名（绕过 TUN 全局劫持）；置空字符串 '' 则不注入 interface-name。
    OutboundInterface = 'WLAN'
}
