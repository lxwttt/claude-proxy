#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地智能代理 - 模型请求转发 + 日志整合
"""
import json
import yaml
import re
import requests
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urljoin
from threading import Thread
import sys
import traceback
import os
import time
from pathlib import Path

# ========== 修复 stdout 编码 ==========
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ========== 全局变量 ==========
current_config = {}
script_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(script_dir, "model_config.yaml")
DEBUG_MODE = False

# ========== 日志系统（文件 + 控制台） ==========
_log_dir = Path(script_dir) / "logs"
_log_dir.mkdir(exist_ok=True)
_log_file = _log_dir / f"proxy_{time.strftime('%Y%m%d')}.log"

_ansi_re = re.compile(r'\033\[[0-9;]*m')

class _FileFormatter(logging.Formatter):
    """文件日志用 —— 自动去除 ANSI 颜色码和 emoji"""
    def format(self, record):
        msg = super().format(record)
        msg = _ansi_re.sub('', msg)
        msg = re.sub(r'[\U00010000-\U0010FFFF\U0000FE00-\U0000FE0F]', '', msg)
        return msg

# 控制台 handler（WARNING+，保留特殊字符）
_console_fmt = logging.Formatter('%(asctime)s - %(levelname)s - [Proxy] %(message)s')
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_console_fmt)
_console_handler.setLevel(logging.WARNING)

# 文件 handler（DEBUG+，每日追加，字符净化）
_file_fmt = _FileFormatter('%(asctime)s - %(levelname)s - %(message)s')
_file_handler = logging.FileHandler(_log_file, encoding='utf-8')
_file_handler.setFormatter(_file_fmt)
_file_handler.setLevel(logging.DEBUG)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger(__name__)
logger.info(f"代理日志文件: {_log_file}")

def load_config():
    """从 YAML 文件加载配置，并提取 debug_mode"""
    global current_config, DEBUG_MODE
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            all_configs = yaml.safe_load(f)

        current_env_name = all_configs.get("current_setting")
        env_config = all_configs.get("settings", {}).get(current_env_name)

        if not env_config:
            logger.error(f"未找到名为 '{current_env_name}' 的环境设置")
            return False

        current_config = env_config
        base_url = current_config.get('api_base_url', '').rstrip('/')
        current_config['api_base_url'] = base_url

        DEBUG_MODE = current_config.get("debug_mode", False)

        logger.info(f"配置 '{current_env_name}' 加载成功 (BaseURL: {base_url})")
        logger.info(f"Debug模式: {'ON' if DEBUG_MODE else 'OFF'}")
        return True
    except Exception as e:
        logger.error(f"读取配置文件错误: {e}")
        return False

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """每请求独立线程，支持多会话并发"""
    allow_reuse_address = True
    daemon_threads = True  # 主进程退出时线程自动终止


class SmartProxy(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 关闭 http.server 自带混乱日志

    def do_GET(self):
        """GET 探针"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def do_POST(self):
        """核心转发逻辑"""
        global DEBUG_MODE
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'

        try:
            data = json.loads(post_data.decode('utf-8'))
            original_model = data.get("model", "")

            # --- 模型映射：在名称中查找 tier 关键字 ---
            model_lower = original_model.lower()
            model_map = current_config.get("model_mapping", {})

            if 'opus' in model_lower:
                base_model = 'opus'
            elif 'sonnet' in model_lower:
                base_model = 'sonnet'
            elif 'haiku' in model_lower:
                base_model = 'haiku'
            else:
                base_model = 'haiku'

            target_model = model_map.get(base_model, model_map.get('haiku'))
            if not target_model:
                raise ValueError(f"未找到型号 '{base_model}' 的映射规则")

            data["model"] = target_model
            new_body = json.dumps(data).encode('utf-8')

            # ===== Debug 日志（DEBUG_MODE=true 时才记录到文件） =====
            if DEBUG_MODE:
                logger.debug(f"[>>] 收到请求: {self.path}")
                logger.debug(f"[>>] 模型转换: {original_model} -> {target_model}")
                logger.debug(f"[>>] 请求体:\n{json.dumps(json.loads(post_data.decode('utf-8')), indent=2, ensure_ascii=False)}")

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {current_config.get("api_key")}'
            }

            base_url = current_config.get("api_base_url", "")
            target_url = urljoin(base_url + '/', self.path.lstrip('/'))

            # 转发请求
            response = requests.post(target_url, data=new_body, headers=headers, timeout=120)

            # ===== Debug 日志 =====
            if DEBUG_MODE:
                logger.debug(f"[<<] 上游响应 ({response.status_code})")
                logger.debug(f"[<<] 转发至: {target_url}")
                logger.debug(f"[<<] 响应体:\n{response.text}")

            # 返回给客户端
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.content)

        except Exception as e:
            tb = traceback.format_exc()
            # error 级别：同时写入文件和控制台
            logger.error(f"[POST] 处理失败:\n{tb}")
            try:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass  # 客户端已断开，再次 send 会抛异常

def keyboard_listener(server):
    """监听键盘输入，支持热加载配置"""
    while True:
        cmd = input().strip().lower()
        if cmd == 'r':
            logger.info("正在重新加载配置...")
            if load_config():
                logger.warning("配置热更新成功")  # warning 才能在控制台显示
            else:
                logger.warning("配置热更新失败，继续使用当前配置运行")
        elif cmd == 'q':
            logger.info("接收到退出指令，正在关闭服务器...")
            server.shutdown()
            sys.exit(0)

if __name__ == '__main__':
    if not load_config():
        logger.critical("初始配置加载失败，程序退出")
        sys.exit(1)

    server_address = ('127.0.0.1', 8899)
    httpd = ThreadedHTTPServer(server_address, SmartProxy)

    listener_thread = Thread(target=keyboard_listener, args=(httpd,), daemon=True)
    listener_thread.start()

    logger.info(f"代理已启动，监听 http://127.0.0.1:8899")
    logger.info(f"按键指令 -> 'r' 重载配置，'q' 退出")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务器已终止")
        httpd.server_close()
