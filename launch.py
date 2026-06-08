#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Auto-Launcher - 支持额外EXE启动的版本
"""

import os
import sys
import time
import yaml
import subprocess
import logging
import msvcrt
import requests
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

from common import (
    is_port_listening,
    wait_for_port,
    terminate_process,
    start_process,
    setup_logging,
    validate_config_schema,
    TIERS,
    DEFAULT_TIER,
    WSL_VERIFY_ATTEMPTS,
    WSL_VERIFY_INTERVAL,
)

# ========== 常量 ==========
PORT_WAIT_TIMEOUT = 30

# ========== 日志 ==========
logger = setup_logging(__name__, log_prefix='launcher', console_level=logging.INFO)


class ExtraExeManager:
    """管理额外需要启动的EXE程序"""

    def __init__(self):
        self.processes: List[subprocess.Popen] = []

    def start_extra_exes(self, extra_exes: List[Dict]) -> bool:
        """启动所有额外的EXE程序"""
        if not extra_exes:
            logger.info("没有需要启动的额外EXE程序")
            return True

        logger.info("=" * 50)
        logger.info("启动额外EXE程序")
        logger.info("=" * 50)

        for exe_config in extra_exes:
            name = exe_config.get('name', 'Unknown')
            path = exe_config.get('path', '')
            args = exe_config.get('args', '')
            wait_port = exe_config.get('wait_for_port', 0)
            description = exe_config.get('description', '')
            required = exe_config.get('required', False)

            logger.info(f"启动: {name}")
            logger.info(f"   路径: {path}")
            logger.info(f"   参数: {args}")
            logger.info(f"   描述: {description}")

            if not Path(path).exists():
                logger.error(f"文件不存在: {path}")
                if required:
                    return False
                continue

            cmd = [path]
            if args:
                cmd.extend(args.split())

            process = start_process(cmd)
            if process is None:
                logger.error(f"启动 {name} 失败")
                if required:
                    return False
                continue

            self.processes.append(process)
            logger.info(f"{name} 启动成功 (PID: {process.pid})")

            if wait_port > 0:
                if not wait_for_port(wait_port, timeout=PORT_WAIT_TIMEOUT):
                    if required:
                        logger.error(f"{name} 端口 {wait_port} 启动失败")
                        return False
                    else:
                        logger.warning(f"{name} 端口 {wait_port} 未启动，但继续执行")

            time.sleep(0.5)

        logger.info("所有额外EXE程序启动完成")
        return True

    def stop_all(self):
        """停止所有启动的EXE程序"""
        logger.info("停止所有额外EXE程序...")
        for process in self.processes:
            terminate_process(process)
        self.processes.clear()
        logger.info("所有额外EXE程序已停止")


class ClaudeLauncher:
    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.proxy_process: Optional[subprocess.Popen] = None
        self.extra_exe_manager = ExtraExeManager()

    def load_config(self) -> bool:
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                logger.error(f"配置文件不存在: {self.config_path}")
                return False

            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)

            logger.info("配置文件加载成功")
            return True
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return False

    # ------------------------------------------------------------------
    # WSL 管理 - 子方法
    # ------------------------------------------------------------------

    @staticmethod
    def _run_wsl(args, timeout, **kwargs):
        """统一执行 wsl 子命令。

        关键：始终带 CREATE_NO_WINDOW，让 wsl.exe 拥有独立控制台。
        否则 wsl.exe 共享父进程控制台，会在 VM 初始化的不稳定期向整个
        控制台进程组广播 CTRL_C/CTRL_BREAK，被 launch.py 当成伪 KeyboardInterrupt
        （历史问题：启动期莫名"收到中断信号"直接退出）。
        """
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        return subprocess.run(['wsl', *args], creationflags=creationflags, timeout=timeout, **kwargs)

    def _check_wsl_available(self) -> bool:
        """检查 wsl.exe 是否存在且可用。"""
        try:
            probe = self._run_wsl(
                ['--version'],
                capture_output=True, encoding='utf-8', errors='replace', timeout=10
            )
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

    def _ensure_wsl2_default(self) -> bool:
        """确认 WSL2 是默认版本（不是 WSL1）。"""
        # 1. 确认 WSL2 是默认版本
        status = self._run_wsl(
            ['--status'],
            capture_output=True, encoding='utf-8', errors='replace', timeout=10
        )
        if 'Default Version: 2' not in status.stdout:
            logger.warning("WSL 默认版本不是 2，VM 需要 WSL2")
            logger.info("尝试设置为 WSL2...")
            set_result = self._run_wsl(
                ['--set-default-version', '2'],
                capture_output=True, encoding='utf-8', errors='replace', timeout=30
            )
            if set_result.returncode != 0:
                logger.error("无法设置 WSL2 为默认版本，VM 不可用")
                return False
            logger.info("WSL2 已设为默认版本")
        return True

    def _ensure_wsl_distro_exists(self) -> bool:
        """检查 WSL 发行版是否已安装。"""
        distro_result = self._run_wsl(
            ['-l', '-v'],
            capture_output=True, encoding='utf-8', errors='replace', timeout=10
        )
        if distro_result.returncode == 0 and len(distro_result.stdout.strip().split('\n')) <= 1:
            logger.warning("没有安装任何 WSL 发行版，VM 需要至少一个发行版")
            logger.error("未安装 WSL 发行版，VM 功能不可用。请手动运行: wsl --install -d Ubuntu")
            return False
        return True

    def _start_wsl_keeper(self) -> bool:
        """用 VBScript 启动 WSL 后台进程（唯一能彻底隐藏控制台窗口的方式）。"""
        # WSL.exe 是控制台子系统程序，会自建 ConPTY 窗口，Popen 的 creationflags 无法抑制
        # VBScript Run(..., 0, False) → 隐藏窗口 (0) + 不等待 (False)
        logger.info("保持 WSL VM 后台运行...")
        try:
            import tempfile
            vbs_content = 'CreateObject("WScript.Shell").Run "wsl -e sleep infinity", 0, False'
            vbs_path = os.path.join(tempfile.gettempdir(), '_claude_wsl_keeper.vbs')
            with open(vbs_path, 'w') as f:
                f.write(vbs_content)
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
            logger.warning("WSL keep-alive 启动失败: {}".format(e))
            return False

    def _verify_wsl_running(self) -> bool:
        """验证 WSL 确实运行中（VBS 启动后需要等 WSL 初始化完成）。
        此方法绝不抛异常——任何错误都降级为 warning 并返回 False。"""
        for attempt in range(WSL_VERIFY_ATTEMPTS):
            try:
                verify = self._run_wsl(['-l', '--running'], capture_output=True, timeout=10)
            except Exception as e:
                logger.warning(f"WSL 状态查询失败（{attempt + 1}/{WSL_VERIFY_ATTEMPTS}）: {e}")
                time.sleep(WSL_VERIFY_INTERVAL)
                continue
            if verify.returncode == 0:
                logger.info("WSL 2 VM 运行确认成功")
                return True
            logger.warning(f"WSL 尚无运行中的发行版（{attempt + 1}/{WSL_VERIFY_ATTEMPTS}），重试...")
            time.sleep(WSL_VERIFY_INTERVAL)

        logger.warning("WSL 运行验证超时（{}次尝试均失败），但 WSL 可能仍在初始化中".format(WSL_VERIFY_ATTEMPTS))
        return False

    # ------------------------------------------------------------------
    # WSL 管理 - 编排
    # ------------------------------------------------------------------

    def ensure_wsl_running(self) -> bool:
        """确保 WSL2 VM 后台持续运行（Claude VM 沙盒的必要前提），仅 Windows 生效"""
        if os.name != 'nt':
            return True

        if not self._check_wsl_available():
            return False

        if not self._ensure_wsl2_default():
            return False

        if not self._ensure_wsl_distro_exists():
            return False

        if not self._start_wsl_keeper():
            return False

        # 前 4 步是硬性要求（失败已 return False）；此步仅最终确认：
        # WSL 异步初始化可能较慢，keeper 已启动，确认不到只告警不中止，避免误杀慢启动
        self._verify_wsl_running()
        return True

    def stop_wsl_keeper(self):
        """停止 WSL 后台 VM（VBS 启动的 sleep infinity 不是 Python 子进程，直接用 shutdown）"""
        logger.info("停止 WSL VM...")
        try:
            self._run_wsl(['--shutdown'], capture_output=True, timeout=15)
            logger.info("WSL VM 已停止")
        except Exception as e:
            logger.warning("WSL shutdown 失败: {}".format(e))

    # ------------------------------------------------------------------
    # 代理管理
    # ------------------------------------------------------------------

    def start_proxy(self) -> bool:
        try:
            python_path = self.config['paths']['python']
            proxy_script = self.config['paths']['proxy_script']

            if not Path(python_path).exists():
                logger.error(f"Python 不存在: {python_path}")
                return False

            if not Path(proxy_script).exists():
                logger.error(f"代理脚本不存在: {proxy_script}")
                return False

            logger.info(f"启动透明代理...")
            logger.info(f"   Python: {python_path}")
            logger.info(f"   脚本: {proxy_script}")

            self.proxy_process = start_process(
                [python_path, proxy_script],
                cwd=str(Path(proxy_script).parent),
                stdin=subprocess.DEVNULL,
            )
            if self.proxy_process is None:
                logger.error("代理进程启动失败")
                return False

            logger.info(f"   代理进程 PID: {self.proxy_process.pid}")

            proxy_port = self.config['ports']['proxy_port']
            if not wait_for_port(proxy_port, label='proxy'):
                logger.error(f"代理在 {PORT_WAIT_TIMEOUT} 秒内未能启动")
                self.stop_proxy()
                return False

            return True

        except Exception as e:
            logger.error(f"启动代理失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def stop_proxy(self):
        if self.proxy_process and self.proxy_process.poll() is None:
            logger.info(f"停止代理进程 (PID: {self.proxy_process.pid})...")
            terminate_process(self.proxy_process, label='proxy')
            logger.info("代理已停止")
            self.proxy_process = None

    def launch_claude(self) -> bool:
        try:
            app_id = self.config.get('app', {}).get('app_id')
            claude_exe = self.config.get('paths', {}).get('claude_exe')

            if app_id:
                logger.info(f"通过 AppID 启动 Claude: {app_id}")
                proc = start_process(['explorer.exe', f'shell:AppsFolder\\{app_id}'], hide_window=True)
                if proc is None:
                    logger.error("Claude AppID 启动失败")
                    return False
                logger.info("Claude 已通过 AppID 启动")
                return True
            elif claude_exe and Path(claude_exe).exists():
                logger.info(f"通过 EXE 启动 Claude: {claude_exe}")
                proc = start_process([claude_exe], hide_window=True)
                if proc is None:
                    logger.error("Claude EXE 启动失败")
                    return False
                logger.info("Claude 已通过 EXE 启动")
                return True
            else:
                logger.error("未找到有效的 Claude 启动方式")
                return False
        except Exception as e:
            logger.error(f"启动 Claude 失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 清理与运行
    # ------------------------------------------------------------------

    def _cleanup(self):
        """清理所有资源：停止代理、WSL、额外EXE"""
        self.stop_proxy()
        self.stop_wsl_keeper()
        self.extra_exe_manager.stop_all()

    def run(self):
        logger.info("=" * 50)
        logger.info("   Claude 自动启动器")
        logger.info("=" * 50)

        # 启动阶段整体包裹：任何未预期异常（含启动期 Ctrl+C / 伪 KeyboardInterrupt）
        # 都清理已启动组件后退出，绝不留下孤儿进程、绝不静默崩溃
        try:
            # 0. 确保 WSL 已就绪——Windows 上 Claude VM 沙盒必需，失败即中止
            #    （ensure_wsl_running 在非 Windows 直接返回 True，不影响其他平台）
            if not self.ensure_wsl_running():
                logger.error("WSL 未就绪——Windows 上 Claude VM 沙盒依赖 WSL，无法继续")
                self._cleanup()
                return False

            # 1. 加载配置
            if not self.load_config():
                self._cleanup()
                return False

            # 2. 配置验证（不阻止启动）
            if not validate_config_schema(self.config, ('paths.python', 'paths.proxy_script', 'ports.proxy_port'), context='launcher'):
                logger.warning("配置验证失败，但继续尝试启动")

            # 3. 设置代理环境变量
            if self.config.get('proxy_settings', {}).get('enabled', False):
                os.environ['HTTP_PROXY'] = self.config['proxy_settings']['http_proxy']
                os.environ['HTTPS_PROXY'] = self.config['proxy_settings']['https_proxy']
                logger.info("设置代理环境变量")

            # 4. 启动额外EXE程序
            extra_exes = self.config.get('extra_exes', [])
            if not self.extra_exe_manager.start_extra_exes(extra_exes):
                logger.error("额外EXE程序启动失败")
                self._cleanup()
                return False

            # 5. 启动透明代理
            if not self.start_proxy():
                self._cleanup()
                return False

            # 6. 启动 Claude
            if not self.launch_claude():
                self._cleanup()
                return False
        except KeyboardInterrupt:
            logger.warning("启动期间收到中断信号，正在清理已启动组件...")
            self._cleanup()
            return False
        except Exception:
            logger.error("启动期间发生未预期异常，正在清理后退出", exc_info=True)
            self._cleanup()
            return False

        logger.info("=" * 50)
        logger.info("所有组件已成功启动！")
        logger.info("   按 Ctrl+C 停止所有程序")
        logger.info("=" * 50)

        # 主循环：轮询按键 + 响应 Ctrl+C + 子进程存活监控
        _reload_url = f"http://127.0.0.1:{self.config['ports']['proxy_port']}/reload"
        logger.info("   按 r 刷新代理配置 | 按 Ctrl+C 停止所有程序")

        _ok = True
        _health_tick = 0
        try:
            while True:
                if msvcrt.kbhit():
                    key = msvcrt.getch().lower()
                    if key == b'r':
                        logger.info("正在刷新代理配置...")
                        try:
                            resp = requests.get(_reload_url, timeout=5)
                            if resp.status_code == 200:
                                logger.info("[OK] 代理配置已刷新")
                            else:
                                logger.warning("[FAIL] 代理配置刷新失败")
                        except Exception as e:
                            logger.warning(f"[FAIL] 无法连接代理: {e}")

                # 每 5 秒检查子进程存活
                _health_tick += 1
                if _health_tick >= 50:
                    _health_tick = 0
                    if self.proxy_process and self.proxy_process.poll() is not None:
                        logger.critical("代理进程意外退出！正在停止所有组件...")
                        _ok = False
                        break
                    if not is_port_listening(self.config['ports']['proxy_port']):
                        logger.critical("代理端口无响应！正在停止所有组件...")
                        _ok = False
                        break

                time.sleep(0.1)
        except KeyboardInterrupt:
            logger.info("\n收到中断信号...")
            _ok = False
        finally:
            self._cleanup()

        return _ok


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    launcher = ClaudeLauncher(config_path)
    success = launcher.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
