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
    DEFAULT_HOST,
    DEFAULT_PROXY_PORT,
    MAX_REQUEST_BODY_BYTES,
    validate_config_schema,
    is_port_listening,
)

# ========== 全局变量 ==========
current_config = {}
current_setting_name = ""  # 当前生效的 profile 名（model_config.yaml 的 current_setting），供 /reload 回显
script_dir = os.path.dirname(os.path.abspath(__file__))
# config/ 在项目根（src 上一级）：模块下沉到 src/ 后须回锚一层，否则会去 src/config 找配置
config_file_path = os.path.join(os.path.dirname(script_dir), "config", "model_config.yaml")
DEBUG_MODE = False
FULL_BODY_LOG = False  # debug 下是否记录完整响应体（关=仅记首少量字节，避免 SSE 日志爆炸）
_config_lock = threading.Lock()
_request_semaphore = threading.Semaphore(32)  # 有界并发：同时处理的 POST 上限，超限快速 503

# ========== OAuth（订阅凭证）认证模式常量 ==========
# 订阅 OAuth token 仅授权给 Claude Code：官方按 system 首块的这段身份文本放行，缺失即被拒。
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_OAUTH_BASE_URL = "https://api.anthropic.com"  # oauth 模式未配 api_base_url 时的默认上游
# Claude Code 在 Windows 下把凭证写到此处，并在后台轮换 accessToken
DEFAULT_CREDENTIALS_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")

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
    global current_config, current_setting_name, DEBUG_MODE, FULL_BODY_LOG
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            all_configs = yaml.safe_load(f)

        current_env_name = all_configs.get("current_setting")
        env_config = all_configs.get("settings", {}).get(current_env_name)

        if not env_config:
            logger.error(f"未找到名为 '{current_env_name}' 的环境设置")
            return False

        # 配置模式验证：oauth 模式 base_url/凭证/模型均有默认或透传，无强制字段；
        # api_key 模式仍需 base_url + key + 映射三件套
        auth_mode = env_config.get("auth_mode", "api_key")
        if auth_mode not in ("api_key", "oauth"):
            logger.error(f"配置 '{current_env_name}' auth_mode 非法: {auth_mode!r}（应为 api_key / oauth）")
            return False
        required = () if auth_mode == "oauth" else ('api_base_url', 'api_key', 'model_mapping')
        if required and not validate_config_schema(env_config, required, context=f'env:{current_env_name}'):
            logger.error(f"配置 '{current_env_name}' 缺少必要字段，加载失败")
            return False

        with _config_lock:
            current_config = env_config
            current_setting_name = current_env_name
            DEBUG_MODE = current_config.get("debug_mode", False)
            FULL_BODY_LOG = current_config.get("full_body_log", False)
            base_url = current_config.get('api_base_url') or (DEFAULT_OAUTH_BASE_URL if auth_mode == "oauth" else '')

        logger.info(f"配置 '{current_env_name}' 加载成功 (模式: {auth_mode}, BaseURL: {base_url})")
        logger.info(f"Debug模式: {'ON' if DEBUG_MODE else 'OFF'}"
                    f"{'（全量响应体）' if (DEBUG_MODE and FULL_BODY_LOG) else ''}")
        return True
    except Exception as e:
        logger.error(f"读取配置文件错误: {e}")
        return False


class OAuthCredentialError(Exception):
    """订阅凭证缺失/损坏/过期：可预期的运维状况，给出清晰刷新提示，不当内部错误(500)处理。"""


def load_oauth_token(credentials_path):
    """读取 Claude 订阅 OAuth accessToken 并校验未过期。

    凭证由 Claude Code 后台轮换写盘、token 短时有效——故每请求按需读盘取最新值，
    不缓存、不自行续签；过期/缺失则抛 OAuthCredentialError 提示打开 Claude Code 刷新。
    """
    try:
        with open(credentials_path, "r", encoding="utf-8") as f:
            oauth = (json.load(f) or {}).get("claudeAiOauth") or {}
    except FileNotFoundError:
        raise OAuthCredentialError(
            f"未找到凭证文件 {credentials_path} — 先登录 Claude Code 生成订阅凭证")
    except (OSError, json.JSONDecodeError) as e:
        raise OAuthCredentialError(f"读取凭证文件失败 {credentials_path}: {e}")
    token = oauth.get("accessToken")
    if not token:
        raise OAuthCredentialError(f"凭证文件缺少 claudeAiOauth.accessToken: {credentials_path}")
    # expiresAt 为 unix 毫秒；留 60s 余量，避免临界 token 在流式途中失效
    expires_at = oauth.get("expiresAt")
    if expires_at is not None and expires_at <= (time.time() + 60) * 1000:
        raise OAuthCredentialError("OAuth token 已过期 — 打开 Claude Code 一次以刷新 .credentials.json")
    return token


def _extract_tier(model_name):
    """从模型名提取 tier 关键字（无匹配则降级默认并告警）。api_key/oauth 两模式共用。"""
    matched_tier = next((tier for tier in TIERS if tier in model_name.lower()), None)
    if matched_tier is None:
        logger.warning(f"模型名 '{model_name}' 未识别 tier 关键字，降级为默认 {DEFAULT_TIER}")
        matched_tier = DEFAULT_TIER
    return matched_tier


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """每请求独立线程，支持多会话并发"""
    # 显式禁用端口复用：旧实例残留时新进程 bind 会显式报错，而非在 Windows 上静默共占端口
    # （配合 ensure_sole_instance：杀旧→复查→绑定，绑不上即 fail-loud）
    allow_reuse_address = False
    daemon_threads = True  # 主进程退出时线程自动终止


class SmartProxy(BaseHTTPRequestHandler):
    timeout = 30  # 连接读超时：卡死/慢客户端被主动断开、释放线程（防 slowloris/线程泄漏）
    disable_nagle_algorithm = True  # 开 TCP_NODELAY，降低流式小包延迟
    DRAIN_CAP = 64 * 1024 * 1024  # 413 排空请求体上限（2× 体积上限，合法超限请求够读完）

    def log_message(self, format, *args):
        pass  # 关闭 http.server 自带混乱日志

    def _send_json(self, status, payload):
        """统一回写本代理自产的小 JSON 响应（探针/热加载/各类错误）。
        客户端可能已断开 → 整体写入失败只记 debug 不外抛（原先仅 500 路径有此保护，
        其余各处复制粘贴时漏了，集中到一处后行为统一）。
        注意：上游流式响应不走这里，仍由 _stream_response_to_client 直传。"""
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())
        except OSError as e:
            logger.debug(f"回写 JSON 响应失败（客户端可能已断开）: {e}")

    def do_GET(self):
        """GET 探针 / 配置热加载"""
        if self.path == '/reload':
            ok = load_config()
            with _config_lock:
                setting = current_setting_name
            self._send_json(200 if ok else 500,
                            {"reload": "ok" if ok else "failed", "setting": setting})
            return
        self._send_json(200, {"status": "ok"})

    def _resolve_model(self, model_name, config):
        """从模型名提取 tier，按映射返回 (tier, target_model)（api_key 模式用）"""
        matched_tier = _extract_tier(model_name)
        model_map = config.get("model_mapping", {})
        target_model = model_map.get(matched_tier, model_map.get(DEFAULT_TIER))
        if not target_model:
            raise ValueError(f"未找到型号 '{matched_tier}' 的映射规则")
        return matched_tier, target_model

    def _inject_claude_code_system(self, data):
        """oauth 模式：把 Claude Code 身份块置于 system 数组首位（订阅 token 放行的前提）。

        原 system 原样保留在身份块之后：缺失→新建；字符串→转 text 块续上；数组→前插。
        已以身份块开头则不重复注入。"""
        cc_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM}
        system = data.get("system")
        if system is None:
            data["system"] = [cc_block]
        elif isinstance(system, str):
            data["system"] = [cc_block, {"type": "text", "text": system}]
        elif isinstance(system, list):
            first = system[0] if system else None
            if isinstance(first, dict) and first.get("text") == CLAUDE_CODE_SYSTEM:
                return  # 已注入，避免重复
            data["system"] = [cc_block] + system
        else:
            data["system"] = [cc_block]  # 非预期类型兜底：至少保证可被官方接受

    def _build_headers(self, config):
        """按 auth_mode 构造转发头。两模式都禁用上游压缩（避免缺 brotli 时流中途解码截断 SSE）。"""
        if config.get("auth_mode", "api_key") == "oauth":
            creds_path = config.get("credentials_path") or DEFAULT_CREDENTIALS_PATH
            token = load_oauth_token(creds_path)  # 过期/缺失抛 OAuthCredentialError，上层转 401
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "anthropic-version": ANTHROPIC_VERSION,
                "anthropic-beta": OAUTH_BETA,
                "Accept-Encoding": "identity",
            }
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.get('api_key')}",
            "Accept-Encoding": "identity",
        }

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
        """排空客户端剩余请求体（避免未读体触发 RST→客户端见 ECONNRESET 而非 413）。
        至多读 DRAIN_CAP 字节：合法超限请求(略超 32MiB)能读完得干净 413；恶意声明超大
        Content-Length 时不被迫陪读到底（配合 timeout=30 兜底慢上传）。"""
        remaining = min(content_length, self.DRAIN_CAP)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))  # 每次最多 1 MiB
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception as e:
            logger.debug(f"排空请求体时出错（客户端可能已断开）: {e}")

    def do_POST(self):
        # 有界并发：超过上限快速回 503，避免线程/内存被打爆
        if not _request_semaphore.acquire(blocking=False):
            self._send_json(503, {"error": "proxy busy"})
            return
        try:
            self._handle_post()
        finally:
            _request_semaphore.release()

    def _handle_post(self):
        """核心转发逻辑"""
        # 线程安全读取配置
        with _config_lock:
            _debug = DEBUG_MODE
            _full_log = FULL_BODY_LOG
            # load_config 整体重绑 current_config（绝不原地改它），取引用即一致快照；
            # 本函数也只读 _config 不写，故无需逐请求 dict() 拷贝
            _config = current_config

        # 不支持 chunked 请求体：BaseHTTPRequestHandler 不解块，若当 0 字节读会静默丢正文，
        # 显式回 400 而非静默转发空体
        if 'chunked' in self.headers.get('Transfer-Encoding', '').lower():
            self._send_json(400, {"error": "chunked request body not supported"})
            return

        # 请求体大小限制：先安全解析 Content-Length，畸形/负值回干净 400，
        # 避免 int() 抛 ValueError 冲出处理线程导致连接被重置（又一种 ECONNRESET）
        try:
            content_length = int(self.headers.get('Content-Length') or 0)
            if content_length < 0:
                raise ValueError('negative Content-Length')
        except (TypeError, ValueError):
            self._send_json(400, {"error": "Invalid Content-Length"})
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
            self._send_json(413, {"error": "Request body too large"})  # Payload Too Large
            return

        post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'

        response_started = False  # 是否已向客户端下发响应头（决定出错时能否回 500）
        try:
            data = json.loads(post_data.decode('utf-8'))
            original_model = data.get("model", "")
            auth_mode = _config.get("auth_mode", "api_key")

            # --- 模型处理：oauth 直打官方、透传原模型名 + 注入 Claude Code 身份块；
            #     api_key 模式按 model_mapping 改写为第三方型号 ---
            if auth_mode == "oauth":
                matched_tier = _extract_tier(original_model)
                self._inject_claude_code_system(data)
            else:
                matched_tier, data["model"] = self._resolve_model(original_model, _config)

            # --- effort 映射（两模式一致：仅当原请求含 output_config 时条件覆写）---
            self._apply_effort_mapping(data, matched_tier, _config, _debug)

            # 构建目标 URL：拒绝带 scheme/netloc 的 path（防 urljoin 逃逸到任意主机 → 密钥外泄/SSRF）
            parsed_path = urlparse(self.path)
            if parsed_path.scheme or parsed_path.netloc:
                self._send_json(400, {"error": "Invalid request path"})
                return
            # oauth 模式默认官方端点；api_key 模式必须由配置给出
            base_url = _config.get("api_base_url") or (DEFAULT_OAUTH_BASE_URL if auth_mode == "oauth" else "")
            # 显式拼接（不用 urljoin，杜绝 scheme/scheme-relative 逃逸），保留 query
            target_url = base_url.rstrip('/') + '/' + self.path.lstrip('/')
            # 复校：目标 host 必须等于配置 base_url 的 host，否则拒绝
            if urlparse(target_url).netloc != urlparse(base_url).netloc:
                self._send_json(400, {"error": "Upstream host mismatch"})
                return

            # ===== Debug 日志（DEBUG_MODE=true 时才记录到文件） =====
            if _debug:
                logger.debug(f"[>>] 收到请求: {self.path}")
                logger.debug(f"[>>] 模型: {original_model} -> {data.get('model', original_model)} ({auth_mode})")
                if len(post_data) > (1 << 20):
                    logger.debug(f"[>>] 请求体: <{len(post_data)} 字节，过大已省略>")
                else:
                    logger.debug(f"[>>] 请求体:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

            del post_data  # 提前释放原始请求体字节，减少与 data/new_body 的内存重叠

            # 按 auth_mode 构造转发头（oauth 读订阅凭证；过期/缺失抛 OAuthCredentialError → 401）
            headers = self._build_headers(_config)

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

        except OAuthCredentialError as e:
            # 订阅凭证缺失/过期：可预期运维状况，清晰提示刷新，不打 traceback、不当 500
            logger.warning(f"[oauth] {e}")
            if not response_started:
                self._send_json(401, {"error": str(e)})
        except Exception as e:
            tb = traceback.format_exc()
            # error 级别：同时写入文件和控制台
            logger.error(f"[POST] 处理失败:\n{tb}")
            if response_started:
                # 响应头/流已开始，再发 500 会向已开始的 SSE 注入脏状态行，只记录不回写
                logger.debug("响应已开始，跳过 500 回写")
            else:
                # 只回固定文案，详情已写日志，避免向客户端泄漏内部 target_url/堆栈
                # （_send_json 已内置断开保护，无需再裹 try）
                self._send_json(500, {"error": "internal proxy error"})


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
            server.shutdown()  # 让主线程 serve_forever 返回而退出；子线程 sys.exit 无法终止进程
            return


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

    # 端口单一事实来源：由 launch.py 以命令行参数传入（config.yaml ports.proxy_port）；独立运行回退默认
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PROXY_PORT
    except (ValueError, IndexError):
        port = DEFAULT_PROXY_PORT
    server_address = (DEFAULT_HOST, port)
    ensure_sole_instance(*server_address)  # 抢占式清理旧实例，杜绝僵尸代理
    try:
        httpd = ThreadedHTTPServer(server_address, SmartProxy)
    except OSError as e:
        # allow_reuse_address=False 下，端口仍被占会在此显式失败（而非静默共占）
        logger.critical(f"绑定 {server_address[0]}:{server_address[1]} 失败（端口可能仍被占用）: {e}")
        sys.exit(1)

    listener_thread = Thread(target=keyboard_listener, args=(httpd,), daemon=True)
    listener_thread.start()

    logger.info(f"代理已启动，监听 http://{DEFAULT_HOST}:{port}")
    logger.info(f"按键指令 -> 'r' 重载配置 | 'q' 退出")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务器已终止")
        httpd.server_close()
