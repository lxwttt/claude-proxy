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
    PORT_WAIT_TIMEOUT,
)
import keep_wsl

# ========== 常量 ==========
HEALTH_CHECK_INTERVAL = 5      # 代理存活检查间隔（秒）
PROXY_PORT_FAIL_LIMIT = 3      # 端口连续无响应多少次才判定代理失效（去抖，防瞬时抖动误杀）

# ========== 日志 ==========
logger = setup_logging(__name__, log_prefix='launcher', console_level=logging.INFO)


class ExtraExeManager:
    """管理额外需要启动的EXE程序"""

    def __init__(self):
        self.processes: List[subprocess.Popen] = []
        self.required: List = []  # [(name, Popen)] 必选项，供主循环存活监控

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
            if required:
                self.required.append((name, process))
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

    def dead_required(self) -> List[str]:
        """返回已退出的必选 EXE 名（poll() 非 None）。供主循环判定关键依赖是否失效。"""
        return [name for name, proc in self.required if proc.poll() is not None]

    def stop_all(self):
        """停止所有启动的EXE程序"""
        logger.info("停止所有额外EXE程序...")
        for process in self.processes:
            terminate_process(process)
        self.processes.clear()
        self.required.clear()
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
    # 代理管理
    # ------------------------------------------------------------------

    def start_proxy(self) -> bool:
        try:
            python_path = self.config['paths']['python']
            proxy_script = self.config['paths']['proxy_script']
            # 相对路径以本文件目录为基准 resolve，不依赖调用方 CWD（与 local_proxy 的 __file__ 锚定一致）
            if not Path(proxy_script).is_absolute():
                proxy_script = str((Path(__file__).parent / proxy_script).resolve())
            proxy_port = self.config['ports']['proxy_port']

            if not Path(python_path).exists():
                logger.error(f"Python 不存在: {python_path}")
                return False

            if not Path(proxy_script).exists():
                logger.error(f"代理脚本不存在: {proxy_script}")
                return False

            logger.info(f"启动转发网关...")
            logger.info(f"   Python: {python_path}")
            logger.info(f"   脚本: {proxy_script}")

            # 端口作为命令行参数传入：config.yaml ports.proxy_port 为单一事实来源
            self.proxy_process = start_process(
                [python_path, proxy_script, str(proxy_port)],
                cwd=str(Path(proxy_script).parent),
                stdin=subprocess.DEVNULL,
            )
            if self.proxy_process is None:
                logger.error("代理进程启动失败")
                return False

            logger.info(f"   代理进程 PID: {self.proxy_process.pid}")

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
        """清理所有资源：停止代理、WSL、额外EXE。每步独立兜底（含 KeyboardInterrupt），
        二次 Ctrl+C 或某步异常不影响其余步骤全部执行，确保不留孤儿进程。"""
        for step in (self.stop_proxy, keep_wsl.stop_wsl_keeper, self.extra_exe_manager.stop_all):
            try:
                step()
            except BaseException as e:
                logger.warning(f"清理步骤 {getattr(step, '__name__', step)} 出错（已跳过继续）: {e}")

    def run(self):
        logger.info("=" * 50)
        logger.info("   Claude 自动启动器")
        logger.info("=" * 50)

        # 启动阶段整体包裹：任何未预期异常（含启动期 Ctrl+C / 伪 KeyboardInterrupt）
        # 都清理已启动组件后退出，绝不留下孤儿进程、绝不静默崩溃
        try:
            # 0. 确保 WSL 已就绪——Windows 上 Claude VM 沙盒必需，失败即中止
            #    （keep_wsl.ensure_wsl_running 在非 Windows 直接返回 True，不影响其他平台）
            if not keep_wsl.ensure_wsl_running():
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
                # 本机回环流量旁路代理，避免 launcher 对 127.0.0.1 的调用被误路由经 VPN
                os.environ['NO_PROXY'] = '127.0.0.1,localhost'
                logger.info("设置代理环境变量")

            # 4. 启动额外EXE程序
            extra_exes = self.config.get('extra_exes', [])
            if not self.extra_exe_manager.start_extra_exes(extra_exes):
                logger.error("额外EXE程序启动失败")
                self._cleanup()
                return False

            # 5. 启动转发网关
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
        _last_health = time.time()
        _port_fails = 0
        try:
            while True:
                if msvcrt.kbhit():
                    key = msvcrt.getch().lower()
                    if key == b'r':
                        logger.info("正在刷新代理配置...")
                        try:
                            resp = requests.get(_reload_url, timeout=5,
                                                proxies={'http': None, 'https': None})
                            if resp.status_code == 200:
                                logger.info("[OK] 代理配置已刷新")
                            else:
                                logger.warning("[FAIL] 代理配置刷新失败")
                        except Exception as e:
                            logger.warning(f"[FAIL] 无法连接代理: {e}")

                # 定期检查代理存活
                if time.time() - _last_health >= HEALTH_CHECK_INTERVAL:
                    _last_health = time.time()
                    # 进程退出是确定信号，立即判定
                    if self.proxy_process and self.proxy_process.poll() is not None:
                        logger.critical("代理进程意外退出！正在停止所有组件...")
                        _ok = False
                        break
                    # 必选附加程序（如 VPN）退出也是确定信号，立即判定
                    dead = self.extra_exe_manager.dead_required()
                    if dead:
                        logger.critical(f"必选附加程序意外退出: {', '.join(dead)}！正在停止所有组件...")
                        _ok = False
                        break
                    # 注意：TCP 探活只代表代理进程存活，不代表上游链路可用（上游全挂时代理仍 listen 并回 500）
                    # 端口无响应可能是瞬时抖动，需连续多次失败才判死，避免误杀
                    if is_port_listening(self.config['ports']['proxy_port']):
                        _port_fails = 0
                    else:
                        _port_fails += 1
                        logger.warning(f"代理端口无响应（{_port_fails}/{PROXY_PORT_FAIL_LIMIT}）...")
                        if _port_fails >= PROXY_PORT_FAIL_LIMIT:
                            logger.critical("代理端口连续无响应，正在停止所有组件...")
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
