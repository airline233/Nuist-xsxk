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
COURSE_TYPE_FANKC = "FANKC"  # 泛选课/跨年级课程
COURSE_TYPE_TYKC = "TYKC"    # 体育课
COURSE_TYPE_XGKC = "XGKC"    # 通识选修课
COURSE_TYPE_FAWKC = "FAWKC"  # 跨专业课程
COURSE_TYPE_CXKC = "CXKC"    # 重修课程
COURSE_TYPE_TJKC = "TJKC"    # 本专业推荐课程

# 课程类型显示名称映射
COURSE_TYPE_NAMES = {
    COURSE_TYPE_FANKC: "跨年级课程",
    COURSE_TYPE_TYKC: "体育课",
    COURSE_TYPE_XGKC: "通识选修课",
    COURSE_TYPE_FAWKC: "跨专业课程",
    COURSE_TYPE_CXKC: "重修课程",
    COURSE_TYPE_TJKC: "本专业推荐课程",
}

# API 端点
BASE_URL = "https://client.vpn.nuist.edu.cn/https/webvpn3315a96df5a2811a49489fcebfe8b135dece10c6255d04cc36c652f60ee89b3a/xsxk"
# BASE_URL = "http://xsxk.nuist.edu.cn/xsxk"
URL_CAPTCHA = f"{BASE_URL}/auth/captcha?enlink-vpn"
URL_LOGIN = f"{BASE_URL}/auth/login?enlink-vpn"
URL_LIST_CLASSES = f"{BASE_URL}/elective/clazz/list?enlink-vpn"
URL_ADD_CLASS = f"{BASE_URL}/elective/clazz/add?enlink-vpn"
URL_SWITCH_BATCH = f"{BASE_URL}/elective/user?enlink-vpn"
URL_GET_USER_INFO = f"{BASE_URL}/elective/user?enlink-vpn"
URL_VOLUNTEER_LIST = f"{BASE_URL}/volunteer/list/choose?enlink-vpn"  # 获取可用志愿等级
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


def guess_course_types_from_batch_name(batch_name):
    """根据轮次名称推测可能的课程类型
    
    Returns:
        list: 推荐的课程类型列表
    """
    types = []
    name_lower = batch_name.lower()
    
    if "体育" in batch_name:
        types.append(COURSE_TYPE_TYKC)
    if "通识" in batch_name or "选修" in batch_name or "xgkc" in name_lower:
        types.append(COURSE_TYPE_XGKC)
    if "跨专业" in batch_name or "fawkc" in name_lower:
        types.append(COURSE_TYPE_FAWKC)
    if "重修" in batch_name or "cxkc" in name_lower:
        types.append(COURSE_TYPE_CXKC)
    if "推荐" in batch_name or "tjkc" in name_lower:
        types.append(COURSE_TYPE_TJKC)
    
    # 如果没有匹配到特定类型，默认添加泛选课和通识选修课
    if not types:
        types = [COURSE_TYPE_FANKC, COURSE_TYPE_XGKC]
    
    # 泛选课作为通用选项，如果不在列表中则添加
    if COURSE_TYPE_FANKC not in types and COURSE_TYPE_TYKC not in types:
        types.append(COURSE_TYPE_FANKC)
    
    return types


def choose_elective_batch(student_data):
    """让用户选择一个可选的选课轮次
    
    Returns:
        tuple: (batch, course_type, is_volunteer) 或 (None, None, False)
    """
    print("\n--- 请选择一个选课轮次 ---")
    batches = student_data.get("student", {}).get("electiveBatchList", [])
    
    # 筛选出 canSelect 为 "1" 的可选轮次
    available_batches = [b for b in batches if b.get("canSelect") == "1"]
    
    if not available_batches:
        print("未找到当前可用的选课轮次。")
        return None, None, False

    for i, batch in enumerate(available_batches):
        batch_name = batch['name']
        type_code = batch.get('typeCode', '02')
        type_name = batch.get('typeName', '正选')
        
        # 标注预选/正选
        mode_tag = f"[{type_name}]"
        
        # 推测课程类型
        guessed_types = guess_course_types_from_batch_name(batch_name)
        type_hints = "/".join([COURSE_TYPE_NAMES.get(t, t) for t in guessed_types[:2]])
        
        print(f"  [{i+1}] {mode_tag} {batch_name}")
        print(f"      时间: {batch['beginTime']} - {batch['endTime']}")
        print(f"      推测类型: {type_hints}")
    
    # 选择轮次
    selected_batch = None
    while True:
        try:
            choice = int(input("\n请输入数字序号选择轮次: "))
            if 1 <= choice <= len(available_batches):
                selected_batch = available_batches[choice-1]
                break
            else:
                print("无效的输入，请输入列表中的数字。")
        except ValueError:
            print("请输入一个有效的数字。")
    
    # 判断是否为预选（志愿）轮次
    is_volunteer = selected_batch.get('typeCode') == '01'
    if is_volunteer:
        print(f"\n[!] 当前为预选轮次，将使用志愿模式选课")
    
    # 选择课程类型
    print("\n--- 请选择课程类型 ---")
    all_course_types = [
        (COURSE_TYPE_XGKC, "通识选修课"),
        (COURSE_TYPE_FANKC, "跨年级课程（泛选课）"),
        (COURSE_TYPE_TYKC, "体育课"),
        (COURSE_TYPE_FAWKC, "跨专业课程"),
        (COURSE_TYPE_CXKC, "重修课程"),
        (COURSE_TYPE_TJKC, "本专业推荐课程"),
    ]
    
    # 根据轮次名称推荐课程类型
    guessed = guess_course_types_from_batch_name(selected_batch['name'])
    
    for i, (code, name) in enumerate(all_course_types):
        recommend = " ★推荐" if code in guessed else ""
        print(f"  [{i+1}] {name} ({code}){recommend}")
    
    while True:
        try:
            type_choice = int(input("请输入数字序号选择课程类型: "))
            if 1 <= type_choice <= len(all_course_types):
                course_type = all_course_types[type_choice-1][0]
                print(f"\n已选择: {all_course_types[type_choice-1][1]}")
                return selected_batch, course_type, is_volunteer
            else:
                print("无效的输入，请输入列表中的数字。")
        except ValueError:
            print("请输入一个有效的数字。")
            
def choose_class(session, token, batch_id, campus, course_type=COURSE_TYPE_FANKC, is_volunteer=False):
    """获取课程列表并让用户选择
    
    Args:
        course_type: 课程类型
        is_volunteer: 是否为志愿模式（预选轮次）
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
        
        # 解析课程列表
        # 有些课程类型（如XGKC）直接返回课程列表，不嵌套在tcList中
        # 有些（如TYKC、FANKC）可能嵌套在tcList中
        all_classes = []
        courses = data.get("data", {}).get("rows", [])
        for course in courses:
            if "tcList" in course and course["tcList"]:
                # 嵌套结构：展开 tcList
                all_classes.extend(course.get("tcList", []))
            else:
                # 直接结构：课程本身就是一个班级
                all_classes.append(course)
        
        if not all_classes:
            print("在此轮次下未找到可选的课程。")
            return None, None

        # 判断课程类型
        is_pe_course = course_type == COURSE_TYPE_TYKC
        is_xgkc = course_type == COURSE_TYPE_XGKC
        
        # 根据课程数量动态决定列数 (体育课/通识课信息多，用较少列数)
        total = len(all_classes)
        if is_pe_course or is_xgkc:
            # 体育课/通识课信息较多，使用较少列数
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
        table = Table(title="请选择要抢的课程", show_header=False, 
                      box=None, padding=(0, 1), collapse_padding=True)
        
        # 添加列
        for _ in range(COLS):
            table.add_column(justify="left", no_wrap=False, overflow="fold")
        
        # 构建单元格内容
        def build_cell(idx, clazz):
            """构建单个课程的显示内容"""
            text = Text()
            text.append(f"[{idx}] ", style="bold cyan")
            
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
            elif is_xgkc:
                # 通识选修课：显示课程名和通识类别
                text.append(f"{clazz['KCM']}\n", style="bold")
                # 显示通识类别（如有）
                xgxklb = clazz.get('XGXKLB', '')
                if xgxklb:
                    text.append(f"[{xgxklb}]\n", style="magenta")
                text.append(f"{clazz['SKJS']} ", style="green")
                text.append(f"[{clazz['YXRS']}/{clazz['KRL']}]\n", style="yellow")
            else:
                # 其他课程：原有显示逻辑
                text.append(f"{clazz['KCM']}\n", style="bold")
                text.append(f"{clazz['SKJS']} ", style="green")
                text.append(f"[{clazz['YXRS']}/{clazz['KRL']}]\n", style="yellow")
            
            # 时间地点按逗号分隔
            place_parts = [p.strip() for p in clazz['teachingPlace'].split(',') if p.strip()]
            for i, part in enumerate(place_parts):
                text.append(part, style="dim")
                if i < len(place_parts) - 1:
                    text.append("\n")
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
        console.print(f"共 [bold]{total}[/bold] 门课程\n")
        
        # 志愿模式提示
        if is_volunteer:
            console.print("[bold yellow]当前为预选（志愿）模式：按输入顺序依次报志愿，满员自动换下一个[/bold yellow]\n")
        
        while True:
            prompt = "请输入课程序号（多个用逗号或空格分隔，按顺序为志愿优先级）: " if is_volunteer else "请输入课程序号（多个用逗号或空格分隔）: "
            choice_str = input(prompt).strip()
            if not choice_str:
                print("请输入至少一个课程序号。")
                continue
            
            try:
                # 支持逗号或空格分隔
                choices = [int(x.strip()) for x in re.split(r'[,\s]+', choice_str) if x.strip()]
                
                if not choices:
                    print("请输入至少一个课程序号。")
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
                    return selected_classes, all_classes
                else:
                    print("未选择任何有效课程，请重新输入。")
                    
            except ValueError:
                print("输入格式错误，请输入数字序号。")

    except Exception as e:
        print(f"获取课程列表时发生错误: {e}")
        return None, None


def get_volunteer_grades(session, token, batch_id, clazz_id, course_type):
    """获取可用的志愿等级列表
    
    Returns:
        list: 可用志愿等级列表，如 [{'grade': '1', ...}, ...]
    """
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Batchid": batch_id,
        "Content-Type": "application/json;charset=UTF-8"
    }
    
    body = {
        "clazzType": course_type,
        "clazzId": clazz_id
    }
    
    try:
        response = session.post(URL_VOLUNTEER_LIST, headers=headers, data=json.dumps(body), timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") == 200:
            return data.get("data", [])
        else:
            print(f"获取志愿等级失败: {data.get('msg')}")
            return []
    except Exception as e:
        print(f"获取志愿等级时发生错误: {e}")
        return []

def start_grabbing(session, token, batch_id, selected_class, backup_classes=None, course_type=COURSE_TYPE_FANKC, is_volunteer=False, max_volunteers=8):
    """开始循环抢课，支持备选课程切换
    
    Args:
        course_type: 课程类型
        is_volunteer: 是否为志愿模式
        max_volunteers: 志愿模式下最大志愿数量
    """
    if backup_classes is None:
        backup_classes = []
    
    # 构建课程队列：主课程 + 备选课程
    course_queue = [selected_class] + backup_classes
    
    # 获取课程显示名称
    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz['KCM'])}({clazz.get('classificationName', '')})"
        elif course_type == COURSE_TYPE_XGKC:
            xgxklb = clazz.get('XGXKLB', '')
            return f"{clazz['KCM']}" + (f"[{xgxklb}]" if xgxklb else "")
        return clazz['KCM']

    print("\n" + "="*50)
    mode_str = "[志愿模式]" if is_volunteer else "[正选模式]"
    print(f"{mode_str} 准备抢课")
    print(f"候选课程数量: {len(course_queue)}")
    if is_volunteer:
        print(f"最大志愿数: {max_volunteers}")
        print("逻辑: 按顺序填报志愿，课程满则跳过用下一个")
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
    
    def build_body(clazz, vol_grade=None):
        body = {
            "clazzType": course_type,
            "clazzId": clazz['JXBID'],
            "secretVal": clazz['secretVal']
        }
        # 志愿模式需要添加志愿等级
        if is_volunteer and vol_grade:
            body["chooseVolunteer"] = str(vol_grade)
        return urlencode(body, quote_via=quote_plus)

    headers = build_headers(current_token)
    
    # 连续JSON解析失败计数
    json_fail_count = 0
    MAX_JSON_FAIL = 3

    # ========== 志愿模式：按顺序填报多个志愿 ==========
    if is_volunteer:
        current_volunteer = 1  # 当前志愿等级（从1开始）
        current_index = 0      # 当前课程索引
        success_count = 0      # 成功填报的志愿数
        results = []           # 记录结果 [(志愿等级, 课程名, 成功/失败)]
        
        print(f"开始填报志愿...\n")
        
        try:
            while current_volunteer <= max_volunteers and current_index < len(course_queue):
                current_class = course_queue[current_index]
                encoded_body = build_body(current_class, current_volunteer)
                
                try:
                    response = session.post(URL_ADD_CLASS, headers=headers, data=encoded_body, timeout=5)
                    
                    try:
                        result = response.json()
                        json_fail_count = 0
                    except json.JSONDecodeError as e:
                        json_fail_count += 1
                        print(f"\n[!] JSON解析失败 ({json_fail_count}/{MAX_JSON_FAIL})")
                        if json_fail_count >= MAX_JSON_FAIL:
                            success, new_token = handle_relogin(response)
                            if success and new_token:
                                current_token = new_token
                                headers = build_headers(current_token)
                                json_fail_count = 0
                        time.sleep(0.5)
                        continue
                    
                    code = result.get('code')
                    msg = result.get('msg', '没有消息')
                    
                    course_name = get_display_name(current_class)
                    main_logger.info(f"[志愿{current_volunteer}] [{course_name}] {code}: {msg}")
                    
                    if code == 200:
                        # 成功：记录结果，志愿等级+1，课程索引+1
                        print(f"[✓] 志愿{current_volunteer}: {course_name} - {current_class['SKJS']} -> 成功")
                        results.append((current_volunteer, course_name, "成功"))
                        success_count += 1
                        current_volunteer += 1
                        current_index += 1
                        time.sleep(0.3)
                    elif msg.find("满") != -1:
                        # 课程满了：跳过这个课程，志愿等级不变
                        print(f"[×] 志愿{current_volunteer}: {course_name} - 已满，跳过")
                        results.append((current_volunteer, course_name, "已满-跳过"))
                        current_index += 1
                        time.sleep(0.3)
                    elif msg.find("志愿已选") != -1 or msg.find("本轮次该类或该门课程志愿已选满") != -1:
                        # 这个志愿等级已用完：志愿等级+1，课程不变（重试当前课程）
                        print(f"[!] 志愿{current_volunteer} 已被其他课程占用，尝试下一个志愿等级")
                        current_volunteer += 1
                        time.sleep(0.3)
                    elif code == 403:
                        # 频率限制，等待后重试
                        time.sleep(0.5)
                        continue
                    elif msg.find("暂未开始") != -1 or msg.find("未开始") != -1 or msg.find("未开放") != -1:
                        # 选课还没开始：不跳过，等待后继续重试当前课程
                        print(f"[⏳] 志愿{current_volunteer}: {course_name} - {msg}，等待中...")
                        time.sleep(0.5)
                        continue
                    else:
                        # 其他错误：记录并跳过这个课程
                        print(f"[?] 志愿{current_volunteer}: {course_name} - {msg}")
                        results.append((current_volunteer, course_name, f"失败:{msg[:20]}"))
                        current_index += 1
                        time.sleep(0.3)
                        
                except requests.exceptions.RequestException as e:
                    print(f"请求失败: {e}")
                    time.sleep(1)
            
            # 打印汇总结果
            print("\n" + "="*50)
            print("志愿填报完成！汇总：")
            print("="*50)
            for vol, name, status in results:
                print(f"  志愿{vol}: {name} -> {status}")
            print(f"\n成功填报: {success_count}/{max_volunteers} 个志愿")
            if current_index >= len(course_queue) and current_volunteer <= max_volunteers:
                print(f"[!] 候选课程已用完，还有 {max_volunteers - current_volunteer + 1} 个志愿位未填")
            print("="*50)
            
        except KeyboardInterrupt:
            print("\n\n脚本已由用户手动停止。")
            print(f"已填报 {success_count} 个志愿")
            input("按回车键退出...")
            sys.exit(0)
    
    # ========== 正选模式：循环抢单个课程 ==========
    else:
        current_index = 0
        current_class = course_queue[current_index]
        encoded_body = build_body(current_class)
        
        try:
            while True:
                try:
                    response = session.post(URL_ADD_CLASS, headers=headers, data=encoded_body, timeout=5)
                    
                    try:
                        result = response.json()
                        json_fail_count = 0
                    except json.JSONDecodeError as e:
                        json_fail_count += 1
                        print(f"\n[!] JSON解析失败 ({json_fail_count}/{MAX_JSON_FAIL}): {e}")
                        main_logger.warning(f"JSON解析失败: {e}, 响应内容: {response.text[:200]}")
                        
                        if json_fail_count >= MAX_JSON_FAIL:
                            print(f"\n[!] 连续{MAX_JSON_FAIL}次JSON解析失败，触发重新登录...")
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
                        
                        if current_index < len(course_queue) - 1:
                            current_index += 1
                            current_class = course_queue[current_index]
                            encoded_body = build_body(current_class)
                            print(f"[->] 切换到备选课程 {current_index}: {get_display_name(current_class)} - {current_class['SKJS']}")
                            main_logger.info(f"切换到备选课程: {get_display_name(current_class)}")
                        else:
                            print("[!] 所有课程（含备选）均已满，继续尝试最后一个课程...")

                    if code != 403:
                        dt = datetime.datetime.now()
                        course_name = get_display_name(current_class)[:10]
                        print(f"[{course_name}] 状态: {code}, 信息: {msg} {dt}", end='\r')
                    
                    if code not in [500, 403, 0]:
                        print(f"\n收到关键响应: {json.dumps(result, ensure_ascii=False)}")
                    
                    if code == 200:
                        print(f"\n[✓] 选课成功！课程: {get_display_name(current_class)} - {current_class['SKJS']}")
                        main_logger.info(f"选课成功: {get_display_name(current_class)}")
                        break
                    
                except requests.exceptions.RequestException as e:
                    print(f"请求失败: {e}", end='\r')

                time.sleep(0.3)

        except KeyboardInterrupt:
            print("\n\n脚本已由用户手动停止。")
            input("按回车键退出...")
            sys.exit(0)


def confirm_course_selection(selected_classes, course_type=COURSE_TYPE_FANKC, is_volunteer=False, max_volunteers=8):
    """显示课程选择信息并请求用户确认
    
    Args:
        selected_classes: 选择的课程列表（第一个为主选，其余为备选）
        course_type: 课程类型
        is_volunteer: 是否为志愿模式
        max_volunteers: 最大志愿数
        
    Returns:
        tuple: (confirmed: bool, selected_class, backup_classes)
    """
    selected_class = selected_classes[0]
    backup_classes = selected_classes[1:] if len(selected_classes) > 1 else []
    
    # 获取课程显示名称
    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz['KCM'])}({clazz.get('classificationName', '')})"
        elif course_type == COURSE_TYPE_XGKC:
            xgxklb = clazz.get('XGXKLB', '')
            return f"{clazz['KCM']}" + (f" [{xgxklb}]" if xgxklb else "")
        return clazz['KCM']
    
    mode_str = f"[志愿模式 - 最多{max_volunteers}个志愿]" if is_volunteer else "[正选模式]"
    print(f"\n{mode_str}")
    print(f"已选择 {len(selected_classes)} 个候选课程:")
    
    for i, clazz in enumerate(selected_classes):
        prefix = f"  第{i+1}志愿候选" if is_volunteer else (f"  主选" if i == 0 else f"  备选{i}")
        print(f"{prefix}: {get_display_name(clazz)} - {clazz['SKJS']} [{clazz['YXRS']}/{clazz['KRL']}]")
        print(f"         {clazz['teachingPlace']}")
    
    if is_volunteer:
        if len(selected_classes) < max_volunteers:
            print(f"\n[提示] 你选择了 {len(selected_classes)} 个课程，但最多可填 {max_volunteers} 个志愿")
        print(f"[提示] 志愿模式下，课程满了会自动跳过，用下一个候选填当前志愿位")
    
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


def run_course_selection(session, token, batch_id, campus, username, use_vpn, course_type=COURSE_TYPE_FANKC, is_volunteer=False):
    """运行课程选择和抢课流程
    
    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        campus: 校区
        username: 学号（用于WebSocket）
        use_vpn: 是否使用VPN
        course_type: 课程类型
        is_volunteer: 是否为志愿模式（预选轮次）
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
        # 选择课程（支持一次性选择多个，第一个为主选，后面为备选）
        result = choose_class(session, token, batch_id, campus, course_type, is_volunteer)
        if result is None or result[0] is None:
            return
        selected_classes, all_classes = result
        
        # 志愿模式：获取最大志愿数
        max_volunteers = 8  # 默认值
        if is_volunteer and selected_classes:
            print("\n正在获取可用志愿数量...")
            # 用第一个选择的课程来获取志愿等级列表
            volunteer_grades = get_volunteer_grades(session, token, batch_id, selected_classes[0]['JXBID'], course_type)
            if volunteer_grades:
                max_volunteers = len(volunteer_grades)
                print(f"[✓] 本轮次最大志愿数: {max_volunteers}")
            else:
                print(f"[!] 未能获取志愿数量，使用默认值: {max_volunteers}")
        
        # 确认课程选择
        confirmed, selected_class, backup_classes = confirm_course_selection(selected_classes, course_type, is_volunteer, max_volunteers)
        if not confirmed:
            print("操作已取消。")
            return
        
        # 停止 HTTP 心跳（确认提交后不再需要）
        http_heartbeat.stop()
        
        # 开始抢课
        start_grabbing(session, token, batch_id, selected_class, backup_classes, course_type, is_volunteer, max_volunteers)
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
    batch, course_type, is_volunteer = choose_elective_batch(login_data)
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
        course_type=course_type,
        is_volunteer=is_volunteer
    )


if __name__ == "__main__":
    main()