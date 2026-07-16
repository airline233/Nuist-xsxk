import requests
import argparse
import base64
import json
import time
import sys
import os
import datetime
import logging
import threading
import ssl
import certifi
import re
import truststore
from urllib.parse import urlencode, quote_plus
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import ddddocr
import websocket
from rich.console import Console
from rich.table import Table
from rich.text import Text
from bs4 import BeautifulSoup
from vpnlogin import NuistVPNClient

# --- 全局配置 ---

# AES 加密密钥 (来自前端代码)
AES_KEY = "MWMqg2tPcDkxcm11".encode('utf-8')

# 课程类型常量
COURSE_TYPE_FANKC = "FANKC"  # 泛选课
COURSE_TYPE_TYKC = "TYKC"    # 体育课

# API 端点
BASE_URL = "https://client.vpn.nuist.edu.cn/https/webvpn3315a96df5a2811a49489fcebfe8b135dece10c6255d04cc36c652f60ee89b3a/xsxk"
# BASE_URL = "http://xsxk.nuist.edu.cn/xsxk"
URL_CAPTCHA = f"{BASE_URL}/auth/captcha?enlink-vpn"
URL_LOGIN = f"{BASE_URL}/auth/login?enlink-vpn"
URL_LIST_CLASSES = f"{BASE_URL}/elective/clazz/list?enlink-vpn"
URL_ADD_CLASS = f"{BASE_URL}/elective/clazz/add?enlink-vpn"
URL_DEL_CLASS = f"{BASE_URL}/elective/clazz/del?enlink-vpn"
URL_SWITCH_BATCH = f"{BASE_URL}/elective/user?enlink-vpn"
URL_GET_USER_INFO = f"{BASE_URL}/elective/user?enlink-vpn"
DEFAULT_COOKIE_FILE = "ck.txt"

# 通用请求头
COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Sec-Ch-Ua": "\"Chromium\";v=\"122\", \"Not(A:Brand\";v=\"24\", \"Google Chrome\";v=\"122\"",
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": "\"Windows\"",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 全局 Logger（在 setup_logging 中初始化）
main_logger = logging.getLogger("main")
heartbeat_logger = logging.getLogger("heartbeat")

def setup_logging(student_id):
    """根据学号初始化日志系统
    
    创建日志目录结构：
    logs/{学号}/course_grab.log  - 主日志（选课操作、登录等）
    logs/{学号}/heartbeat.log   - 心跳日志（HTTP心跳 + WebSocket心跳）
    
    Args:
        student_id: 学号
    """
    global main_logger, heartbeat_logger
    
    # 创建日志目录
    log_dir = os.path.join(os.path.dirname(__file__), "logs", str(student_id))
    os.makedirs(log_dir, exist_ok=True)
    
    # 日志格式
    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # 配置主日志 logger
    main_logger = logging.getLogger("main")
    main_logger.setLevel(logging.INFO)
    main_logger.handlers.clear()  # 清除已有 handlers
    
    main_handler = logging.FileHandler(
        os.path.join(log_dir, "course_grab.log"),
        encoding='utf-8'
    )
    main_handler.setFormatter(log_format)
    main_logger.addHandler(main_handler)
    
    # 配置心跳日志 logger
    heartbeat_logger = logging.getLogger("heartbeat")
    heartbeat_logger.setLevel(logging.DEBUG)  # 心跳日志记录更详细
    heartbeat_logger.handlers.clear()
    
    heartbeat_handler = logging.FileHandler(
        os.path.join(log_dir, "heartbeat.log"),
        encoding='utf-8'
    )
    heartbeat_handler.setFormatter(log_format)
    heartbeat_logger.addHandler(heartbeat_handler)
    
    main_logger.info(f"日志系统已初始化，学号: {student_id}")
    print(f"[✓] 日志目录: {log_dir}")


# 全局登录状态（用于重登时更新）
class LoginState:
    def __init__(self):
        self.session = None
        self.token = None
        self.username = None
        self.password = None
        self.use_vpn = False
        self.vpn_client = None
        self.batch_id = None
        self.campus = None

login_state = LoginState()


def relogin_vpn():
    """重新登录VPN"""
    if login_state.use_vpn and login_state.vpn_client:
        print("\n[!] 正在重新登录VPN...")
        main_logger.info("正在重新登录VPN")
        try:
            cookies_dict = login_state.vpn_client.login_and_get_cookies()
            login_state.session.cookies.update(cookies_dict)
            print("[✓] VPN重新登录成功")
            main_logger.info("VPN重新登录成功")
            return True
        except Exception as e:
            print(f"[✗] VPN重新登录失败: {e}")
            main_logger.error(f"VPN重新登录失败: {e}")
            return False
    return True  # 不使用VPN时直接返回True


def relogin_system():
    """重新登录选课系统"""
    print("\n[!] 正在重新登录选课系统...")
    main_logger.info("正在重新登录选课系统")
    try:
        login_data = login(login_state.session, login_state.username, login_state.password)
        if login_data:
            login_state.token = login_data.get("token")
            print("[✓] 选课系统重新登录成功")
            main_logger.info("选课系统重新登录成功")
            return True
        return False
    except Exception as e:
        print(f"[✗] 选课系统重新登录失败: {e}")
        main_logger.error(f"选课系统重新登录失败: {e}")
        return False


def handle_relogin(response=None):
    """
    处理重新登录逻辑
    - 如果有302跳转，重登VPN+选课系统
    - 没有302则直接重登选课系统，验证码获取失败时再重登VPN
    返回: (success, new_token)
    """
    # 检查是否有302跳转（通过response.history或状态码判断）
    has_redirect = False
    if response is not None:
        # requests 默认会自动跟随重定向，可以通过 history 检查
        if response.history:
            for hist in response.history:
                if hist.status_code in [301, 302, 303, 307, 308]:
                    has_redirect = True
                    print(f"[!] 检测到重定向: {hist.status_code} -> {hist.headers.get('Location', '')}")
                    main_logger.info(f"检测到重定向: {hist.status_code}")
                    break
        # 也检查最终URL是否包含登录页面特征
        if 'login' in response.url.lower() or 'auth' in response.url.lower():
            has_redirect = True
            print(f"[!] 检测到跳转到登录页: {response.url}")
    
    if has_redirect:
        # 有302跳转，重登VPN+选课系统
        print("[!] 检测到302跳转，需要重新登录VPN和选课系统")
        if not relogin_vpn():
            return False, None
        if not relogin_system():
            return False, None
    else:
        # 没有302，直接尝试重登选课系统
        print("[!] 尝试重新登录选课系统...")
        try:
            if not relogin_system():
                # 验证码获取失败等情况，尝试重登VPN
                print("[!] 选课系统登录失败，尝试重新登录VPN...")
                if not relogin_vpn():
                    return False, None
                if not relogin_system():
                    return False, None
        except Exception as e:
            # 捕获验证码获取失败等异常
            print(f"[!] 登录异常: {e}，尝试重新登录VPN...")
            main_logger.error(f"登录异常: {e}")
            if not relogin_vpn():
                return False, None
            if not relogin_system():
                return False, None
    
    return True, login_state.token


# HTTP 心跳管理类（用于维持登录态）
class HttpHeartbeat:
    """HTTP 心跳管理，每隔30秒请求课程列表保持登录态"""
    
    def __init__(self, session, token, batch_id, campus, interval=30):
        self.session = session
        self.token = token
        self.batch_id = batch_id
        self.campus = campus
        self.interval = interval
        self.running = False
        self.thread = None
    
    def _heartbeat_loop(self):
        """心跳循环，每interval秒请求一次课程列表"""
        while self.running:
            try:
                time.sleep(self.interval)
                if not self.running:
                    break
                
                headers = {
                    **COMMON_HEADERS,
                    "Authorization": self.token,
                    "Batchid": self.batch_id,
                    "Content-Type": "application/json;charset=UTF-8"
                }
                body = {
                    "teachingClassType": "FANKC",
                    "pageNumber": 1,
                    "pageSize": 10,
                    "orderBy": "",
                    "campus": self.campus,
                    "SFYX": "2"
                }
                
                response = self.session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=10)
                
                try:
                    data = response.json()
                    code = data.get("code")
                    if code == 200:
                        heartbeat_logger.debug("[HTTP心跳] 成功")
                    else:
                        heartbeat_logger.warning(f"[HTTP心跳] 响应code={code}, msg={data.get('msg')}")
                except json.JSONDecodeError as e:
                    print(f"\n[HTTP心跳] JSON解析失败，触发重登流程: {e}")
                    heartbeat_logger.warning(f"[HTTP心跳] JSON解析失败: {e}, 响应: {response.text[:200]}")
                    
                    # 立即执行重登流程
                    success, new_token = handle_relogin(response)
                    if success and new_token:
                        self.token = new_token
                        login_state.token = new_token
                        print("[HTTP心跳] 重新登录成功")
                        heartbeat_logger.info("[HTTP心跳] 重新登录成功")
                    else:
                        print("[HTTP心跳] 重新登录失败")
                        heartbeat_logger.error("[HTTP心跳] 重新登录失败")
                        
            except requests.exceptions.RequestException as e:
                heartbeat_logger.warning(f"[HTTP心跳] 请求异常: {e}")
            except Exception as e:
                heartbeat_logger.error(f"[HTTP心跳] 异常: {e}")
    
    def update_token(self, new_token):
        """更新token（重登后调用）"""
        self.token = new_token
    
    def start(self):
        """启动HTTP心跳（在后台线程运行）"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.thread.start()
        heartbeat_logger.info(f"[HTTP心跳] 已启动，间隔 {self.interval} 秒")
    
    def stop(self):
        """停止HTTP心跳"""
        self.running = False
        heartbeat_logger.info("[HTTP心跳] 已停止")


# WebSocket 心跳管理类
class WebSocketHeartbeat:
    """WebSocket 心跳管理，每隔5秒发送 'hi' 保持连接"""
    
    def __init__(self, student_id, cookies=None, use_vpn=False):
        self.student_id = student_id
        self.cookies = cookies
        self.use_vpn = use_vpn
        self.ws = None
        self.running = False
        self.thread = None
        self.heartbeat_thread = None
        
        # 根据是否使用VPN选择不同的WebSocket地址
        if use_vpn:
            # VPN 模式下的 WebSocket 地址
            self.ws_url = f"wss://client.vpn.nuist.edu.cn/https/webvpn3315a96df5a2811a49489fcebfe8b135dece10c6255d04cc36c652f60ee89b3a/xsxk/websocket/{student_id}"
        else:
            self.ws_url = f"wss://xsxk.nuist.edu.cn/xsxk/websocket/{student_id}"
    
    def _on_open(self, ws):
        heartbeat_logger.info(f"[WebSocket] 连接已建立: {self.student_id}")
        # 启动心跳线程
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
    
    def _on_message(self, ws, message):
        # heartbeat_logger.debug(f"[WebSocket] 收到消息: {message}")
        try:
            data = json.loads(message)
            heartbeat_logger.info(f"[WebSocket] 解析消息: {data}")
            code = data.get("code")
            if code is not None and code != 200:
                msg = data.get("msg", "未知错误")
                print(f"\n[WebSocket 警告] code={code}, msg={msg}")
                heartbeat_logger.warning(f"[WebSocket] 非200响应: code={code}, msg={msg}")
        except json.JSONDecodeError:
            # 非 JSON 消息（如心跳响应），忽略
            pass
        except Exception as e:
            heartbeat_logger.error(f"[WebSocket] 解析消息异常: {e}")
    
    def _on_error(self, ws, error):
        heartbeat_logger.error(f"[WebSocket] 错误: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        heartbeat_logger.info(f"[WebSocket] 连接已关闭: {close_status_code} - {close_msg}")
        # 如果还在运行状态，尝试重连
        if self.running:
            heartbeat_logger.info("[WebSocket] 尝试重新连接...")
            time.sleep(2)
            self._connect()
    
    def _heartbeat_loop(self):
        """心跳循环，每5秒发送一次 'hi'"""
        while self.running and self.ws:
            try:
                if self.ws.sock and self.ws.sock.connected:
                    self.ws.send("hi")
                    heartbeat_logger.debug("[WebSocket] 发送心跳: hi")
                time.sleep(5)
            except Exception as e:
                heartbeat_logger.error(f"[WebSocket] 心跳发送失败: {e}")
                break
    
    def _connect(self):
        """建立 WebSocket 连接"""
        try:
            # 构建 cookie 字符串
            cookie_str = ""
            if self.cookies:
                cookie_str = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])
            
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                cookie=cookie_str if cookie_str else None
            )
            self.ws.run_forever()
        except Exception as e:
            heartbeat_logger.error(f"[WebSocket] 连接失败: {e}")
    
    def start(self):
        """启动 WebSocket 心跳（在后台线程运行）"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._connect, daemon=True)
        self.thread.start()
        heartbeat_logger.info(f"[WebSocket] 心跳线程已启动")
    
    def stop(self):
        """停止 WebSocket 心跳"""
        self.running = False
        if self.ws:
            self.ws.close()
        heartbeat_logger.info("[WebSocket] 心跳已停止")


# --- 辅助函数 ---

def encrypt_password(password):
    """使用 AES-128-ECB 加密密码"""
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    password_bytes = password.encode('utf-8')
    padded_password = pad(password_bytes, AES.block_size)
    encrypted_password = cipher.encrypt(padded_password)
    return base64.b64encode(encrypted_password).decode('utf-8')

def parse_cookies(cookie_str):
    """将从文件中读取的原始cookie字符串解析成字典格式"""
    cookies = {}
    try:
        for item in cookie_str.strip().split(';'):
            if '=' in item:
                key, value = item.strip().split('=', 1)
                cookies[key] = value
    except Exception as e:
        print(f"解析Cookie时出错: {e}")
    return cookies


def load_cookie_file(session, cookie_file_path):
    """从文件读取 cookie（raw: k=v; ...）并合并到 session.cookies。
    
    Args:
        session: requests.Session 对象
        cookie_file_path: cookie 文件路径
        
    Returns:
        bool: 是否成功加载
    """
    if not cookie_file_path:
        return True

    # 允许相对路径：相对当前脚本目录解析，更符合直接运行体验
    if not os.path.isabs(cookie_file_path):
        cookie_file_path = os.path.join(os.path.dirname(__file__), cookie_file_path)

    try:
        with open(cookie_file_path, 'r', encoding='utf-8') as f:
            cookie_string = f.read().strip()
        if not cookie_string:
            print(f"错误: cookie文件为空: {cookie_file_path}")
            return False

        cookies_dict = parse_cookies(cookie_string)
        if not cookies_dict:
            print(f"错误: 未能从cookie文件解析出任何字段: {cookie_file_path}")
            return False

        session.cookies.update(cookies_dict)
        main_logger.info(f"已合并cookie文件: {cookie_file_path} ({len(cookies_dict)} items)")
        return True
    except FileNotFoundError:
        print(f"错误: 未找到 cookie 文件: {cookie_file_path}")
        return False
    except Exception as e:
        print(f"读取或解析cookie文件时出错: {e}")
        main_logger.error(f"读取或解析cookie文件时出错: {e}")
        return False


def get_captcha(session):
    """获取并识别验证码"""
    print("正在获取验证码...")
    ocr = ddddocr.DdddOcr(show_ad=False)
    while True:
        try:
            headers = {**COMMON_HEADERS}
            response = session.post(URL_CAPTCHA, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("code") == 200:
                captcha_b64 = data["data"]["captcha"]
                uuid = data["data"]["uuid"]
                # 移除 base64 头部
                img_b64 = captcha_b64.split(',')[1]
                img_bytes = base64.b64decode(img_b64)
                
                captcha_text = ocr.classification(img_bytes)
                
                # 验证码通常是4位
                if len(captcha_text) == 4 and captcha_text.isalnum():
                    print(f"验证码识别成功: {captcha_text}")
                    return captcha_text, uuid
                else:
                    print(f"验证码识别结果 '{captcha_text}' 不符合规范, 正在重试...")
            else:
                print(f"获取验证码失败: {data.get('msg', '未知错误')}")

        except Exception as e:
            print(f"获取验证码时发生错误: {e}")
            time.sleep(1)

def login(session, username, password):
    """登录系统并获取 token"""
    while True:
        captcha_code, uuid = get_captcha(session)
        encrypted_pass = encrypt_password(password)
        
        headers = {
            **COMMON_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        body = {
            "loginname": username,
            "password": encrypted_pass,
            "captcha": captcha_code,
            "uuid": uuid
        }
        
        print("正在尝试登录...")
        try:
            response = session.post(URL_LOGIN, headers=headers, data=urlencode(body), timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") == 200:
                print(f"登录成功！欢迎你，{data['data']['student']['XM']}同学。")
                return data['data']
            else:
                print(f"登录失败: {data.get('msg', '密码或验证码错误')}，正在重试...")
                time.sleep(1)
        except Exception as e:
            print(f"登录请求时发生错误: {e}")
            time.sleep(1)

def get_user_info_from_cookies(session, token):
    """通过cookies获取用户信息（跳过登录时使用）"""
    main_logger.info("正在通过cookies获取用户信息...")
    
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # 先请求首页，解析 batch code（平台系统bug，需要先访问首页才能正常获取用户信息）
    batch_id = None
    try:
        profile_url = f"{BASE_URL}/profile/index.html"
        profile_response = session.get(profile_url, headers={**COMMON_HEADERS}, timeout=10)
        profile_response.raise_for_status()
        
        # 使用 BeautifulSoup 解析 HTML，定位包含 var batch 的 script 标签
        soup = BeautifulSoup(profile_response.text, 'html.parser')
        for script in soup.find_all('script'):
            script_text = script.string
            if script_text and 'var batch' in script_text:
                # 在 script 内容中查找 var batch = {...};
                # 匹配 var batch = 到下一个分号之间的 JSON 对象
                match = re.search(r'var\s+batch\s*=\s*(\{.*?\});', script_text, re.DOTALL)
                if match:
                    try:
                        batch_json_str = match.group(1)
                        batch_data = json.loads(batch_json_str)
                        batch_id = batch_data.get("code")
                        if batch_id:
                            main_logger.info(f"从首页解析到 batchId: {batch_id}")
                            break
                    except json.JSONDecodeError as e:
                        print(f"[!] 解析 batch JSON 失败: {e}")
                        main_logger.warning(f"解析 batch JSON 失败: {e}")
        
        if not batch_id:
            print("[!] 未在首页找到 batch 变量")
            main_logger.warning("未在首页找到 batch 变量")
    except Exception as e:
        print(f"[!] 请求首页时发生错误: {e}")
        main_logger.warning(f"请求首页时发生错误: {e}")
    
    try:
        # 使用 POST 请求并带上 batchId（即使解析失败也继续尝试）
        body = f"batchId={batch_id}" if batch_id else ""
        response = session.post(URL_GET_USER_INFO, headers=headers, data=body, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") == 200:
            user_data = data.get("data", {})
            student_info = user_data.get("student", {})
            print(f"获取用户信息成功！欢迎你，{student_info.get('XM', '未知')}同学。")
            # 返回格式与login一致，添加token字段
            user_data["token"] = token
            return user_data
        else:
            print(f"获取用户信息失败: {data.get('msg', '未知错误')}")
            return None
    except Exception as e:
        print(f"获取用户信息时发生错误: {e}")
        return None


def choose_elective_batch(student_data):
    """让用户选择一个可选的选课轮次
    
    Returns:
        tuple: (batch, course_type) 或 (None, None)
    """
    print("\n--- 请选择一个选课轮次 ---")
    batches = student_data.get("student", {}).get("electiveBatchList", [])
    
    # 筛选出 canSelect 为 "1" 的可选轮次
    available_batches = [b for b in batches if b.get("canSelect") == "1"]
    
    if not available_batches:
        print("未找到当前可用的选课轮次。")
        return None, None

    for i, batch in enumerate(available_batches):
        # 判断课程类型并标注
        batch_name = batch['name']
        type_tag = "[体育]" if "体育" in batch_name else "[泛选课]"
        print(f"  [{i+1}] {type_tag} {batch_name} ({batch['beginTime']} - {batch['endTime']})")
    
    while True:
        try:
            choice = int(input("请输入数字序号选择轮次: "))
            if 1 <= choice <= len(available_batches):
                selected_batch = available_batches[choice-1]
                # 根据轮次名称自动判断课程类型
                course_type = COURSE_TYPE_TYKC if "体育" in selected_batch['name'] else COURSE_TYPE_FANKC
                return selected_batch, course_type
            else:
                print("无效的输入，请输入列表中的数字。")
        except ValueError:
            print("请输入一个有效的数字。")
            
def choose_course_from_list(courses, course_type=COURSE_TYPE_FANKC):
    """当轮次中有多门不同课程时，让用户先选择课程
    
    Args:
        courses: 课程列表（rows）
        course_type: 课程类型
        
    Returns:
        dict or None: 选中的课程对象
    """
    console = Console()
    
    print("\n--- 该轮次下有多门课程，请先选择要抢的课程 ---")
    
    # 判断是否为体育课
    is_pe_course = course_type == COURSE_TYPE_TYKC
    
    # 根据课程数量动态决定列数
    total = len(courses)
    if total <= 6:
        COLS = 2
    elif total <= 12:
        COLS = 3
    elif total <= 20:
        COLS = 4
    else:
        COLS = 5
    
    table = Table(title="可选课程列表", show_header=False, 
                  box=None, padding=(0, 1), collapse_padding=True)
    
    # 添加列
    for _ in range(COLS):
        table.add_column(justify="left", no_wrap=False, overflow="fold")
    
    # 构建单元格内容
    def build_course_cell(idx, course):
        tc_list = course.get("tcList", [])
        tc_count = len(tc_list)
        
        if is_pe_course:
            course_name = tc_list[0].get('projectName', course.get('KCM', '未知课程')) if tc_list else course.get('KCM', '未知课程')
        else:
            course_name = course.get('KCM', '未知课程')
        
        text = Text()
        text.append(f"[{idx}] ", style="bold cyan")
        text.append(f"{course_name}\n", style="bold")
        text.append(f"({tc_count}个教学班)", style="dim")
        return text
    
    # 将课程分组并添加到表格
    for i in range(0, total, COLS):
        row_cells = []
        for j in range(COLS):
            idx = i + j
            if idx < total:
                row_cells.append(build_course_cell(idx + 1, courses[idx]))
            else:
                row_cells.append("")
        table.add_row(*row_cells)
    
    console.print(table)
    console.print(f"共 [bold]{total}[/bold] 门课程，输入 [bold cyan]0[/bold cyan] 返回上一级\n")
    
    while True:
        try:
            choice = int(input("请输入课程序号 (0返回): ").strip())
            if choice == 0:
                return "BACK"
            if 1 <= choice <= len(courses):
                selected = courses[choice - 1]
                tc_list = selected.get("tcList", [])
                if is_pe_course:
                    course_name = tc_list[0].get('projectName', selected.get('KCM', '未知课程')) if tc_list else selected.get('KCM', '未知课程')
                else:
                    course_name = selected.get('KCM', '未知课程')
                print(f"已选择课程: {course_name}")
                return selected
            else:
                print("无效的输入，请输入列表中的数字。")
        except ValueError:
            print("请输入一个有效的数字。")


def choose_class(session, token, batch_id, campus, course_type=COURSE_TYPE_FANKC):
    """获取课程列表并让用户选择（支持二级菜单）
    
    Args:
        course_type: 课程类型，COURSE_TYPE_FANKC(方案内课程) 或 COURSE_TYPE_TYKC(体育课)
    """
    print("\n正在获取课程列表中...")
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    body0 = "batchId=" + batch_id
    # 先发送一次请求，切换轮次
    try:
        response0 = session.post(URL_SWITCH_BATCH, headers=headers, data=body0, timeout=10)
        response0.raise_for_status()
        data0 = response0.json()
        if data0.get("code") != 200:
            print(f"切换轮次失败: {data0.get('msg')}")
            return None, None
    except Exception as e:
        print(f"切换轮次时发生错误: {e}")
        return None, None
    
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Batchid": batch_id,
        "Content-Type": "application/json;charset=UTF-8"
    }
    # 增加 pageSize 以获取所有课程
    body = {
        "teachingClassType": course_type,  # 根据课程类型设置
        "pageNumber": 1,
        "pageSize": 200, # 设置一个较大的值
        "orderBy": "",
        "campus": campus,
        "SFYX": "2" # 2 通常表示所有
    }

    try:
        response = session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=20)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 200:
            print(f"获取课程列表失败: {data.get('msg')}")
            return None, None
        
        # 获取所有课程（rows）
        courses = data.get("data", {}).get("rows", [])
        
        if not courses:
            print("在此轮次下未找到可选的课程。")
            return None, None
        
        # 课程选择循环（支持返回上一级）
        while True:
            # 判断是否需要选择课程：
            # 1. 如果有多门不同的课程，检查总教学班数量
            # 2. 如果总教学班数量较少（<10个），自动全部展开展示
            should_expand_all = False
            if len(courses) > 1:
                total_classes = sum(len(course.get("tcList", [])) for course in courses)
                should_expand_all = total_classes < 10  # 总教学班少于10个时自动展开
                
                if should_expand_all:
                    print(f"\n总计 {total_classes} 个教学班，自动全部展开展示")
                    all_classes = []
                    for course in courses:
                        tc_list = course.get("tcList", [])
                        for clazz in tc_list:
                            # 标记每个教学班所属的课程信息
                            clazz["_parent_course"] = course.get("KCM", "")
                        all_classes.extend(tc_list)
                    selected_course = None  # 标记为自动展开模式
                else:
                    # 教学班较多，先让用户选择课程
                    selected_course = choose_course_from_list(courses, course_type)
                    if selected_course == "BACK":
                        # 返回上一级（轮次选择）
                        return "BACK", None
                    if not selected_course:
                        return "BACK", None
                    all_classes = selected_course.get("tcList", [])
            else:
                # 只有一门课程，直接展开
                all_classes = courses[0].get("tcList", [])
                selected_course = None  # 标记为单课程模式
            
            if not all_classes:
                print("在此轮次下未找到可选的课程。")
                return None, None
            
            # 选择教学班
            result = _choose_teaching_class(all_classes, course_type, has_multi_courses=(len(courses) > 1), is_auto_expand=should_expand_all)
            if result == "BACK":
                # 返回课程选择（如果有多门课程）
                if len(courses) > 1 and not should_expand_all:
                    continue  # 重新选择课程
                else:
                    return "BACK", None
            
            selected_classes = result
            return selected_classes, all_classes

    except Exception as e:
        print(f"获取课程列表时发生错误: {e}")
        return None, None


def _choose_teaching_class(all_classes, course_type, has_multi_courses=False, is_auto_expand=False):
    """选择教学班（内部函数）
    
    Args:
        all_classes: 教学班列表
        course_type: 课程类型
        has_multi_courses: 是否有多门课程（影响返回提示）
        is_auto_expand: 是否为自动展开模式（跳过课程选择直接展示教学班）
        
    Returns:
        list or str: 选中的教学班列表，或 "BACK" 表示返回上一级
    """
    # 判断是否为体育课
    is_pe_course = course_type == COURSE_TYPE_TYKC
    
    # 根据课程数量动态决定列数 (体育课信息多，用较少列数)
    total = len(all_classes)
    if is_pe_course:
        # 体育课信息较多，使用较少列数
        if total <= 6:
            COLS = 2
        elif total <= 12:
            COLS = 3
        else:
            COLS = 4
    else:
        if total <= 9:
            COLS = 3
        elif total <= 16:
            COLS = 4
        else:
            COLS = 5
    
    console = Console()
    table = Table(title="请选择要抢的教学班", show_header=False, 
                  box=None, padding=(0, 1), collapse_padding=True)
    
    # 添加列
    for _ in range(COLS):
        table.add_column(justify="left", no_wrap=False, overflow="fold")
    
    # 构建单元格内容
    def build_cell(idx, clazz):
        """构建单个课程的显示内容"""
        text = Text()
        text.append(f"[{idx}] ", style="bold cyan")
        
        # 检查是否为自动展开模式（有父级课程信息）
        parent_course = clazz.get("_parent_course")
        if parent_course:
            # 自动展开模式：显示父级课程信息
            text.append(f"【{parent_course}】\n", style="magenta")
        
        if is_pe_course:
            # 体育课：显示项目名和分类
            project_name = clazz.get('projectName', '')
            classification = clazz.get('classificationName', '')
            # 分类样式：选项课绿色，锻炼课黄色
            class_style = "green" if classification == "选项课" else "yellow"
            text.append(f"{project_name}", style="bold")
            text.append(f" ({classification})\n", style=class_style)
            text.append(f"{clazz['SKJS']} ", style="green")
            text.append(f"[{clazz['YXRS']}/{clazz['KRL']}]\n", style="yellow")
        else:
            # 方案内课程：原有显示逻辑
            text.append(f"{clazz['KCM']}\n", style="bold")
            text.append(f"{clazz['SKJS']} ", style="green")
            text.append(f"[{clazz['YXRS']}/{clazz['KRL']}]\n", style="yellow")
        
        # 时间地点按逗号分隔
        try:
            teaching_place = clazz.get('teachingPlace') or "未安排地点"
            place_parts = [p.strip() for p in teaching_place.split(',') if p.strip()]
            for i, part in enumerate(place_parts):
                text.append(part, style="dim")
                if i < len(place_parts) - 1:
                    text.append("\n")
        except Exception as e:
            print(f"处理时间地点信息时发生错误: {e}")
            text.append("未安排地点", style="dim")
        return text
    
    # 将课程分组并添加到表格
    for i in range(0, total, COLS):
        row_cells = []
        for j in range(COLS):
            idx = i + j
            if idx < total:
                row_cells.append(build_cell(idx + 1, all_classes[idx]))
            else:
                row_cells.append("")  # 空单元格
        table.add_row(*row_cells)
    
    console.print(table)
    # 根据模式决定返回提示
    if is_auto_expand:
        back_hint = "返回上一级"  # 自动展开模式下直接返回轮次选择
    else:
        back_hint = "返回课程选择" if has_multi_courses else "返回上一级"
    console.print(f"共 [bold]{total}[/bold] 个教学班，输入 [bold cyan]0[/bold cyan] {back_hint}\n")

    while True:
        choice_str = input("请输入教学班序号（多个用逗号或空格分隔，0返回）: ").strip()
        if not choice_str:
            print("请输入至少一个序号。")
            continue
        
        # 检查是否返回
        if choice_str == "0":
            return "BACK"
        
        try:
            # 支持逗号或空格分隔
            import re
            choices = [int(x.strip()) for x in re.split(r'[,\s]+', choice_str) if x.strip()]
            
            # 检查是否包含0（返回）
            if 0 in choices:
                return "BACK"
            
            if not choices:
                print("请输入至少一个序号。")
                continue
            
            selected_classes = []
            invalid_choices = []
            for choice in choices:
                if 1 <= choice <= len(all_classes):
                    # 避免重复添加同一个课程
                    clazz = all_classes[choice - 1]
                    if clazz not in selected_classes:
                        selected_classes.append(clazz)
                else:
                    invalid_choices.append(choice)
            
            if invalid_choices:
                print(f"警告: 序号 {invalid_choices} 无效，已跳过。")
            
            if selected_classes:
                return selected_classes
                
        except ValueError:
            print("输入格式错误，请输入数字序号。")


def drop_class(session, token, batch_id, course_to_drop, course_type=COURSE_TYPE_FANKC):
    """退掉指定课程
    
    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        course_to_drop: 要退的课程信息（含 secretVal）
        course_type: 课程类型
        
    Returns:
        bool: 是否退课成功
    """
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "batchId": batch_id,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    body = {
        "clazzType": course_type,
        "clazzId": course_to_drop['JXBID'],
        "secretVal": course_to_drop['secretVal']
    }
    
    try:
        response = session.post(URL_DEL_CLASS, headers=headers, data=urlencode(body, quote_via=quote_plus), timeout=10)
        result = response.json()
        
        code = result.get('code')
        msg = result.get('msg', '无消息')
        
        if code == 200:
            print(f"[✓] 退课成功: {msg}")
            main_logger.info(f"退课成功: {course_to_drop.get('KCM', '未知')} - {msg}")
            return True
        else:
            print(f"[✗] 退课失败: {code} - {msg}")
            main_logger.warning(f"退课失败: {code} - {msg}")
            return False
    except Exception as e:
        print(f"[✗] 退课请求异常: {e}")
        main_logger.error(f"退课请求异常: {e}")
        return False


def get_course_capacity(session, token, batch_id, campus, target_class_id, course_type=COURSE_TYPE_FANKC):
    """查询指定课程的当前容量信息
    
    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        campus: 校区
        target_class_id: 目标课程的 JXBID
        course_type: 课程类型
        
    Returns:
        tuple: (已选人数, 课容量, 课程对象) 或 (None, None, None)
    """
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Batchid": batch_id,
        "Content-Type": "application/json;charset=UTF-8"
    }
    
    body = {
        "teachingClassType": course_type,
        "pageNumber": 1,
        "pageSize": 200,
        "orderBy": "",
        "campus": campus,
        "SFYX": "2"
    }
    
    try:
        response = session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=10)
        data = response.json()
        
        if data.get("code") != 200:
            return None, None, None
        
        courses = data.get("data", {}).get("rows", [])
        for course in courses:
            for clazz in course.get("tcList", []):
                if clazz['JXBID'] == target_class_id:
                    return int(clazz['YXRS']), int(clazz['KRL']), clazz
        
        return None, None, None
    except Exception as e:
        print(f"[!] 查询课容量异常: {e}")
        return None, None, None


def start_monitoring(session, token, batch_id, campus, target_class, drop_class_info, course_type=COURSE_TYPE_FANKC):
    """监控目标课程容量，检测到有空位时先退课再选课
    
    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        campus: 校区
        target_class: 目标课程信息
        drop_class_info: 要退的课程信息（含 secretVal）
        course_type: 课程类型
    """
    target_id = target_class['JXBID']
    
    # 获取课程显示名称
    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz.get('KCM', '未知'))}({clazz.get('classificationName', '')})"
        return clazz.get('KCM', '未知')
    
    target_name = get_display_name(target_class)
    drop_name = get_display_name(drop_class_info)
    
    print("\n" + "="*60)
    print(f"监控目标课程: {target_name} - {target_class['SKJS']}")
    print(f"准备退掉课程: {drop_name} - {drop_class_info['SKJS']}")
    print("每 5 秒检测一次课容量，检测到空位后自动退课并选课。")
    print("按 Ctrl+C 停止监控。")
    print("="*60 + "\n")
    
    main_logger.info(f"开始监控: 目标={target_name}, 退课={drop_name}")
    
    current_token = token
    check_count = 0
    
    try:
        while True:
            check_count += 1
            current_selected, capacity, updated_class = get_course_capacity(
                session, current_token, batch_id, campus, target_id, course_type
            )
            
            if current_selected is None:
                print(f"[{check_count}] 查询失败，5秒后重试...", end='\r')
                time.sleep(5)
                continue
            
            dt = datetime.datetime.now().strftime("%H:%M:%S")
            
            if current_selected < capacity:
                # 有空位！
                print(f"\n[{dt}] 检测到空位！当前 {current_selected}/{capacity}")
                main_logger.info(f"检测到空位: {current_selected}/{capacity}")
                
                # 1. 先退课
                print(f"[>] 正在退课: {drop_name}...")
                drop_success = drop_class(session, current_token, batch_id, drop_class_info, course_type)
                
                if not drop_success:
                    print("[!] 退课失败，继续监控...")
                    main_logger.warning("退课失败，继续监控")
                    time.sleep(5)
                    continue
                
                # 2. 再选课（使用更新后的 secretVal）
                print(f"[>] 正在选课: {target_name}...")
                
                headers = {
                    **COMMON_HEADERS,
                    "Authorization": current_token,
                    "batchId": batch_id,
                    "Content-Type": "application/x-www-form-urlencoded"
                }
                
                # 使用查询到的最新课程信息（含新的 secretVal）
                select_class = updated_class if updated_class else target_class
                body = {
                    "clazzType": course_type,
                    "clazzId": select_class['JXBID'],
                    "secretVal": select_class['secretVal']
                }
                
                try:
                    response = session.post(URL_ADD_CLASS, headers=headers, 
                                          data=urlencode(body, quote_via=quote_plus), timeout=10)
                    result = response.json()
                    code = result.get('code')
                    msg = result.get('msg', '无消息')
                    
                    if code == 200:
                        print(f"\n[✓] 选课成功！{target_name} - {target_class['SKJS']}")
                        main_logger.info(f"选课成功: {target_name}")
                        break
                    else:
                        print(f"[✗] 选课失败: {code} - {msg}")
                        main_logger.warning(f"选课失败: {code} - {msg}")
                        print("[!] 继续监控...")
                except Exception as e:
                    print(f"[✗] 选课请求异常: {e}")
                    main_logger.error(f"选课请求异常: {e}")
            else:
                # 课程已满
                print(f"[{dt}] [{check_count}] {target_name}: {current_selected}/{capacity} (已满)", end='\r')
            
            time.sleep(5)
    
    except KeyboardInterrupt:
        print("\n\n监控已由用户手动停止。")
        main_logger.info("监控已停止")


def start_grabbing(session, token, batch_id, selected_class, backup_classes=None, course_type=COURSE_TYPE_FANKC):
    """开始循环抢课，支持备选课程切换
    
    Args:
        course_type: 课程类型，COURSE_TYPE_FANKC(泛选课) 或 COURSE_TYPE_TYKC(体育课)
    """
    if backup_classes is None:
        backup_classes = []
    
    # 构建课程队列：主课程 + 备选课程
    course_queue = [selected_class] + backup_classes
    current_index = 0
    
    current_class = course_queue[current_index]
    class_id = current_class['JXBID']
    secret_val = current_class['secretVal']
    
    # 获取课程显示名称（体育课用项目名，泛选课用课程名）
    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz['KCM'])}({clazz.get('classificationName', '')})"
        return clazz['KCM']

    print("\n" + "="*50)
    print(f"准备抢课: {get_display_name(current_class)} - {current_class['SKJS']}")
    if backup_classes:
        print(f"备选课程数量: {len(backup_classes)}")
    print("按 Ctrl+C 停止脚本。")
    print("="*50 + "\n")

    # 使用当前token（可能会在重登后更新）
    current_token = token
    
    def build_headers(tk):
        return {
            **COMMON_HEADERS,
            "Authorization": tk,
            "batchId": batch_id,
            "Content-Type": "application/x-www-form-urlencoded"
        }
    
    def build_body(clazz):
        body = {
            "clazzType": course_type,  # 根据课程类型设置
            "clazzId": clazz['JXBID'],
            "secretVal": clazz['secretVal']
        }
        return urlencode(body, quote_via=quote_plus)

    headers = build_headers(current_token)
    encoded_body = build_body(current_class)
    
    # 连续JSON解析失败计数
    json_fail_count = 0
    MAX_JSON_FAIL = 3  # 连续失败3次后触发重登

    try:
        while True:
            try:
                response = session.post(URL_ADD_CLASS, headers=headers, data=encoded_body, timeout=5)
                
                try:
                    result = response.json()
                    json_fail_count = 0  # 重置计数
                except json.JSONDecodeError as e:
                    json_fail_count += 1
                    print(f"\n[!] JSON解析失败 ({json_fail_count}/{MAX_JSON_FAIL}): {e}")
                    main_logger.warning(f"JSON解析失败: {e}, 响应内容: {response.text[:200]}")
                    
                    if json_fail_count >= MAX_JSON_FAIL:
                        print(f"\n[!] 连续{MAX_JSON_FAIL}次JSON解析失败，触发重新登录...")
                        main_logger.warning(f"连续{MAX_JSON_FAIL}次JSON解析失败，开始重登流程")
                        
                        # 执行重登逻辑
                        success, new_token = handle_relogin(response)
                        if success and new_token:
                            current_token = new_token
                            headers = build_headers(current_token)
                            json_fail_count = 0
                            print("[✓] 重新登录成功，继续抢课...")
                        else:
                            print("[✗] 重新登录失败，等待后重试...")
                            time.sleep(5)
                    
                    time.sleep(0.5)
                    continue
                
                code = result.get('code')
                msg = result.get('msg', '没有消息')

                main_logger.info(f"[{get_display_name(current_class)} - {current_class['SKJS']}] {code}: {msg}")

                # 课程已满，尝试切换到备选课程
                if msg.find("满") != -1:
                    print(f"\n[!] 当前课程 [{get_display_name(current_class)} - {current_class['SKJS']}] 已满")
                    
                    # 尝试切换到下一个备选课程
                    if current_index < len(course_queue) - 1:
                        current_index += 1
                        current_class = course_queue[current_index]
                        encoded_body = build_body(current_class)
                        print(f"[->] 切换到备选课程 {current_index}: {get_display_name(current_class)} - {current_class['SKJS']}")
                        main_logger.info(f"切换到备选课程: {get_display_name(current_class)}")
                    else:
                        print("[!] 所有课程（含备选）均已满，继续尝试最后一个课程...")

                # 根据 code 决定输出格式
                if code != 403:
                    # 403 通常是高频请求限制，不重要
                    dt = datetime.datetime.now()
                    course_name = get_display_name(current_class)[:10]  # 截断显示
                    print(f"[{course_name}] 状态: {code}, 信息: {msg} {dt}", end='\r')
                
                # 如果是成功或其他需要注意的错误，换行并打印完整 JSON
                if code not in [500, 403, 0]: # 500 是常见系统错误, 403 是频率限制, 0 是已满
                    print(f"\n收到关键响应: {json.dumps(result, ensure_ascii=False)}")
                
                # 成功选课 code == 200
                if code == 200:
                    print(f"\n[✓] 选课成功！课程: {get_display_name(current_class)} - {current_class['SKJS']}")
                    main_logger.info(f"选课成功: {get_display_name(current_class)}")
                    break
                
            except requests.exceptions.RequestException as e:
                print(f"请求失败: {e}", end='\r')

            time.sleep(0.3) # 可调整请求间隔，避免过快

    except KeyboardInterrupt:
        print("\n\n脚本已由用户手动停止。")
        input("按回车键退出...")
        sys.exit(0)


def confirm_course_selection(selected_classes, course_type=COURSE_TYPE_FANKC):
    """显示课程选择信息并请求用户确认
    
    Args:
        selected_classes: 选择的课程列表（第一个为主选，其余为备选）
        course_type: 课程类型
        
    Returns:
        tuple: (confirmed: bool, selected_class, backup_classes)
    """
    selected_class = selected_classes[0]
    backup_classes = selected_classes[1:] if len(selected_classes) > 1 else []
    
    # 获取课程显示名称
    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz['KCM'])}({clazz.get('classificationName', '')})"
        return clazz['KCM']
    
    print(f"\n已选择主课程: {get_display_name(selected_class)} - {selected_class['SKJS']} [{selected_class['YXRS']}/{selected_class['KRL']}]\n\t{selected_class.get('teachingPlace', '未安排地点')}")
    
    if backup_classes:
        print(f"\n备选课程 ({len(backup_classes)} 个):")
        for i, bc in enumerate(backup_classes):
            print(f"  备选{i+1}: {get_display_name(bc)} - {bc['SKJS']} [{bc['YXRS']}/{bc['KRL']}]\n\t{bc.get('teachingPlace', '未安排地点')}")
    
    confirm = input("\n是否确认提交选择？(y/n): ").strip().lower()
    return confirm == 'y', selected_class, backup_classes


def init_vpn_login(session, username, password):
    """初始化VPN登录
    
    Args:
        session: requests.Session 对象
        username: 用户名
        password: 密码
        
    Returns:
        NuistVPNClient or None: VPN客户端对象，失败时返回None
    """
    if not username or not password:
        print("错误: 使用VPN模式时必须提供 -u/--username 和 -p/--password 参数")
        return None
    
    client = NuistVPNClient(username=username, password=password)
    cookies_dict = client.login_and_get_cookies()
    session.cookies.update(cookies_dict)
    return client


def init_login(session, username, password, skip_login=False):
    """初始化登录流程
    
    Args:
        session: requests.Session 对象
        username: 用户名
        password: 密码
        skip_login: 是否跳过登录
        
    Returns:
        tuple: (login_data, token) 或 (None, None)
    """
    if skip_login:
        # 从 cookies 中提取 Authorization token
        token = session.cookies.get("Authorization")
        if not token:
            main_logger.error("错误: cookie文件中未找到 Authorization 字段")
            return None, None
        main_logger.info(f"[✓] 从Cookies中获取到Authorization token")
        
        # 通过API获取用户信息
        login_data = get_user_info_from_cookies(session, token)
        if not login_data:
            return None, None
        return login_data, token
    else:
        # 正常登录流程
        login_data = login(session, username, password)
        if not login_data:
            return None, None
        token = login_data.get("token")
        return login_data, token


def run_course_selection(session, token, batch_id, campus, username, use_vpn, course_type=COURSE_TYPE_FANKC):
    """运行监控抢课流程
    
    流程：
    1. 选择目标课程（只选1个）
    2. 选择要退的已选课程
    3. 确认后开始监控
    4. 检测到空位：退课 → 选课
    
    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        campus: 校区
        username: 学号（用于WebSocket）
        use_vpn: 是否使用VPN
        course_type: 课程类型
    """
    # 启动 HTTP 心跳（维持登录态，每30秒请求一次课程列表）
    http_heartbeat = HttpHeartbeat(
        session=session,
        token=token,
        batch_id=batch_id,
        campus=campus,
        interval=30
    )
    http_heartbeat.start()

    # 启动 WebSocket 心跳
    ws_heartbeat = WebSocketHeartbeat(
        student_id=username,
        cookies=dict(session.cookies),
        use_vpn=use_vpn
    )
    ws_heartbeat.start()

    try:
        # 1. 选择目标课程（只选1个）
        print("\n--- 第一步：选择要抢的目标课程 ---")
        result = choose_class(session, token, batch_id, campus, course_type)
        if result is None or result[0] is None:
            return
        selected_classes, all_classes = result
        
        # 只取第一个作为目标课程
        target_class = selected_classes[0]
        
        # 获取课程显示名称
        def get_display_name(clazz):
            if course_type == COURSE_TYPE_TYKC:
                return f"{clazz.get('projectName', clazz.get('KCM', '未知'))}({clazz.get('classificationName', '')})"
            return clazz.get('KCM', '未知')
        
        print(f"\n目标课程: {get_display_name(target_class)} - {target_class['SKJS']} [{target_class['YXRS']}/{target_class['KRL']}]")
        print(f"上课地点: {target_class['teachingPlace']}")
        
        # 2. 选择要退的课程（直接复用前面获取的课程列表）
        print("\n--- 第二步：选择要退掉的课程（从上面列表中选择）---")
        while True:
            try:
                drop_choice = int(input("请输入要退掉的课程序号: ").strip())
                if 1 <= drop_choice <= len(all_classes):
                    drop_class_info = all_classes[drop_choice - 1]
                    break
                else:
                    print("无效的序号，请重新输入。")
            except ValueError:
                print("请输入有效的数字。")
        
        print(f"\n要退的课程: {get_display_name(drop_class_info)} - {drop_class_info['SKJS']}")
        print(f"上课地点: {drop_class_info['teachingPlace']}")
        
        # 3. 确认
        print("\n" + "="*50)
        print("确认信息：")
        print(f"  目标课程: {get_display_name(target_class)} - {target_class['SKJS']}")
        print(f"  要退课程: {get_display_name(drop_class_info)} - {drop_class_info['SKJS']}")
        print("="*50)
        
        confirm = input("\n确认开始监控？(y/n): ").strip().lower()
        if confirm != 'y':
            print("操作已取消。")
            return
        
        # 4. 开始监控
        start_monitoring(session, token, batch_id, campus, target_class, drop_class_info, course_type)
        
    finally:
        http_heartbeat.stop()
        ws_heartbeat.stop()


def parse_arguments():
    """解析命令行参数
    
    Returns:
        argparse.Namespace: 解析后的参数对象
    """
    parser = argparse.ArgumentParser(description="NUIST 教务系统自动选课脚本")
    parser.add_argument("-u", "--username", required=False, 
                        help="学号（使用--skip-login时可选，但使用VPN时仍需提供）")
    parser.add_argument("-p", "--password", required=False, 
                        help="密码（使用--skip-login时可选，但使用VPN时仍需提供）")
    parser.add_argument(
        "-ck", "--cookie-file",
        default=None,
        help="可选：cookie 文件路径。文件格式：key=value; key2=value2; ...",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="跳过登录流程，从cookie文件读取Authorization token并通过API获取用户信息",
    )
    return parser.parse_args()


def validate_arguments(args):
    """验证命令行参数
    
    Args:
        args: 解析后的参数对象
        
    Returns:
        tuple: (valid: bool, cookie_file_path: str or None)
    """
    if args.skip_login:
        cookie_file_path = args.cookie_file if args.cookie_file else DEFAULT_COOKIE_FILE
        return True, cookie_file_path
    else:
        if not args.username or not args.password:
            print("错误: 必须提供 -u/--username 和 -p/--password 参数（除非使用 --skip-login）")
            return False, None
        return True, args.cookie_file


# --- 主程序 ---
def main():
    # 0. 信任系统证书目录
    truststore.inject_into_ssl()
    print("[✓] 已信任系统证书目录(truststore)")

    # 1. 解析并验证参数
    args = parse_arguments()
    valid, cookie_file_path = validate_arguments(args)
    if not valid:
        return
    
    # 初始化日志系统（使用学号，如果有的话）
    if args.username:
        setup_logging(args.username)
    else:
        setup_logging("unknown")

    # 2. 初始化 Session
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)
    
    # 初始化全局登录状态
    login_state.session = session
    login_state.username = args.username
    login_state.password = args.password
    
    # 3. VPN 登录（如果需要）
    use_vpn = BASE_URL.startswith("https://client.vpn.nuist.edu.cn")
    login_state.use_vpn = use_vpn
    
    if use_vpn:
        vpn_client = init_vpn_login(session, args.username, args.password)
        if not vpn_client:
            return
        login_state.vpn_client = vpn_client

    # 4. 合并 cookie 文件（在 VPN 登录后合并）
    if cookie_file_path:
        if not load_cookie_file(session, cookie_file_path):
            return
    
    # 5. 登录或跳过登录
    session.get(BASE_URL)  # 访问首页以建立会话
    login_data, token = init_login(session, args.username, args.password, args.skip_login)
    if not login_data:
        return
    
    login_state.token = token
    login_state.campus = login_data.get("student", {}).get("campus")

    # 6. 选择轮次
    batch, course_type = choose_elective_batch(login_data)
    if not batch:
        return
    batch_id = batch['code']
    login_state.batch_id = batch_id

    # 7. 运行课程选择和抢课流程
    run_course_selection(
        session=session,
        token=token,
        batch_id=batch_id,
        campus=login_state.campus,
        username=args.username,
        use_vpn=use_vpn,
        course_type=course_type
    )


if __name__ == "__main__":
    main()