#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享工具模块 — 日志、端口检查、进程管理、常量。
launch.py 与 local_proxy.py 的公共依赖。
"""

import os
import re
import sys
import time
import socket
import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple

# ============================================================================
# 常量
# ============================================================================

# 模型 tier 关键字（按优先级排列：长关键字优先，避免 "sonnet" 误匹配 "haiku"）
TIERS = ('opus', 'sonnet', 'haiku')
DEFAULT_TIER = 'haiku'

# 网络
DEFAULT_HOST = '127.0.0.1'
PORT_CHECK_TIMEOUT = 1.0          # 端口探测超时（秒）
PORT_WAIT_INTERVAL = 0.5          # 端口轮询间隔（秒）
PORT_WAIT_TIMEOUT = 30            # 端口等待超时（秒）
DEFAULT_PORT_WAIT_TIMEOUT = PORT_WAIT_TIMEOUT  # 默认端口等待超时（秒）

# 进程
PROCESS_TERMINATE_TIMEOUT = 5     # terminate 后等待超时（秒）

# 代理
MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024  # do_POST 最大请求体 10 MiB

# WSL
WSL_VERIFY_ATTEMPTS = 5
WSL_VERIFY_INTERVAL = 0.5

# ============================================================================
# stdout/stderr 编码修复
# ============================================================================

def _fix_stdio_encoding():
    """修复 stdout/stderr 编码，避免重定向到文件时中文乱码。"""
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

_fix_stdio_encoding()

# ============================================================================
# ANSI / emoji 清理（用于文件日志）
# ============================================================================

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
_EMOJI_RE = re.compile(r'[\U00010000-\U0010FFFF\U0000FE00-\U0000FE0F]')

def strip_ansi_and_emoji(text: str) -> str:
    """去除 ANSI SGR 转义序列与非 BMP 字符（含大部分 emoji）。"""
    text = _ANSI_RE.sub('', text)
    text = _EMOJI_RE.sub('', text)
    return text


class _CleanFileFormatter(logging.Formatter):
    """文件日志 Formatter：自动去除 ANSI 颜色码和 emoji。"""
    def format(self, record):
        return strip_ansi_and_emoji(super().format(record))


# ============================================================================
# 日志清理
# ============================================================================

LOG_RETENTION_DAYS = 7  # 日志保留天数

def cleanup_old_logs(log_dir: Path, prefix: str, keep_days: int = LOG_RETENTION_DAYS):
    """删除超过 keep_days 天的旧日志文件。"""
    if not log_dir.exists():
        return
    cutoff = time.time() - keep_days * 86400
    log_pattern = f"{prefix}_*.log"
    deleted = 0
    for f in sorted(log_dir.glob(log_pattern)):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        logging.getLogger(__name__).info(f"清理了 {deleted} 个超过 {keep_days} 天的旧日志")


# ============================================================================
# 统一日志配置
# ============================================================================

def setup_logging(
    name: str,
    log_prefix: str = 'app',
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    console_fmt: str = '%(asctime)s - %(levelname)s - %(message)s',
    file_fmt: str = '%(asctime)s - %(levelname)s - %(message)s',
    clean_file: bool = True,
) -> logging.Logger:
    """
    配置双输出日志（控制台 + 按日滚动的文件），返回 logger。

    参数:
        name: logger 名称（通常 __name__）。
        log_prefix: 日志文件名前缀，生成 `<prefix>_YYYYMMDD.log`。
        console_level: 控制台最低级别。
        file_level: 文件最低级别。
        console_fmt: 控制台格式。
        file_fmt: 文件格式。
        clean_file: True 时文件日志去除 ANSI/emoji。
    """
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{log_prefix}_{time.strftime('%Y%m%d')}.log"

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter(console_fmt))
    console_handler.setLevel(console_level)

    # 文件 handler（UTF-8，追加模式）
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_formatter_cls = _CleanFileFormatter if clean_file else logging.Formatter
    file_handler.setFormatter(file_formatter_cls(file_fmt))
    file_handler.setLevel(file_level)

    logging.basicConfig(
        level=min(console_level, file_level),
        handlers=[console_handler, file_handler],
        force=True,  # 覆盖已有配置
    )
    logger = logging.getLogger(name)
    logger.info(f"日志文件: {log_file}")
    cleanup_old_logs(log_dir, log_prefix)
    return logger


# ============================================================================
# 端口工具
# ============================================================================

def is_port_listening(port: int, host: str = DEFAULT_HOST) -> bool:
    """检查指定端口是否在监听（TCP connect 探测）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(PORT_CHECK_TIMEOUT)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def wait_for_port(
    port: int,
    timeout: int = DEFAULT_PORT_WAIT_TIMEOUT,
    interval: float = PORT_WAIT_INTERVAL,
    label: str = '',
) -> bool:
    """
    轮询等待端口就绪。

    返回: True 就绪，False 超时。
    """
    if port <= 0:
        return True

    tag = f" ({label})" if label else ""
    logger = logging.getLogger(__name__)
    logger.info(f"等待端口 {port}{tag} 启动...")
    start = time.time()

    while time.time() - start < timeout:
        if is_port_listening(port):
            logger.info(f"端口 {port}{tag} 已启动")
            return True
        time.sleep(interval)
        elapsed = int(time.time() - start)
        logger.info(f"   等待中... ({elapsed}s/{timeout}s)")

    logger.error(f"端口 {port}{tag} 在 {timeout}s 内未能启动")
    return False


# ============================================================================
# 进程工具
# ============================================================================

def terminate_process(proc: Optional[subprocess.Popen], label: str = '') -> bool:
    """
    优雅终止一个 Popen 进程：terminate → wait(timeout) → kill。

    返回: True 成功终止，False 进程已退出或操作失败。
    """
    if proc is None:
        return False
    if proc.poll() is not None:
        return False  # 已退出

    tag = f" ({label})" if label else ""
    logger = logging.getLogger(__name__)
    try:
        logger.info(f"终止进程{tag} (PID: {proc.pid})...")
        proc.terminate()
        try:
            proc.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
            logger.info(f"进程{tag} 已终止")
            return True
        except subprocess.TimeoutExpired:
            logger.warning(f"进程{tag} 未响应 terminate，强制 kill")
            proc.kill()
            proc.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
            return True
    except Exception as e:
        logger.error(f"终止进程{tag} 失败: {e}")
        return False


def start_process(
    cmd: list,
    cwd: Optional[str] = None,
    hide_window: bool = True,
    stdin: Optional[int] = None,
) -> Optional[subprocess.Popen]:
    """
    启动子进程，返回 Popen 对象。失败返回 None。

    hide_window: Windows 下隐藏控制台窗口。
    stdin: 标准输入，默认 None（继承父进程）。
    """
    try:
        kwargs = dict(
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if stdin is not None:
            kwargs['stdin'] = stdin
        if hide_window and os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        if cwd:
            kwargs['cwd'] = cwd
        return subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        logging.getLogger(__name__).error(f"启动进程失败: {' '.join(cmd)} — {e}")
        return None


# ============================================================================
# 配置验证
# ============================================================================

def validate_config_schema(config: dict, required_keys: Tuple[str, ...], context: str = '') -> bool:
    """
    检查 config 字典中 required_keys 点路径是否存在（如 'paths.python'）。

    缺失时自动记录 error 日志并返回 False。
    """
    logger = logging.getLogger(__name__)
    ok = True
    for key_path in required_keys:
        parts = key_path.split('.')
        node = config
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                logger.error(f"[{context}] 配置缺少必要字段: {key_path}")
                ok = False
                break
    return ok
