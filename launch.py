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
import socket
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

# ========== 修复 stdout/stderr 编码，避免重定向到文件时中文乱码 ==========
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ========== 自动文件日志 + 控制台输出 ==========
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)
_log_file = _log_dir / f"launcher_{time.strftime('%Y%m%d')}.log"

_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# 控制台 handler（stderr）
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_formatter)

# 文件 handler（UTF-8，每日追加）
_file_handler = logging.FileHandler(_log_file, encoding='utf-8')
_file_handler.setFormatter(_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger(__name__)
logger.info(f"日志文件: {_log_file}")


class ExtraExeManager:
    """管理额外需要启动的EXE程序"""

    def __init__(self):
        self.processes: List[subprocess.Popen] = []

    def is_port_listening(self, port: int, host: str = '127.0.0.1') -> bool:
        """检查端口是否在监听"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result == 0
        except:
            return False

    def wait_for_port(self, port: int, timeout: int = 30) -> bool:
        """等待端口启动"""
        if port <= 0:
            return True

        logger.info(f"等待端口 {port} 启动...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            if self.is_port_listening(port):
                logger.info(f"端口 {port} 已启动")
                return True
            time.sleep(1)
            elapsed = int(time.time() - start_time)
            logger.info(f"   等待中... ({elapsed}s/{timeout}s)")

        logger.error(f"端口 {port} 在 {timeout} 秒内未能启动")
        return False

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

            try:
                cmd = [path]
                if args:
                    cmd.extend(args.split())

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )

                self.processes.append(process)
                logger.info(f"{name} 启动成功 (PID: {process.pid})")

                if wait_port > 0:
                    if not self.wait_for_port(wait_port):
                        if required:
                            logger.error(f"{name} 端口 {wait_port} 启动失败")
                            return False
                        else:
                            logger.warning(f"{name} 端口 {wait_port} 未启动，但继续执行")

                time.sleep(2)

            except Exception as e:
                logger.error(f"启动 {name} 失败: {e}")
                if required:
                    return False

        logger.info("所有额外EXE程序启动完成")
        return True

    def stop_all(self):
        """停止所有启动的EXE程序"""
        logger.info("停止所有额外EXE程序...")
        for process in self.processes:
            try:
                if process.poll() is None:
                    logger.info(f"   停止 PID: {process.pid}")
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            except Exception as e:
                logger.error(f"停止进程失败: {e}")

        self.processes.clear()
        logger.info("所有额外EXE程序已停止")


class ClaudeLauncher:
    def __init__(self, config_path: str = "config.yaml"):
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

    def is_port_listening(self, port: int, host: str = '127.0.0.1') -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result == 0
        except:
            return False

    def ensure_wsl_running(self) -> bool:
        """确保 WSL 已启动（Claude VM 依赖 WSL），仅 Windows 生效"""
        if os.name != 'nt':
            return True

        logger.info("检查 WSL 状态...")
        try:
            result = subprocess.run(
                ['wsl', '--status'],
                capture_output=True, encoding='utf-8', errors='replace', timeout=10
            )
            if result.returncode == 0:
                status_line = result.stdout.split('\n')[0] if result.stdout else ''
                logger.info(f"WSL 状态正常" + (f" ({status_line.strip()})" if status_line else ""))
                return True
        except FileNotFoundError:
            logger.info("未找到 WSL（系统未安装），跳过")
            return True
        except subprocess.TimeoutExpired:
            logger.info("WSL 状态查询超时，尝试直接启动...")
        except Exception:
            pass

        logger.info("启动 WSL（Claude VM 的底层依赖）...")
        try:
            result = subprocess.run(
                ['wsl', '--cd', '~', '-e', 'true'],
                capture_output=True, encoding='utf-8', errors='replace', timeout=30
            )
            if result.returncode == 0:
                logger.info("WSL 已成功启动")
                return True
            else:
                logger.warning(f"WSL 启动异常 (返回码 {result.returncode})")
                return False
        except FileNotFoundError:
            logger.info("未找到 WSL 可执行文件，跳过")
            return True
        except subprocess.TimeoutExpired:
            logger.warning("WSL 启动超时 (30s)，继续执行后续流程")
            return True
        except Exception as e:
            logger.warning(f"WSL 启动异常: {e}")
            return True

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

            self.proxy_process = subprocess.Popen(
                [python_path, proxy_script],
                cwd=str(Path(proxy_script).parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            logger.info(f"   代理进程 PID: {self.proxy_process.pid}")

            proxy_port = self.config['ports']['proxy_port']
            logger.info(f"   等待代理在端口 {proxy_port} 启动...")

            for i in range(30):
                if self.is_port_listening(proxy_port):
                    logger.info(f"代理已在端口 {proxy_port} 启动")
                    return True
                time.sleep(1)
                logger.info(f"   等待中... ({i+1}/30)")

            logger.error(f"代理在 30 秒内未能启动")
            self.stop_proxy()
            return False

        except Exception as e:
            logger.error(f"启动代理失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def stop_proxy(self):
        if self.proxy_process and self.proxy_process.poll() is None:
            logger.info(f"停止代理进程 (PID: {self.proxy_process.pid})...")
            self.proxy_process.terminate()
            try:
                self.proxy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proxy_process.kill()
            logger.info("代理已停止")

    def launch_claude(self) -> bool:
        try:
            app_id = self.config['app'].get('app_id')
            claude_exe = self.config['paths'].get('claude_exe')

            if app_id:
                logger.info(f"通过 AppID 启动 Claude: {app_id}")
                subprocess.Popen(['explorer.exe', f'shell:AppsFolder\\{app_id}'])
                logger.info("Claude 已通过 AppID 启动")
                return True
            elif claude_exe and Path(claude_exe).exists():
                logger.info(f"通过 EXE 启动 Claude: {claude_exe}")
                subprocess.Popen([claude_exe])
                logger.info("Claude 已通过 EXE 启动")
                return True
            else:
                logger.error("未找到有效的 Claude 启动方式")
                return False
        except Exception as e:
            logger.error(f"启动 Claude 失败: {e}")
            return False

    def run(self):
        logger.info("=" * 50)
        logger.info("   Claude 自动启动器")
        logger.info("=" * 50)

        # 0. 确保 WSL 已启动（Claude VM 需要）
        self.ensure_wsl_running()

        # 1. 加载配置
        if not self.load_config():
            return False

        # 2. 设置代理环境变量
        if self.config.get('proxy_settings', {}).get('enabled', False):
            os.environ['HTTP_PROXY'] = self.config['proxy_settings']['http_proxy']
            os.environ['HTTPS_PROXY'] = self.config['proxy_settings']['https_proxy']
            logger.info(f"设置代理环境变量")

        # 3. 启动额外EXE程序
        extra_exes = self.config.get('extra_exes', [])
        if not self.extra_exe_manager.start_extra_exes(extra_exes):
            logger.error("额外EXE程序启动失败")
            self.extra_exe_manager.stop_all()
            return False

        # 4. 启动透明代理
        if not self.start_proxy():
            self.extra_exe_manager.stop_all()
            return False

        # 5. 启动 Claude
        if not self.launch_claude():
            self.stop_proxy()
            self.extra_exe_manager.stop_all()
            return False

        logger.info("=" * 50)
        logger.info("所有组件已成功启动！")
        logger.info("   按 Ctrl+C 停止所有程序")
        logger.info("=" * 50)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\n收到中断信号...")
        finally:
            self.stop_proxy()
            self.extra_exe_manager.stop_all()

        return True


def main():
    launcher = ClaudeLauncher("config.yaml")
    success = launcher.run()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
