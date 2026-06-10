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
from urllib.parse import urlparse
from threading import Thread
import sys
import traceback
import os
import time
import subprocess

from common import (
    setup_logging,
    TIERS,
    DEFAULT_TIER,
    MAX_REQUEST_BODY_BYTES,
    validate_config_schema,
    is_port_listening,
)

# ========== 全局变量 ==========
current_config = {}
script_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(script_dir, "config", "model_config.yaml")
DEBUG_MODE = False
FULL_BODY_LOG = False  # debug 下是否记录完整响应体（关=仅记首少量字节，避免 SSE 日志爆炸）
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
    global current_config, DEBUG_MODE, FULL_BODY_LOG
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
            FULL_BODY_LOG = current_config.get("full_body_log", False)
            base_url = current_config.get('api_base_url', '')

        logger.info(f"配置 '{current_env_name}' 加载成功 (BaseURL: {base_url})")
        logger.info(f"Debug模式: {'ON' if DEBUG_MODE else 'OFF'}"
                    f"{'（全量响应体）' if (DEBUG_MODE and FULL_BODY_LOG) else ''}")
        return True
    except Exception as e:
        logger.error(f"读取配置文件错误: {e}")
        return False


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """每请求独立线程，支持多会话并发"""
    # 显式禁用端口复用：旧实例残留时新进程 bind 会显式报错，而非在 Windows 上静默共占端口
    # （配合 ensure_sole_instance：杀旧→复查→绑定，绑不上即 fail-loud）
    allow_reuse_address = False
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
        """转发请求到上游 API（流式：响应头一到就返回，body 由调用方边收边转发）"""
        new_body = json.dumps(data).encode('utf-8')
        # stream=True：headers 到达即返回，不等整段 body 生成完
        # timeout=(连接超时, 读超时)；读超时是"相邻两个数据块之间"的最大间隔，流式下足够宽松
        return requests.post(
            target_url, data=new_body, headers=headers,
            stream=True, timeout=(10, 300),
        )

    def _stream_response_to_client(self, response, debug, full):
        """流式把上游响应写回客户端；客户端或上游中途断开则安静收尾。

        关键：每收到一块就 flush——客户端一连上就持续收到字节，
        永不会在长达数十秒的静默里触发自身超时而 reset（根治 ECONNRESET）。
        debug 日志仅留存响应体前 cap 字节：full=True 留 1 MiB，否则只留 4 KiB，避免 SSE 刷爆日志。
        """
        cap = (1 << 20) if full else 4096
        preview = bytearray() if debug else None
        try:
            # requests 异常(ChunkedEncodingError/ConnectionError/ReadTimeout)均是 OSError 子类，
            # 与 wfile 写入的 BrokenPipe/ConnectionAbort 一并被 OSError 接住
            for chunk in response.iter_content(chunk_size=8192):
                self.wfile.write(chunk)
                self.wfile.flush()
                if preview is not None and len(preview) < cap:
                    preview.extend(chunk)
        except OSError as e:
            # 客户端断开（ECONNRESET/Abort）或上游中断：停止透传，不再回写
            logger.debug(f"流式透传中断（客户端或上游断开）: {e}")
        finally:
            response.close()
        if preview is not None:
            logger.debug(f"[<<] 响应体(前{cap // 1024}KiB):\n{preview.decode('utf-8', errors='replace')}")

    def _drain_request_body(self, content_length):
        """排空并丢弃客户端剩余请求体，避免未读体导致 TCP RST（客户端表现为 ECONNRESET）。"""
        remaining = content_length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))  # 每次最多 1 MiB
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception as e:
            logger.debug(f"排空请求体时出错（客户端可能已断开）: {e}")

    def do_POST(self):
        """核心转发逻辑"""
        # 线程安全读取配置
        with _config_lock:
            _debug = DEBUG_MODE
            _full_log = FULL_BODY_LOG
            _config = dict(current_config)  # shallow copy for this request

        # 请求体大小限制：先安全解析 Content-Length，畸形/负值回干净 400，
        # 避免 int() 抛 ValueError 冲出处理线程导致连接被重置（又一种 ECONNRESET）
        try:
            content_length = int(self.headers.get('Content-Length') or 0)
            if content_length < 0:
                raise ValueError('negative Content-Length')
        except (TypeError, ValueError):
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid Content-Length"}).encode())
            return
        if content_length > MAX_REQUEST_BODY_BYTES:
            # 先排空客户端仍在上传的请求体，否则"响应先于请求体读完"会触发 TCP RST，
            # 客户端收到的将是 ECONNRESET 而非 413。同时记录真实体积，不再静默。
            self._drain_request_body(content_length)
            logger.warning(
                f"[>>] 请求体过大被拒 413: {content_length} 字节 "
                f"({content_length / 1024 / 1024:.2f} MiB) > 上限 "
                f"{MAX_REQUEST_BODY_BYTES / 1024 / 1024:.0f} MiB"
            )
            self.send_response(413)  # Payload Too Large
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Request body too large"}).encode())
            return

        post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'

        response_started = False  # 是否已向客户端下发响应头（决定出错时能否回 500）
        try:
            data = json.loads(post_data.decode('utf-8'))
            original_model = data.get("model", "")

            # --- 模型映射 ---
            matched_tier, target_model = self._resolve_model(original_model, _config)
            data["model"] = target_model

            # --- effort 映射 ---
            self._apply_effort_mapping(data, matched_tier, _config, _debug)

            # 构建目标 URL：拒绝带 scheme/netloc 的 path（防 urljoin 逃逸到任意主机 → 密钥外泄/SSRF）
            parsed_path = urlparse(self.path)
            if parsed_path.scheme or parsed_path.netloc:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid request path"}).encode())
                return
            base_url = _config.get("api_base_url", "")
            # 显式拼接（不用 urljoin，杜绝 scheme/scheme-relative 逃逸），保留 query
            target_url = base_url.rstrip('/') + '/' + self.path.lstrip('/')
            # 复校：目标 host 必须等于配置 base_url 的 host，否则拒绝
            if urlparse(target_url).netloc != urlparse(base_url).netloc:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Upstream host mismatch"}).encode())
                return

            # ===== Debug 日志（DEBUG_MODE=true 时才记录到文件） =====
            if _debug:
                logger.debug(f"[>>] 收到请求: {self.path}")
                logger.debug(f"[>>] 模型转换: {original_model} -> {target_model}")
                if len(post_data) > (1 << 20):
                    logger.debug(f"[>>] 请求体: <{len(post_data)} 字节，过大已省略>")
                else:
                    logger.debug(f"[>>] 请求体:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {_config.get("api_key")}',
                # 禁用上游压缩：避免运行环境缺 brotli 时流中途解码失败截断 SSE，也省去解码缓冲
                'Accept-Encoding': 'identity',
            }

            # 转发请求（流式）
            response = self._forward_request(data, target_url, headers)

            # ===== Debug 日志 =====
            if _debug:
                logger.debug(f"[<<] 上游响应 ({response.status_code})")
                logger.debug(f"[<<] 转发至: {target_url}")

            # 流式透传给客户端：先下发响应头（透传上游真实 Content-Type，
            # SSE 为 text/event-stream，不再硬编码 application/json），再 body 边收边 flush
            self.send_response(response.status_code)
            response_started = True  # 头已下发：此后任何异常都不能再回写 500，否则污染流
            self.send_header('Content-Type',
                             response.headers.get('Content-Type', 'application/json'))
            self.end_headers()
            self._stream_response_to_client(response, _debug, _full_log)

        except Exception as e:
            tb = traceback.format_exc()
            # error 级别：同时写入文件和控制台
            logger.error(f"[POST] 处理失败:\n{tb}")
            if response_started:
                # 响应头/流已开始，再发 500 会向已开始的 SSE 注入脏状态行，只记录不回写
                logger.debug("响应已开始，跳过 500 回写")
            else:
                try:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    # 只回固定文案，详情已写日志，避免向客户端泄漏内部 target_url/堆栈
                    self.wfile.write(json.dumps({"error": "internal proxy error"}).encode())
                except Exception as send_err:
                    logger.debug(f"发送错误响应失败（客户端可能已断开）: {send_err}")


def keyboard_listener(server):
    """监听键盘输入，支持热加载配置（仅在终端独立运行时生效）"""
    if not sys.stdin.isatty():
        return  # 非终端模式（如被 launch.py 子进程启动），跳过键盘监听

    print()  # 换行，避免干扰启动日志
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
                logger.warning("配置热更新成功")
            else:
                logger.warning("配置热更新失败，继续使用当前配置运行")
        elif cmd == 'q':
            logger.info("接收到退出指令，正在关闭服务器...")
            server.shutdown()
            sys.exit(0)


def ensure_sole_instance(host, port):
    """启动前独占端口：杀掉确为本代理旧实例的占用者；清理后端口仍被占则中止启动。

    安全校验：只杀「python 进程且命令行含 local_proxy.py」者——迁移到别的环境时
    绝不误伤无关进程。清理后复查端口，仍被占（杀失败/外来进程/释放慢）即 sys.exit，
    拒绝与残留进程共享端口（杜绝双实例跑旧代码）。此刻本进程尚未绑定，不会误杀自己。
    """
    if not is_port_listening(port, host):
        return
    marker = os.path.basename(__file__)
    # 逐个占用 PID 校验：python 进程 + 命令行含本脚本名才 Stop-Process，否则标记 FOREIGN 不动手
    ps = (
        f"foreach ($procId in (Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)) {{ "
        f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$procId\" -ErrorAction SilentlyContinue; "
        f"if ($p.Name -like 'python*' -and $p.CommandLine -like '*{marker}*') "
        f"{{ Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue; \"KILLED $procId\" }} "
        f"else {{ \"FOREIGN $procId $($p.Name)\" }} }}"
    )
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except OSError as e:
        logger.error(f"无法调用 powershell 清理旧实例: {e}")
        out = ""
    killed = [ln.split()[1] for ln in out.splitlines() if ln.startswith('KILLED')]
    foreign = [ln[len('FOREIGN '):].strip() for ln in out.splitlines() if ln.startswith('FOREIGN')]
    if killed:
        logger.warning(f"已请求终止占用端口 {port} 的旧代理 PID: {', '.join(killed)}")
    if foreign:
        logger.error(f"端口 {port} 被非本代理进程占用: {'; '.join(foreign)}")
    # 复查：清理后端口必须真正释放，否则拒绝以共享端口方式启动（不信 KILLED 字样，只信端口状态）
    for _ in range(20):  # 最多等 ~2s 让端口释放
        if not is_port_listening(port, host):
            return
        time.sleep(0.1)
    logger.critical(f"端口 {port} 清理后仍被占用，拒绝共享端口启动，请手动处理后重试")
    sys.exit(1)


if __name__ == '__main__':
    if not load_config():
        logger.critical("初始配置加载失败，程序退出")
        sys.exit(1)

    server_address = ('127.0.0.1', 8899)
    ensure_sole_instance(*server_address)  # 抢占式清理旧实例，杜绝僵尸代理
    try:
        httpd = ThreadedHTTPServer(server_address, SmartProxy)
    except OSError as e:
        # allow_reuse_address=False 下，端口仍被占会在此显式失败（而非静默共占）
        logger.critical(f"绑定 {server_address[0]}:{server_address[1]} 失败（端口可能仍被占用）: {e}")
        sys.exit(1)

    listener_thread = Thread(target=keyboard_listener, args=(httpd,), daemon=True)
    listener_thread.start()

    logger.info(f"代理已启动，监听 http://127.0.0.1:8899")
    logger.info(f"按键指令 -> 'r' 重载配置 | 'q' 退出")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务器已终止")
        httpd.server_close()
