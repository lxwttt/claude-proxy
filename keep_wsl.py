#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WSL2 VM 保活 —— 从 launch.py 解耦的独立模块/脚本。

Claude 桌面版的沙盒/代码执行功能依赖 WSL2 后台运行；这与"让 Claude 用第三方 API 后端"
完全正交，故独立成模块：launch.py 直接 `import keep_wsl` 调用，也可单独
`python keep_wsl.py` 运行（仅保活 WSL，不启动代理/Claude）。

行为与原 launch.py 内嵌实现一致；仅 Windows 生效。
"""
import os
import time
import logging
import tempfile
import subprocess

from common import WSL_VERIFY_ATTEMPTS, WSL_VERIFY_INTERVAL

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())  # 库约定：未配置日志的调用方 import 时不向 stderr 喷日志

# 本次运行中 WSL2 VM 是否由本程序拉起（启动前未运行）；决定退出时是否 --shutdown
_self_started = False


def _run_wsl(args, timeout, **kwargs):
    """统一执行 wsl 子命令。

    关键：始终带 CREATE_NO_WINDOW，让 wsl.exe 拥有独立控制台。否则 wsl.exe 共享父进程
    控制台，会在 VM 初始化的不稳定期向整个控制台进程组广播 CTRL_C/CTRL_BREAK，被
    launch.py 当成伪 KeyboardInterrupt（历史问题：启动期莫名"收到中断信号"直接退出）。
    """
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.run(['wsl', *args], creationflags=creationflags, timeout=timeout, **kwargs)


def _run_wsl_safe(args, timeout, **kwargs):
    """运行 wsl 子命令，异常时记录 warning 并返回 None（统一错误处理，避免各调用方重复 try/except）。"""
    try:
        return _run_wsl(args, timeout=timeout, **kwargs)
    except Exception as e:
        logger.warning(f"wsl {' '.join(args)} 执行失败: {e}")
        return None


def _check_wsl_available() -> bool:
    """检查 wsl.exe 是否存在且可用。"""
    try:
        probe = _run_wsl(['--version'], capture_output=True, encoding='utf-8', errors='replace', timeout=10)
        if probe.returncode != 0:
            logger.warning("WSL 未安装或不可用，VM 功能将无法启动")
            return False
    except FileNotFoundError:
        logger.info("未找到 WSL，跳过")
        return False
    except Exception:
        logger.warning("WSL 探测失败，VM 功能将无法启动")
        return False
    return True


def _ensure_wsl2_default() -> bool:
    """确保 WSL2 为默认版本。直接幂等设置，避免解析 wsl --status 的本地化/UTF-16 输出。"""
    result = _run_wsl_safe(['--set-default-version', '2'], timeout=30,
                           capture_output=True, encoding='utf-8', errors='replace')
    if result is None:
        return False
    if result.returncode != 0:
        logger.error("无法设置 WSL2 为默认版本，VM 不可用")
        return False
    return True


def _ensure_wsl_distro_exists() -> bool:
    """检查 WSL 发行版是否已安装。"""
    # wsl -l -v 输出为 UTF-16；显式指定编码，否则按 utf-8 解码得到夹 NUL 的乱码、行数判断失真
    result = _run_wsl_safe(['-l', '-v'], timeout=10, capture_output=True, encoding='utf-16', errors='replace')
    if result is None:
        return False
    # 首行为表头，其后每行一个发行版；按非空数据行数判断
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if result.returncode == 0 and len(lines) <= 1:
        logger.error("未安装 WSL 发行版，VM 功能不可用。请手动运行: wsl --install -d Ubuntu")
        return False
    return True


def _start_wsl_keeper() -> bool:
    """用 VBScript 启动 WSL 后台进程（唯一能彻底隐藏控制台窗口的方式）。"""
    # WSL.exe 是控制台子系统程序，会自建 ConPTY 窗口，Popen 的 creationflags 无法抑制
    # VBScript Run(..., 0, False) → 隐藏窗口 (0) + 不等待 (False)
    logger.info("保持 WSL VM 后台运行...")
    vbs_content = 'CreateObject("WScript.Shell").Run "wsl -e sleep infinity", 0, False'
    vbs_path = None
    try:
        # O_EXCL + 随机名独占创建，避免固定可预测名被预创建/符号链接抢占替换
        fd, vbs_path = tempfile.mkstemp(suffix='.vbs', prefix='_claude_wsl_keeper_')
        with os.fdopen(fd, 'w') as f:
            f.write(vbs_content)
        # subprocess.run 阻塞至 cscript 读完并退出（VBS 的 Run(...,False) 已异步拉起 wsl）
        subprocess.run(
            ['cscript.exe', '//NoLogo', '//B', vbs_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        )
        logger.info("WSL keep-alive 已通过 VBS 后台启动")
        return True
    except Exception as e:
        logger.warning(f"WSL keep-alive 启动失败: {e}")
        return False
    finally:
        if vbs_path:
            try:
                os.remove(vbs_path)
            except OSError:
                pass


def _verify_wsl_running() -> bool:
    """验证 WSL 确实运行中（VBS 启动后需要等 WSL 初始化完成）。
    除 KeyboardInterrupt 外不抛异常——其余错误降级为 warning 并返回 False（让真正的中断能传播到上层清理）。"""
    for attempt in range(WSL_VERIFY_ATTEMPTS):
        try:
            verify = _run_wsl(['-l', '--running'], capture_output=True, timeout=10)
        except Exception as e:
            logger.warning(f"WSL 状态查询失败（{attempt + 1}/{WSL_VERIFY_ATTEMPTS}）: {e}")
            time.sleep(WSL_VERIFY_INTERVAL)
            continue
        if verify.returncode == 0:
            logger.info("WSL 2 VM 运行确认成功")
            return True
        logger.warning(f"WSL 尚无运行中的发行版（{attempt + 1}/{WSL_VERIFY_ATTEMPTS}），重试...")
        time.sleep(WSL_VERIFY_INTERVAL)

    logger.warning(f"WSL 运行验证超时（{WSL_VERIFY_ATTEMPTS}次尝试均失败），但 WSL 可能仍在初始化中")
    return False


def _is_wsl_running() -> bool:
    """WSL 是否已有运行中的发行版（-q 仅输出发行版名，无则空），用于判断 VM 是否本程序拉起。"""
    result = _run_wsl_safe(['-l', '--running', '-q'], timeout=10, capture_output=True, encoding='utf-16', errors='replace')
    return bool(result and result.returncode == 0 and any(ln.strip() for ln in result.stdout.splitlines()))


def ensure_wsl_running() -> bool:
    """确保 WSL2 VM 后台持续运行（Claude VM 沙盒的必要前提），仅 Windows 生效。"""
    global _self_started
    if os.name != 'nt':
        return True

    if not _check_wsl_available():
        return False
    if not _ensure_wsl2_default():
        return False
    if not _ensure_wsl_distro_exists():
        return False
    # 记录 VM 是否本程序拉起：启动前已在运行则退出时不 --shutdown（避免殃及用户其它发行版/工作）
    _self_started = not _is_wsl_running()
    if not _start_wsl_keeper():
        return False

    # 前 4 步是硬性要求（失败已 return False）；此步仅最终确认：
    # WSL 异步初始化可能较慢，keeper 已启动，确认不到只告警不中止，避免误杀慢启动
    _verify_wsl_running()
    return True


def stop_wsl_keeper():
    """停止 WSL 后台 VM。仅当 VM 由本程序拉起（_self_started）时才 --shutdown；
    否则保留（启动前用户已在用），避免越权关停整机 WSL2 殃及其它发行版/Docker。"""
    if not _self_started:
        logger.info("WSL 启动前已在运行，退出时不关停（避免影响用户其它发行版）")
        return
    logger.info("停止 WSL VM...")
    if _run_wsl_safe(['--shutdown'], timeout=15, capture_output=True) is not None:
        logger.info("WSL VM 已停止")


if __name__ == '__main__':
    from common import setup_logging
    setup_logging(__name__, log_prefix='launcher', console_level=logging.INFO)
    logger.info("独立运行：仅保活 WSL VM")
    ensure_wsl_running()
