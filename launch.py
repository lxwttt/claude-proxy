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

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
                logger.info(f"✅ 端口 {port} 已启动")
                return True
            time.sleep(1)
            elapsed = int(time.time() - start_time)
            logger.info(f"   等待中... ({elapsed}s/{timeout}s)")
        
        logger.error(f"❌ 端口 {port} 在 {timeout} 秒内未能启动")
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
            
            # 检查文件是否存在
            if not Path(path).exists():
                logger.error(f"❌ 文件不存在: {path}")
                if required:
                    return False
                continue
            
            try:
                # 构建完整命令
                cmd = [path]
                if args:
                    cmd.extend(args.split())
                
                # 启动进程（后台运行）
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                self.processes.append(process)
                logger.info(f"✅ {name} 启动成功 (PID: {process.pid})")
                
                # 如果需要等待端口
                if wait_port > 0:
                    if not self.wait_for_port(wait_port):
                        if required:
                            logger.error(f"❌ {name} 端口 {wait_port} 启动失败")
                            return False
                        else:
                            logger.warning(f"⚠️ {name} 端口 {wait_port} 未启动，但继续执行")
                
                # 给进程一点启动时间
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"❌ 启动 {name} 失败: {e}")
                if required:
                    return False
        
        logger.info("✅ 所有额外EXE程序启动完成")
        return True
    
    def stop_all(self):
        """停止所有启动的EXE程序"""
        logger.info("停止所有额外EXE程序...")
        for process in self.processes:
            try:
                if process.poll() is None:  # 进程仍在运行
                    logger.info(f"   停止 PID: {process.pid}")
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            except Exception as e:
                logger.error(f"停止进程失败: {e}")
        
        self.processes.clear()
        logger.info("✅ 所有额外EXE程序已停止")

class ClaudeLauncher:
    def __init__(self, config_path: str = "config.yaml"):
        """初始化启动器"""
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.proxy_process: Optional[subprocess.Popen] = None
        self.extra_exe_manager = ExtraExeManager()
        
    def load_config(self) -> bool:
        """加载配置文件"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                logger.error(f"配置文件不存在: {self.config_path}")
                return False
                
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            
            logger.info("✅ 配置文件加载成功")
            return True
        except Exception as e:
            logger.error(f"❌ 加载配置文件失败: {e}")
            return False
    
    def is_port_listening(self, port: int, host: str = '127.0.0.1') -> bool:
        """检查端口是否在监听"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result == 0
        except:
            return False
    
    def start_proxy(self) -> bool:
        """启动透明代理"""
        try:
            python_path = self.config['paths']['python']
            proxy_script = self.config['paths']['proxy_script']
            
            # 验证文件存在
            if not Path(python_path).exists():
                logger.error(f"Python 不存在: {python_path}")
                return False
                
            if not Path(proxy_script).exists():
                logger.error(f"代理脚本不存在: {proxy_script}")
                return False
            
            logger.info(f"启动透明代理...")
            logger.info(f"   Python: {python_path}")
            logger.info(f"   脚本: {proxy_script}")
            
            # 启动代理进程
            self.proxy_process = subprocess.Popen(
                [python_path, proxy_script],
                cwd=str(Path(proxy_script).parent),
                stdout=None,
                stderr=None,
                text=True
            )
            
            logger.info(f"   代理进程 PID: {self.proxy_process.pid}")
            
            # 等待代理启动
            proxy_port = self.config['ports']['proxy_port']
            logger.info(f"   等待代理在端口 {proxy_port} 启动...")
            
            for i in range(30):  # 等待最多30秒
                if self.is_port_listening(proxy_port):
                    logger.info(f"✅ 代理已在端口 {proxy_port} 启动")
                    return True
                time.sleep(1)
                logger.info(f"   等待中... ({i+1}/30)")
            
            logger.error(f"❌ 代理在 30 秒内未能启动")
            self.stop_proxy()
            return False
            
        except Exception as e:
            logger.error(f"❌ 启动代理失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def stop_proxy(self):
        """停止代理进程"""
        if self.proxy_process and self.proxy_process.poll() is None:
            logger.info(f"停止代理进程 (PID: {self.proxy_process.pid})...")
            self.proxy_process.terminate()
            try:
                self.proxy_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proxy_process.kill()
            logger.info("✅ 代理已停止")
    
    def launch_claude(self) -> bool:
        """启动Claude"""
        try:
            app_id = self.config['app'].get('app_id')
            claude_exe = self.config['paths'].get('claude_exe')
            
            if app_id:
                logger.info(f"通过 AppID 启动 Claude: {app_id}")
                subprocess.Popen(['explorer.exe', f'shell:AppsFolder\\{app_id}'])
                logger.info("✅ Claude 已通过 AppID 启动")
                return True
            elif claude_exe and Path(claude_exe).exists():
                logger.info(f"通过 EXE 启动 Claude: {claude_exe}")
                subprocess.Popen([claude_exe])
                logger.info("✅ Claude 已通过 EXE 启动")
                return True
            else:
                logger.error("❌ 未找到有效的 Claude 启动方式")
                return False
        except Exception as e:
            logger.error(f"❌ 启动 Claude 失败: {e}")
            return False
    
    def run(self):
        """主运行函数"""
        logger.info("=" * 50)
        logger.info("   Claude 自动启动器")
        logger.info("=" * 50)
        
        # 1. 加载配置
        if not self.load_config():
            return False
        
        # 2. 设置代理环境变量
        if self.config.get('proxy_settings', {}).get('enabled', False):
            os.environ['HTTP_PROXY'] = self.config['proxy_settings']['http_proxy']
            os.environ['HTTPS_PROXY'] = self.config['proxy_settings']['https_proxy']
            logger.info(f"✅ 设置代理环境变量")
        
        # 3. 启动额外EXE程序（如Clash等）
        extra_exes = self.config.get('extra_exes', [])
        if not self.extra_exe_manager.start_extra_exes(extra_exes):
            logger.error("❌ 额外EXE程序启动失败")
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
        logger.info("✅ 所有组件已成功启动！")
        logger.info("   按 Ctrl+C 停止所有程序")
        logger.info("=" * 50)
        
        # 保持运行，直到用户中断
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
    """主入口点"""
    launcher = ClaudeLauncher("config.yaml")
    success = launcher.run()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()