#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地智能代理 - 模型请求转发 + 日志整合
"""
import json
import yaml
import threading
import requests
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urljoin
from threading import Thread
import sys
import traceback
import os

from common import (
    setup_logging,
    TIERS,
    DEFAULT_TIER,
    MAX_REQUEST_BODY_BYTES,
    validate_config_schema,
)

# ========== 全局变量 ==========
current_config = {}
script_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(script_dir, "model_config.yaml")
DEBUG_MODE = False
_config_lock = threading.Lock()

# ========== 日志系统（文件 + 控制台） ==========
logger = setup_logging(
    __name__,
    log_prefix='proxy',
    console_level=logging.WARNING,
    file_level=logging.DEBUG,
    console_fmt='%(asctime)s - %(levelname)s - [Proxy] %(message)s',
)


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

        # 配置模式验证
        if not validate_config_schema(
            env_config,
            ('api_base_url', 'api_key', 'model_mapping'),
            context=f'env:{current_env_name}',
        ):
            logger.error(f"配置 '{current_env_name}' 缺少必要字段，加载失败")
            return False

        with _config_lock:
            current_config = env_config
            DEBUG_MODE = current_config.get("debug_mode", False)
            base_url = current_config.get('api_base_url', '')

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
        """GET 探针 / 配置热加载"""
        if self.path == '/reload':
            ok = load_config()
            self.send_response(200 if ok else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(
                {"reload": "ok" if ok else "failed"}, ensure_ascii=False
            ).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def _resolve_model(self, model_name, config):
        """从模型名提取 tier 关键字，返回 (tier, target_model)"""
        model_lower = model_name.lower()
        model_map = config.get("model_mapping", {})

        matched_tier = DEFAULT_TIER
        for tier in TIERS:
            if tier in model_lower:
                matched_tier = tier
                break

        target_model = model_map.get(matched_tier, model_map.get(DEFAULT_TIER))
        if not target_model:
            raise ValueError(f"未找到型号 '{matched_tier}' 的映射规则")
        return matched_tier, target_model

    def _apply_effort_mapping(self, data, tier, config, debug):
        """effort 映射：仅在原请求已包含 output_config 时覆盖"""
        effort_map = config.get("effort_mapping", {})
        if tier in effort_map and "output_config" in data:
            target_effort = effort_map[tier]
            if target_effort:
                old_effort = data["output_config"].get("effort", "未设置")
                data["output_config"]["effort"] = target_effort
                if debug:
                    logger.debug(f"[>>] effort 转换: {old_effort} -> {target_effort}")

    def _forward_request(self, data, target_url, headers):
        """转发请求到上游 API"""
        new_body = json.dumps(data).encode('utf-8')
        return requests.post(target_url, data=new_body, headers=headers, timeout=120)

    def do_POST(self):
        """核心转发逻辑"""
        # 线程安全读取配置
        with _config_lock:
            _debug = DEBUG_MODE
            _config = dict(current_config)  # shallow copy for this request

        # 请求体大小限制
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > MAX_REQUEST_BODY_BYTES:
            self.send_response(413)  # Payload Too Large
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Request body too large"}).encode())
            return

        post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'

        try:
            data = json.loads(post_data.decode('utf-8'))
            original_model = data.get("model", "")

            # --- 模型映射 ---
            matched_tier, target_model = self._resolve_model(original_model, _config)
            data["model"] = target_model

            # --- effort 映射 ---
            self._apply_effort_mapping(data, matched_tier, _config, _debug)

            # 构建目标 URL（确保 base_url 以 '/' 结尾，避免 urljoin 截断）
            base_url = _config.get("api_base_url", "")
            if not base_url.endswith('/'):
                base_url += '/'
            target_url = urljoin(base_url, self.path.lstrip('/'))

            # ===== Debug 日志（DEBUG_MODE=true 时才记录到文件） =====
            if _debug:
                logger.debug(f"[>>] 收到请求: {self.path}")
                logger.debug(f"[>>] 模型转换: {original_model} -> {target_model}")
                logger.debug(f"[>>] 请求体:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {_config.get("api_key")}'
            }

            # 转发请求
            response = self._forward_request(data, target_url, headers)

            # ===== Debug 日志 =====
            if _debug:
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
            except Exception as send_err:
                logger.debug(f"发送错误响应失败（客户端可能已断开）: {send_err}")


def keyboard_listener(server):
    """监听键盘输入，支持热加载配置"""
    while True:
        try:
            cmd = input().strip().lower()
        except EOFError:
            logger.info("stdin 已关闭，键盘监听退出")
            break
        except Exception:
            break
        if cmd == 'r':
            logger.info("正在重新加载配置...")
            if load_config():
                logger.warning("配置热更新成功")  # warning 才能在控制台显示
            else:
                logger.warning("配置热更新失败，继续使用当前配置运行")


if __name__ == '__main__':
    if not load_config():
        logger.critical("初始配置加载失败，程序退出")
        sys.exit(1)

    server_address = ('127.0.0.1', 8899)
    httpd = ThreadedHTTPServer(server_address, SmartProxy)

    listener_thread = Thread(target=keyboard_listener, args=(httpd,), daemon=True)
    listener_thread.start()

    logger.info(f"代理已启动，监听 http://127.0.0.1:8899")
    logger.info(f"按键指令 -> 'r' 重载配置")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务器已终止")
        httpd.server_close()
