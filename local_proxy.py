import json
import yaml
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin
from threading import Thread
import sys
import traceback
import os

# ========== 全局变量 ==========
current_config = {}
# 自动获取与脚本同路径下的 model_config.yaml
script_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(script_dir, "model_config.yaml")
DEBUG_MODE = False  # 默认关闭，由 config.yaml 动态接管

def load_config():
    """从 YAML 文件加载配置，并提取 debug_mode"""
    global current_config, DEBUG_MODE
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            all_configs = yaml.safe_load(f)
        
        current_env_name = all_configs.get("current_setting")
        env_config = all_configs.get("settings", {}).get(current_env_name)
        
        if not env_config:
            print(f"[-] 加载失败: 未找到名为 '{current_env_name}' 的环境设置")
            return False
            
        current_config = env_config
        base_url = current_config.get('api_base_url', '').rstrip('/')
        current_config['api_base_url'] = base_url
        
        # 加载 debug 模式状态
        DEBUG_MODE = current_config.get("debug_mode", False)
            
        print(f"[+] 配置 '{current_env_name}' 加载成功! (BaseURL: {base_url})")
        print(f"    - Debug模式: {'[ON]' if DEBUG_MODE else '[OFF]'}")
        return True
    except Exception as e:
        print(f"[-] 读取配置文件错误: {e}")
        return False

class SmartProxy(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # 关闭默认的混乱日志

    def do_GET(self):
        """完美骗过客户端的 GET 探针"""
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
            
            # --- 模型映射逻辑 ---
            cleaned = original_model.lower().replace('/', '-').split('-')
            base_model = cleaned[-1] if cleaned else 'haiku'
            model_map = current_config.get("model_mapping", {})
            target_model = model_map.get(base_model, model_map.get('haiku'))
            
            if not target_model:
                raise ValueError(f"未找到型号 '{base_model}' 的映射规则")
                
            data["model"] = target_model
            new_body = json.dumps(data).encode('utf-8')
            # --------------------

            # ===== 终端颜色代码 =====
            CYAN = '\033[96m'
            GREEN = '\033[92m'
            RED = '\033[91m'
            ENDC = '\033[0m'
            BOLD = '\033[1m'

            # ===== Debug 日志：打印收到的请求详情 =====
            if DEBUG_MODE:
                print(f"\n{CYAN}{'='*25} 📩 收到新的业务请求 {'='*25}{ENDC}")
                print(f"{BOLD}🌐 请求路径:{ENDC} {self.path}")
                print(f"{BOLD}🤖 模型转换:{ENDC} {original_model}  -->  {target_model}")
                print(f"{BOLD}📦 请求体原文:{ENDC}\n{json.dumps(json.loads(post_data.decode('utf-8')), indent=2, ensure_ascii=False)}")

            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {current_config.get("api_key")}'
            }
            
            # 拼接目标 URL
            base_url = current_config.get("api_base_url", "")
            target_url = urljoin(base_url + '/', self.path.lstrip('/'))

            # 转发请求
            response = requests.post(target_url, data=new_body, headers=headers, timeout=120)
            
            # ===== Debug 日志：打印返回的响应详情 =====
            if DEBUG_MODE:
                print(f"{GREEN}{'='*25} 📤 上游服务器响应 {'='*25}{ENDC}")
                print(f"{BOLD}🚀 转发至:{ENDC} {target_url}")
                print(f"{BOLD}📊 状态码:{ENDC} {response.status_code}")
                print(f"{BOLD}📦 响应体原文:{ENDC}\n{response.text}")
                print(f"{CYAN}{'='*25} 🏁 本次请求结束 {'='*25}{ENDC}\n")

            # 将上游响应抛回给客户端
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.content)

        except Exception as e:
            print(f"{RED}❌ [POST] 处理失败: {traceback.format_exc()}{ENDC}")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

def keyboard_listener(server):
    """监听键盘输入，支持热加载配置"""
    global current_config
    while True:
        cmd = input().strip().lower()
        if cmd == 'r':
            print("[*] 正在重新加载配置...")
            if load_config():
                print("[+] 配置热更新成功！")
            else:
                print("[-] 配置热更新失败，继续使用当前配置运行。")
        elif cmd == 'q':
            print("[*] 接收到退出指令，正在关闭服务器...")
            server.shutdown()
            sys.exit(0)

if __name__ == '__main__':
    if not load_config():
        print("[-] 初始配置加载失败，请检查 config.yaml 文件。程序退出。")
        sys.exit(1)
        
    server_address = ('127.0.0.1', 8899)
    httpd = HTTPServer(server_address, SmartProxy)
    
    # 启动键盘监听线程
    listener_thread = Thread(target=keyboard_listener, args=(httpd,), daemon=True)
    listener_thread.start()
    
    print(f"[*] 本地智能代理已启动，正在监听 http://127.0.0.1:8899")
    print(f"[*] 按键指令 -> 按 'r' 键重新加载配置，按 'q' 键退出程序")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 服务器已终止。")
        httpd.server_close()