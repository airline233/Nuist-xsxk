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
sys.path.append(os.path.abspath('..'))
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


def _is_auth_failure(code, msg=""):
    """判断响应是否表示鉴权/登录态失效。

    仅 401 直接认定；403 结合文案（避免限频 403 误重登）。
    其它业务 code 不再靠宽泛关键词猜鉴权。
    """
    msg = str(msg or "")
    if code == 401:
        return True
    if code == 403:
        keywords = ("登录", "token", "Token", "授权", "认证", "未登录", "过期", "失效", "重新登录")
        return any(k in msg for k in keywords)
    return False


def _get_live_token(fallback=None):
    """优先使用 login_state 中最新 token，避免主循环用过期快照"""
    return login_state.token or fallback


def _is_full_msg(msg):
    """判断消息是否表示课程已满（避免过宽匹配「满」字）

    不使用裸「已满」，以免误伤「已满足先修条件」等文案。
    """
    msg = str(msg or "")
    keywords = ("课容量已满", "人数已满", "容量已满", "名额已满", "选课人数已满", "课容量已达上限")
    return any(k in msg for k in keywords)


def _is_already_selected_msg(msg):
    """判断消息是否表示目标课已选/重复选"""
    msg = str(msg or "")
    keywords = (
        "已选该", "已经选过", "已经选择", "已选择该", "您已选", "你已选",
        "重复选", "不可重复", "不能重复", "请勿重复", "重复提交",
        "已在选课结果", "已选中该",
    )
    return any(k in msg for k in keywords)


def _extract_ws_clazz_id(data):
    """从 WebSocket 失败/成功 data 中尽量取出教学班 ID"""
    if not isinstance(data, dict):
        return ""
    cid = data.get("clazzId") or data.get("teachingClassID") or data.get("JXBID") or ""
    if not cid:
        for course in data.get("xkjgList") or []:
            if not isinstance(course, dict):
                continue
            cid = course.get("teachingClassID") or course.get("JXBID") or ""
            if cid:
                break
    return str(cid) if cid else ""


def _extract_student_id(login_data):
    """从 login_data.student 提取学号（skip-login 等场景）"""
    student = (login_data or {}).get("student") or {}
    for key in ("XH", "xh", "studentId", "studentID", "loginName", "username", "USERID"):
        val = student.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


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
    if not login_state.username or not login_state.password:
        print("\n[✗] 无学号/密码，无法重新登录（--skip-login 时需同时提供 -u/-p 才能自动重登）")
        main_logger.error("无账密，跳过选课系统重登")
        return False
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


_RELOGIN_LOCK = threading.Lock()
_RELOGIN_DEBOUNCE_SECONDS = 5.0
_relogin_last_finished_at = 0.0
_relogin_last_success = False


def _finish_relogin(success):
    """记录本次重登结果，供并发调用在防抖窗口内复用。"""
    global _relogin_last_finished_at, _relogin_last_success
    _relogin_last_finished_at = time.monotonic()
    _relogin_last_success = bool(success and login_state.token)
    return _relogin_last_success, login_state.token if _relogin_last_success else None


def handle_relogin(response=None):
    """串行化重登，并在短时间内复用最近一次的结果。"""
    global _relogin_last_finished_at, _relogin_last_success

    # HTTP 心跳与前台请求共用 session；只允许一个线程执行实际重登。
    with _RELOGIN_LOCK:
        now = time.monotonic()
        elapsed = now - _relogin_last_finished_at
        if _relogin_last_finished_at and elapsed < _RELOGIN_DEBOUNCE_SECONDS:
            if _relogin_last_success and login_state.token:
                main_logger.info(
                    f"重登防抖：复用 {elapsed:.1f}s 前的成功登录结果"
                )
                return True, login_state.token
            main_logger.warning(
                f"重登防抖：距上次失败仅 {elapsed:.1f}s，跳过重复登录"
            )
            return False, None

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
                return _finish_relogin(False)
            if not relogin_system():
                return _finish_relogin(False)
        else:
            # 没有302，直接尝试重登选课系统
            print("[!] 尝试重新登录选课系统...")
            try:
                if not relogin_system():
                    # 验证码获取失败等情况，尝试重登VPN
                    print("[!] 选课系统登录失败，尝试重新登录VPN...")
                    if not relogin_vpn():
                        return _finish_relogin(False)
                    if not relogin_system():
                        return _finish_relogin(False)
            except Exception as e:
                # 捕获验证码获取失败等异常
                print(f"[!] 登录异常: {e}，尝试重新登录VPN...")
                main_logger.error(f"登录异常: {e}")
                if not relogin_vpn():
                    return _finish_relogin(False)
                if not relogin_system():
                    return _finish_relogin(False)

        return _finish_relogin(True)


# HTTP 心跳管理类（用于维持登录态）
class HttpHeartbeat:
    """HTTP 心跳管理，每隔30秒请求课程列表保持登录态"""

    def __init__(self, session, token, batch_id, campus, interval=30, on_relogin=None):
        self.session = session
        self.token = token
        self.batch_id = batch_id
        self.campus = campus
        self.interval = interval
        self.on_relogin = on_relogin  # 重登成功后回调，用于同步 WS cookie 等
        self.running = False
        self.thread = None

    def _notify_relogin(self, new_token):
        """重登成功后更新 token 并触发 on_relogin（如同步 WS cookies）"""
        self.token = new_token
        login_state.token = new_token
        if self.on_relogin:
            try:
                self.on_relogin(new_token)
            except Exception as e:
                heartbeat_logger.warning(f"[HTTP心跳] on_relogin 回调异常: {e}")

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
                    msg = str(data.get("msg", ""))
                    if code == 200:
                        heartbeat_logger.debug("[HTTP心跳] 成功")
                    elif _is_auth_failure(code, msg):
                        print(f"\n[HTTP心跳] 鉴权失败(code={code})，触发重登...")
                        heartbeat_logger.warning(f"[HTTP心跳] 鉴权失败: code={code}, msg={msg}")
                        success, new_token = handle_relogin(response)
                        if success and new_token:
                            self._notify_relogin(new_token)
                            print("[HTTP心跳] 重新登录成功")
                            heartbeat_logger.info("[HTTP心跳] 重新登录成功")
                        else:
                            print("[HTTP心跳] 重新登录失败")
                            heartbeat_logger.error("[HTTP心跳] 重新登录失败")
                    else:
                        heartbeat_logger.warning(f"[HTTP心跳] 响应code={code}, msg={msg}")
                except json.JSONDecodeError as e:
                    print(f"\n[HTTP心跳] JSON解析失败，触发重登流程: {e}")
                    heartbeat_logger.warning(f"[HTTP心跳] JSON解析失败: {e}, 响应: {response.text[:200]}")

                    # 立即执行重登流程
                    success, new_token = handle_relogin(response)
                    if success and new_token:
                        self._notify_relogin(new_token)
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
    """WebSocket 心跳管理，每隔5秒发送 'hi' 保持连接，并监听选课成功/失败消息"""

    def __init__(self, student_id, cookies=None, use_vpn=False, on_course_success=None, on_course_fail=None):
        self.student_id = student_id
        self.cookies = cookies
        self.use_vpn = use_vpn
        self.ws = None
        self.running = False
        self.thread = None
        self.heartbeat_thread = None
        self.on_course_success = on_course_success
        self.on_course_fail = on_course_fail
        self.success_messages = []
        self._lock = threading.Lock()

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
        try:
            data = json.loads(message)
            code = data.get("code")
            msg = data.get("msg", "")
            result_data = data.get("data")

            # 心跳包
            if code == 200 and result_data == "heart":
                heartbeat_logger.debug(f"[WebSocket] 心跳包响应: {data}")
                return

            heartbeat_logger.info(f"[WebSocket] 解析消息: {data}")

            # 选课成功
            if code == 200 and "选课成功" in str(msg):
                clazz_id = ""
                course_name = ""
                if isinstance(result_data, dict):
                    clazz_id = result_data.get("clazzId", "")
                    for course in result_data.get("xkjgList", []):
                        if course.get("teachingClassID") == clazz_id:
                            course_name = course.get("KCM", "")
                            break
                if not course_name:
                    course_name = msg.split(":", 1)[1] if ":" in str(msg) else str(msg)

                print(f"\n[WebSocket] 🎉 选课成功: {course_name}")
                main_logger.info(f"[WebSocket] 选课成功: {course_name}, clazzId={clazz_id}")
                with self._lock:
                    self.success_messages.append({
                        "clazz_id": clazz_id,
                        "course_name": course_name,
                        "msg": msg,
                        "data": result_data,
                        "timestamp": time.time()
                    })
                if self.on_course_success:
                    try:
                        self.on_course_success(clazz_id, course_name, msg, result_data)
                    except Exception as e:
                        heartbeat_logger.error(f"[WebSocket] 选课成功回调异常: {e}")
                return

            # 选课失败（课容量已满等）
            if code == 500:
                print(f"\n[WebSocket] ⚠️ 选课失败: {msg}")
                main_logger.warning(f"[WebSocket] 选课失败: {msg}")
                if self.on_course_fail:
                    try:
                        self.on_course_fail(code, msg, result_data)
                    except Exception as e:
                        heartbeat_logger.error(f"[WebSocket] 选课失败回调异常: {e}")
                return

            if code is not None and code != 200:
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
        consecutive_failures = 0
        max_failures = 3
        while self.running and self.ws:
            try:
                if self.ws.sock and self.ws.sock.connected:
                    self.ws.send("hi")
                    heartbeat_logger.debug("[WebSocket] 发送心跳: hi")
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    heartbeat_logger.warning(f"[WebSocket] 连接异常 ({consecutive_failures}/{max_failures})")
                time.sleep(5)
            except Exception as e:
                consecutive_failures += 1
                heartbeat_logger.error(f"[WebSocket] 心跳发送失败 ({consecutive_failures}/{max_failures}): {e}")
                if consecutive_failures >= max_failures:
                    break
                time.sleep(2)

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

    def update_cookies(self, cookies):
        """重登后更新 cookie，并触发重连"""
        self.cookies = cookies or {}
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def set_success_callback(self, callback):
        self.on_course_success = callback

    def set_fail_callback(self, callback):
        self.on_course_fail = callback

    def check_success(self, clazz_id=None):
        """查询成功消息。传入 clazz_id 时只匹配非空且相等的 id（str 比较）。"""
        with self._lock:
            if clazz_id is not None:
                cid = str(clazz_id)
                return [
                    m for m in self.success_messages
                    if m.get("clazz_id") and str(m["clazz_id"]) == cid
                ]
            return list(self.success_messages)

    def clear_success_messages(self):
        with self._lock:
            self.success_messages.clear()


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

    鉴权失败或 JSON 解析失败时尝试重登并用新 token 再试一次。

    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        course_to_drop: 要退的课程信息（含 secretVal）
        course_type: 课程类型

    Returns:
        bool: 是否退课成功
    """
    def _do_drop(tk):
        headers = {
            **COMMON_HEADERS,
            "Authorization": tk,
            "batchId": batch_id,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        body = {
            "clazzType": course_type,
            "clazzId": course_to_drop['JXBID'],
            "secretVal": course_to_drop['secretVal']
        }
        response = session.post(
            URL_DEL_CLASS, headers=headers,
            data=urlencode(body, quote_via=quote_plus), timeout=10
        )
        try:
            result = response.json()
        except json.JSONDecodeError:
            return "auth_or_parse", response, None
        code = result.get('code')
        msg = result.get('msg', '无消息')
        if code == 200:
            return "ok", response, result
        if _is_auth_failure(code, msg):
            return "auth", response, result
        return "fail", response, result

    try:
        status, response, result = _do_drop(token)
        if status == "ok":
            msg = (result or {}).get('msg', '无消息')
            print(f"[✓] 退课成功: {msg}")
            main_logger.info(f"退课成功: {course_to_drop.get('KCM', '未知')} - {msg}")
            return True

        if status in ("auth", "auth_or_parse"):
            print("[!] 退课鉴权/解析失败，尝试重登后重试...")
            main_logger.warning(f"退课鉴权失败，尝试重登: status={status}")
            success, new_token = handle_relogin(response)
            if success and new_token:
                login_state.token = new_token
                status2, _, result2 = _do_drop(new_token)
                if status2 == "ok":
                    msg = (result2 or {}).get('msg', '无消息')
                    print(f"[✓] 退课成功(重登后): {msg}")
                    main_logger.info(f"退课成功(重登后): {course_to_drop.get('KCM', '未知')} - {msg}")
                    return True
                msg2 = (result2 or {}).get('msg', '无消息') if result2 else '解析失败'
                print(f"[✗] 退课失败(重登后仍失败): {msg2}")
                main_logger.warning(f"退课失败(重登后): {msg2}")
                return False
            print("[✗] 退课重登失败")
            main_logger.error("退课重登失败")
            return False

        code = (result or {}).get('code')
        msg = (result or {}).get('msg', '无消息')
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

        target_str = str(target_class_id)
        courses = data.get("data", {}).get("rows", [])
        for course in courses:
            for clazz in course.get("tcList", []):
                if str(clazz.get('JXBID', '')) == target_str:
                    return int(clazz['YXRS']), int(clazz['KRL']), clazz

        return None, None, None
    except Exception as e:
        print(f"[!] 查询课容量异常: {e}")
        return None, None, None


def fetch_selected_classes(session, token, batch_id, campus, course_type=COURSE_TYPE_FANKC,
                           exclude_jxbid=None, page_size=200):
    """拉取当前轮次下已选教学班（SFYX==1），不依赖目标课列表页。

    通过 list 接口按 SFYX=1 过滤；若服务端忽略该过滤，再本地二次筛。
    可排除目标课自身。

    Returns:
        list: 已选教学班字典列表；失败返回 []
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
        "pageSize": page_size,
        "orderBy": "",
        "campus": campus,
        "SFYX": "1",  # 优先只要已选
    }
    try:
        response = session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=15)
        data = response.json()
        if data.get("code") != 200:
            print(f"[!] 拉取已选课失败: {data.get('msg')}")
            main_logger.warning(f"拉取已选课失败: {data}")
            return []

        selected = []
        seen = set()
        courses = data.get("data", {}).get("rows", []) or []
        for course in courses:
            parent_name = course.get("KCM", "")
            for clazz in course.get("tcList", []) or []:
                jxbid = clazz.get("JXBID")
                if not jxbid or jxbid in seen:
                    continue
                # 服务端若忽略 SFYX=1，本地再筛
                if str(clazz.get("SFYX", "0")) != "1":
                    continue
                if exclude_jxbid and jxbid == exclude_jxbid:
                    continue
                # 便于展示：挂上父课程名
                if not clazz.get("KCM") and parent_name:
                    clazz = dict(clazz)
                    clazz["KCM"] = parent_name
                    clazz["_parent_course"] = parent_name
                selected.append(clazz)
                seen.add(jxbid)

        # 若 SFYX=1 请求结果为空，回退拉全量再筛（兼容服务端忽略过滤）
        if not selected:
            body_all = dict(body)
            body_all["SFYX"] = "2"
            response2 = session.post(
                URL_LIST_CLASSES, headers=headers, data=json.dumps(body_all), timeout=15
            )
            data2 = response2.json()
            if data2.get("code") == 200:
                for course in data2.get("data", {}).get("rows", []) or []:
                    parent_name = course.get("KCM", "")
                    for clazz in course.get("tcList", []) or []:
                        jxbid = clazz.get("JXBID")
                        if not jxbid or jxbid in seen:
                            continue
                        if str(clazz.get("SFYX", "0")) != "1":
                            continue
                        if exclude_jxbid and jxbid == exclude_jxbid:
                            continue
                        if not clazz.get("KCM") and parent_name:
                            clazz = dict(clazz)
                            clazz["KCM"] = parent_name
                            clazz["_parent_course"] = parent_name
                        selected.append(clazz)
                        seen.add(jxbid)

        main_logger.info(f"已选课拉取完成: {len(selected)} 门")
        return selected
    except Exception as e:
        print(f"[!] 拉取已选课异常: {e}")
        main_logger.error(f"拉取已选课异常: {e}")
        return []


def _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
    """列表复核：目标教学班是否已选上。

    优先 get_course_capacity 看 SFYX==1；再 fetch_selected_classes 看是否在已选列表。
    JXBID 一律 str 比较。
    """
    live_token = _get_live_token(token)
    campus = login_state.campus
    tid = str(target_id)
    try:
        _, _, latest = get_course_capacity(
            session, live_token, batch_id, campus, target_id, course_type
        )
        if latest and str(latest.get("SFYX", "0")) == "1":
            main_logger.info(f"列表复核成功(SFYX=1): {tid}")
            return True
    except Exception as e:
        main_logger.warning(f"列表复核 get_course_capacity 异常: {e}")

    try:
        selected = fetch_selected_classes(
            session, live_token, batch_id, campus, course_type
        )
        for clazz in selected or []:
            if str(clazz.get("JXBID", "")) == tid:
                main_logger.info(f"列表复核成功(已选列表): {tid}")
                return True
    except Exception as e:
        main_logger.warning(f"列表复核 fetch_selected_classes 异常: {e}")

    return False


def _try_add_class(session, token, batch_id, select_class, course_type, ws_heartbeat=None, wait_timeout=5.0):
    """提交选课并尽量用 WebSocket / 列表复核确认真实成功。

    Returns:
        tuple: (ok: bool, reason: str)
            ok=True 表示确认选上；False 表示失败/未确认
            reason: success / http_fail / timeout / exception / full / relogin / other_fail
    """
    secret = select_class.get('secretVal')
    if not secret:
        print("[✗] 缺少 secretVal，无法提交选课")
        main_logger.error("选课缺少 secretVal")
        return False, "exception"

    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "batchId": batch_id,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    body = {
        "clazzType": course_type,
        "clazzId": select_class['JXBID'],
        "secretVal": secret
    }

    ws_success_event = threading.Event()
    ws_fail_event = threading.Event()
    ws_fail_reason = {"value": "other_fail"}
    target_id = select_class['JXBID']
    target_id_str = str(target_id)

    def on_success(clazz_id, course_name, msg, data):
        # 仅 clazz_id 非空且与目标匹配才认定成功；空 id 忽略
        if clazz_id and str(clazz_id) == target_id_str:
            ws_success_event.set()

    def on_fail(code, msg, data):
        fail_id = _extract_ws_clazz_id(data)
        # 带 id 且不是目标课 → 忽略
        if fail_id and str(fail_id) != target_id_str:
            main_logger.info(f"[WebSocket] 忽略非目标课失败: fail={fail_id}, target={target_id_str}, msg={msg}")
            return
        if _is_full_msg(msg):
            ws_fail_reason["value"] = "full"
        else:
            ws_fail_reason["value"] = "other_fail"
        ws_fail_event.set()

    # A: 先查迟到成功消息，再 clear 并注册回调
    if ws_heartbeat:
        if ws_heartbeat.check_success(target_id_str):
            main_logger.info(f"发现迟到成功消息: {target_id_str}")
            return True, "success"
        ws_heartbeat.clear_success_messages()
        ws_heartbeat.set_success_callback(on_success)
        ws_heartbeat.set_fail_callback(on_fail)

    try:
        response = session.post(
            URL_ADD_CLASS, headers=headers,
            data=urlencode(body, quote_via=quote_plus), timeout=10
        )
        try:
            result = response.json()
        except json.JSONDecodeError:
            # 非 JSON 可能是登录态失效；也可能其实已选上
            success, new_token = handle_relogin(response)
            if success and new_token:
                # 重登后仍做一次复核，防止假失败
                if _confirm_selected_by_list(session, new_token, batch_id, target_id, course_type):
                    return True, "success"
                return False, "relogin"
            if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                return True, "success"
            return False, "exception"

        code = result.get('code')
        msg = str(result.get('msg', '无消息'))
        main_logger.info(f"选课请求响应: code={code}, msg={msg}")

        if _is_auth_failure(code, msg):
            success, new_token = handle_relogin(response)
            if success and new_token:
                if _confirm_selected_by_list(session, new_token, batch_id, target_id, course_type):
                    return True, "success"
                return False, "relogin"
            return False, "http_fail"

        if code != 200:
            # 已选/重复：列表或目标课程的 WS 成功消息必须至少确认一项。
            if _is_already_selected_msg(msg):
                if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                    print(f"[✓] 接口提示已选且列表复核通过: {msg}")
                    return True, "success"

                print(f"[⏳] 接口提示已选，但列表未确认；等待目标课程 WS 确认（最长 {wait_timeout:.0f}s）...")
                deadline = time.time() + wait_timeout
                while time.time() < deadline:
                    if ws_success_event.is_set():
                        print(f"[✓] 接口提示已选且 WS 确认目标教学班: {msg}")
                        return True, "success"
                    if ws_heartbeat:
                        msgs = ws_heartbeat.check_success(target_id_str)
                        if msgs:
                            print(f"[✓] 接口提示已选且 WS 轮询确认目标教学班: {msg}")
                            return True, "success"
                    if ws_fail_event.is_set():
                        break
                    time.sleep(0.1)

                # WS 可能先于列表提交，等待后再复核一次；两者均未确认则不可退出。
                if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                    print(f"[✓] 接口提示已选，延迟列表复核通过: {msg}")
                    return True, "success"
                print(f"[!] 接口提示已选，但 WS/列表均未确认目标教学班，将继续抢课: {msg}")
                main_logger.warning(f"已选文案未获 WS/列表确认: target={target_id_str}, msg={msg}")
                return False, "other_fail"
            if _is_full_msg(msg):
                return False, "full"
            # 其它失败也做一次列表复核（防止假失败 / 冲突文案但其实已选上）
            if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                print(f"[✓] 接口失败但列表复核已选上: {msg}")
                return True, "success"
            return False, "http_fail"

        # code==200 仅表示入队，等 WS 确认
        print(f"[→] 已加入选课队列，等待 WebSocket 确认（最长 {wait_timeout:.0f}s）...")
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            if ws_success_event.is_set():
                return True, "success"
            if ws_fail_event.is_set():
                reason = ws_fail_reason["value"]
                if reason == "full":
                    return False, "full"
                # 其它 WS 失败也可能是假失败，列表复核一次
                if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                    return True, "success"
                return False, reason
            if ws_heartbeat:
                # 只匹配有 clazzId 且等于目标的成功消息
                msgs = ws_heartbeat.check_success(target_id_str)
                if msgs:
                    return True, "success"
            time.sleep(0.1)

        # 超时：列表复核
        if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
            print("[✓] WebSocket 超时，但列表复核确认已选上")
            return True, "success"

        # 超时未确认：不视为成功，交由上层继续抢
        print("[!] WebSocket 超时未确认成功，将继续尝试选课（不会再次退课）")
        main_logger.warning("选课入队后 WS 超时未确认")
        return False, "timeout"
    except Exception as e:
        print(f"[✗] 选课请求异常: {e}")
        main_logger.error(f"选课请求异常: {e}")
        # 异常路径也尝试复核一次
        try:
            if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                return True, "success"
        except Exception:
            pass
        return False, "exception"
    finally:
        if ws_heartbeat:
            ws_heartbeat.set_success_callback(None)
            ws_heartbeat.set_fail_callback(None)


def start_monitoring(session, token, batch_id, campus, target_class, drop_class_info,
                     course_type=COURSE_TYPE_FANKC, ws_heartbeat=None, http_heartbeat=None):
    """监控目标课程容量，检测到空位后：退旧课 → 抢新课。

    状态机：
      watching           监控容量，有空位才退课
      dropped_pending_add 已退旧课，只抢目标，绝不再退
                         目标失败时尝试回补旧课；回补成功则回到 watching
    """
    target_id = target_class['JXBID']
    drop_id = drop_class_info['JXBID']
    # 退课时尽量保留最新快照（含 secretVal），供回补使用
    drop_snapshot = dict(drop_class_info)

    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz.get('KCM', '未知'))}({clazz.get('classificationName', '')})"
        return clazz.get('KCM', '未知')

    def sync_after_relogin(new_token):
        """重登后同步主循环 token / HTTP 心跳 / WS cookie"""
        nonlocal token
        token = new_token
        login_state.token = new_token
        if http_heartbeat is not None:
            http_heartbeat.update_token(new_token)
        if ws_heartbeat and login_state.session is not None:
            ws_heartbeat.update_cookies(dict(login_state.session.cookies))

    def attempt_restore_dropped(current_token, reason_tag=""):
        """目标未选上时，尝试把旧课加回。

        Returns:
            bool: 是否确认旧课已在选课结果中（含原本就还在 / 本次回补成功）
        """
        nonlocal drop_snapshot
        live = _get_live_token(current_token)
        tag = f"({reason_tag})" if reason_tag else ""
        print(f"[↩] 尝试回补旧课{tag}: {get_display_name(drop_snapshot)} - {drop_snapshot.get('SKJS', '-')}")
        main_logger.info(f"尝试回补旧课{tag}: JXBID={drop_id}")

        # 已在已选列表则视为回补成功（可能未真正退掉 / 上次已加回）
        if _confirm_selected_by_list(session, live, batch_id, drop_id, course_type):
            print(f"[✓] 旧课已在选课结果中，无需再投: {get_display_name(drop_snapshot)}")
            main_logger.info(f"回补跳过(列表已选): {drop_id}")
            return True

        # 尽量刷新 secretVal（全量列表里找该班）
        restore_class = dict(drop_snapshot)
        try:
            _, _, latest = get_course_capacity(
                session, live, batch_id, campus, drop_id, course_type
            )
            if latest and latest.get("secretVal"):
                restore_class = dict(latest)
                if not restore_class.get("KCM") and drop_snapshot.get("KCM"):
                    restore_class["KCM"] = drop_snapshot.get("KCM")
                drop_snapshot = restore_class
        except Exception as e:
            main_logger.warning(f"回补前刷新 secretVal 失败: {e}")

        if not restore_class.get("secretVal"):
            print("[✗] 回补失败：旧课缺少 secretVal，请手动登录教务核对课表")
            main_logger.error(f"回补缺少 secretVal: {drop_id}")
            return False

        ok, reason = _try_add_class(
            session, live, batch_id, restore_class, course_type,
            ws_heartbeat=ws_heartbeat, wait_timeout=5.0,
        )
        if ok:
            print(f"[✓] 旧课回补成功: {get_display_name(restore_class)} - {restore_class.get('SKJS', '-')}")
            main_logger.info(f"旧课回补成功: {drop_id}")
            return True

        if reason == "relogin":
            live2 = _get_live_token(live)
            if live2:
                sync_after_relogin(live2)
                live = live2
            # 重登后再确认一次列表
            if _confirm_selected_by_list(session, live, batch_id, drop_id, course_type):
                print(f"[✓] 重登后列表确认旧课已在: {get_display_name(restore_class)}")
                return True

        print(f"[✗] 旧课回补未成功({reason})，将继续抢目标（课表可能暂时为空）")
        main_logger.warning(f"旧课回补失败: reason={reason}, JXBID={drop_id}")
        return False

    def handle_target_fail_then_restore(current_token, reason, force=False):
        """目标抢失败后：累计失败次数，达阈值或 force 时尝试回补。

        避免每次失败都回补导致「退→抢失败→回补→再退」空转。

        Returns:
            str: "restored" 回到 watching；"keep" 保持 dropped_pending_add
        """
        nonlocal state, pending_fail_streak, last_restore_ok_at
        if reason == "relogin":
            live = _get_live_token(token)
            if live:
                sync_after_relogin(live)
                current_token = live

        pending_fail_streak += 1
        # force（Ctrl+C 不走这里）或连续失败达到阈值才回补
        should_restore = force or pending_fail_streak >= RESTORE_AFTER_FAILS
        if not should_restore:
            print(
                f"[!] 目标未成功({reason})，继续抢目标 "
                f"({pending_fail_streak}/{RESTORE_AFTER_FAILS} 次后尝试回补旧课)"
            )
            main_logger.warning(
                f"目标未成功暂不回补: reason={reason}, streak={pending_fail_streak}"
            )
            return "keep"

        print(f"[!] 目标连续未成功({reason}, streak={pending_fail_streak})，尝试回补旧课以防空窗...")
        main_logger.warning(f"目标未成功，尝试回补: {reason}, streak={pending_fail_streak}")
        if attempt_restore_dropped(current_token, reason_tag=reason):
            state = "watching"
            pending_fail_streak = 0
            last_restore_ok_at = time.time()
            print(
                f"[→] 已回补旧课，回到监控（{RESTORE_COOLDOWN_SEC:.0f}s 内不重复退课，"
                "之后有空位再换）"
            )
            main_logger.info("状态切换: dropped_pending_add -> watching (回补成功)")
            return "restored"
        # 回补失败：保持已退，重置计数以便隔几次再试回补
        pending_fail_streak = 0
        print("[!] 回补失败，保持「已退待选」，继续抢目标（不会再退课）")
        return "keep"

    target_name = get_display_name(target_class)
    drop_name = get_display_name(drop_class_info)

    # watching | dropped_pending_add
    # 已退待选时目标连续失败次数；达 RESTORE_AFTER_FAILS 才回补，防抖
    RESTORE_AFTER_FAILS = 3
    RESTORE_COOLDOWN_SEC = 20.0  # 回补成功后冷却，避免立刻再退再抢空转
    state = "watching"
    check_count = 0
    auth_fail_streak = 0
    pending_fail_streak = 0
    last_restore_ok_at = 0.0

    print("\n" + "="*60)
    print(f"监控目标课程: {target_name} - {target_class['SKJS']}")
    print(f"准备退掉课程: {drop_name} - {drop_class_info['SKJS']}")
    print("每 5 秒检测一次课容量；空位时退课→选课。")
    print(
        f"状态保护：退课后持续抢目标；连续 {RESTORE_AFTER_FAILS} 次未中则尝试回补旧课，"
        f"回补成功后 {RESTORE_COOLDOWN_SEC:.0f}s 内不再退。"
    )
    print("Ctrl+C 中断时会立即尝试回补旧课。")
    print("="*60 + "\n")

    main_logger.info(f"开始监控: 目标={target_name}, 退课={drop_name}")

    try:
        while True:
            check_count += 1
            current_token = _get_live_token(token)
            if current_token and current_token != token:
                # 心跳可能已重登，同步到本循环与 HTTP 心跳对象
                sync_after_relogin(current_token)

            current_selected, capacity, updated_class = get_course_capacity(
                session, current_token, batch_id, campus, target_id, course_type
            )

            if current_selected is None:
                # 已退待选：容量查询失败也不跳过，用 target_class 快照继续抢
                if state == "dropped_pending_add":
                    auth_fail_streak += 1
                    print(f"[{check_count}] 查询失败，但仍处于已退待选，继续投递... ({auth_fail_streak})")
                    if auth_fail_streak >= 3:
                        print(f"\n[!] 连续查询失败，尝试重新登录...")
                        success, new_token = handle_relogin(None)
                        if success and new_token:
                            sync_after_relogin(new_token)
                            auth_fail_streak = 0
                            current_token = _get_live_token(token) or new_token
                            print("[✓] 重新登录成功，继续抢课")
                        else:
                            print("[✗] 重新登录失败，稍后仍会用现有会话尝试选课")
                    select_class = target_class
                    dt = datetime.datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{dt}] [已退待选] 查询失败，用快照继续抢: {target_name}")
                    ok, reason = _try_add_class(
                        session, current_token, batch_id, select_class, course_type,
                        ws_heartbeat=ws_heartbeat, wait_timeout=5.0
                    )
                    if ok:
                        print(f"\n[✓] 选课成功！{target_name} - {target_class['SKJS']}")
                        main_logger.info(f"选课成功(已退待选/查询失败路径): {target_name}")
                        break
                    handle_target_fail_then_restore(current_token, reason)
                    time.sleep(1)
                    continue

                # watching：查询失败走 streak 重登，不投递
                auth_fail_streak += 1
                print(f"[{check_count}] 查询失败，重试中... ({auth_fail_streak})", end='\r')
                # 连续失败时尝试重登（token 可能已过期但响应仍是 JSON 失败）
                if auth_fail_streak >= 3:
                    print(f"\n[!] 连续查询失败，尝试重新登录...")
                    success, new_token = handle_relogin(None)
                    if success and new_token:
                        sync_after_relogin(new_token)
                        auth_fail_streak = 0
                        print("[✓] 重新登录成功，继续监控")
                    else:
                        print("[✗] 重新登录失败，稍后重试")
                time.sleep(5)
                continue

            auth_fail_streak = 0
            dt = datetime.datetime.now().strftime("%H:%M:%S")
            has_slot = current_selected < capacity

            # ---- 状态：已退后只抢目标；失败则回补 ----
            if state == "dropped_pending_add":
                # 即使当前看起来已满也继续投递（可能瞬时有人退）
                select_class = updated_class if updated_class else target_class
                print(f"\n[{dt}] [已退待选] 继续抢目标: {target_name} ({current_selected}/{capacity})")
                ok, reason = _try_add_class(
                    session, current_token, batch_id, select_class, course_type,
                    ws_heartbeat=ws_heartbeat, wait_timeout=5.0
                )
                if ok:
                    print(f"\n[✓] 选课成功！{target_name} - {target_class['SKJS']}")
                    main_logger.info(f"选课成功(已退待选): {target_name}")
                    break
                handle_target_fail_then_restore(current_token, reason)
                time.sleep(1)
                continue

            # ---- 状态：watching ----
            if has_slot:
                # 刚回补成功后的冷却：避免「回补→立刻又退」抖动
                if last_restore_ok_at and (time.time() - last_restore_ok_at) < RESTORE_COOLDOWN_SEC:
                    remain = RESTORE_COOLDOWN_SEC - (time.time() - last_restore_ok_at)
                    print(
                        f"[{dt}] 有空位，但回补冷却中({remain:.0f}s)，暂不退课 "
                        f"{target_name}: {current_selected}/{capacity}",
                        end='\r',
                    )
                    time.sleep(min(5, max(1, remain)))
                    continue

                print(f"\n[{dt}] 检测到空位！当前 {current_selected}/{capacity}")
                main_logger.info(f"检测到空位: {current_selected}/{capacity}")

                # 1. 退课（仅 watching 状态执行一次）
                print(f"[>] 正在退课: {drop_name}...")
                # 尝试用最新 secretVal（若列表里能找到该已选课）
                drop_info = drop_class_info
                try:
                    _, _, latest_drop = get_course_capacity(
                        session, current_token, batch_id, campus,
                        drop_class_info['JXBID'], course_type
                    )
                    if latest_drop and latest_drop.get('secretVal'):
                        drop_info = latest_drop
                        drop_snapshot = dict(latest_drop)
                        if not drop_snapshot.get("KCM") and drop_class_info.get("KCM"):
                            drop_snapshot["KCM"] = drop_class_info.get("KCM")
                except Exception:
                    pass

                drop_success = drop_class(session, current_token, batch_id, drop_info, course_type)
                # drop 可能触发重登，同步 token / 心跳 / WS
                live_after_drop = _get_live_token(current_token)
                if live_after_drop and live_after_drop != current_token:
                    sync_after_relogin(live_after_drop)
                    current_token = live_after_drop
                if not drop_success:
                    print("[!] 退课失败，继续监控（不进入待选状态）...")
                    main_logger.warning("退课失败，继续监控")
                    time.sleep(5)
                    continue

                # 退课成功 → 进入待选，后续绝不再退；快照供回补
                drop_snapshot = dict(drop_info)
                state = "dropped_pending_add"
                pending_fail_streak = 0
                print(
                    f"[✓] 退课成功，进入「已退待选」"
                    f"（连续 {RESTORE_AFTER_FAILS} 次目标失败会尝试回补）"
                )
                main_logger.info("状态切换: watching -> dropped_pending_add")

                # 2. 立刻选课
                select_class = updated_class if updated_class else target_class
                print(f"[>] 正在选课: {target_name}...")
                ok, reason = _try_add_class(
                    session, current_token, batch_id, select_class, course_type,
                    ws_heartbeat=ws_heartbeat, wait_timeout=5.0
                )
                if ok:
                    print(f"\n[✓] 选课成功！{target_name} - {target_class['SKJS']}")
                    main_logger.info(f"选课成功: {target_name}")
                    break

                handle_target_fail_then_restore(current_token, reason)
                time.sleep(1)
                continue

            # 已满
            print(f"[{dt}] [{check_count}] {target_name}: {current_selected}/{capacity} (已满) [{state}]", end='\r')
            time.sleep(5)

    except KeyboardInterrupt:
        print("\n\n监控已由用户手动停止。")
        if state == "dropped_pending_add":
            print("[!] 中断时处于「已退待选」，正在尝试回补旧课...")
            main_logger.warning("用户中断时处于 dropped_pending_add，尝试回补")
            live = _get_live_token(token)
            restored = attempt_restore_dropped(live, reason_tag="Ctrl+C")
            if restored:
                print("[✓] 中断回补成功，请仍建议登录教务核对课表。")
            else:
                print("[!] 警告：旧课可能已退、目标可能未选上，请立刻登录教务核对课表！")
        main_logger.info("监控已停止")


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
    """运行监控换课流程

    流程：
    1. 选择目标课程（只选1个）
    2. 从已选课程中选择要退的课
    3. 确认后开始监控
    4. 检测到空位：退课 → 选课（失败则保持已退状态继续抢）
    """
    student_id = username
    if not student_id:
        print("错误: 学号无效，无法建立 WebSocket（请提供 -u 或确保 login_data 含学号）")
        main_logger.error("WebSocket student_id 无效，退出")
        return

    # 先创建 WS，再挂 HTTP 心跳的 on_relogin 以便重登后同步 cookie
    ws_heartbeat = WebSocketHeartbeat(
        student_id=student_id,
        cookies=dict(session.cookies),
        use_vpn=use_vpn
    )

    def on_http_relogin(new_token):
        """HTTP 心跳重登成功后同步 token 与 WS cookies"""
        login_state.token = new_token
        try:
            ws_heartbeat.update_cookies(dict(session.cookies))
        except Exception as e:
            heartbeat_logger.warning(f"on_relogin 同步 WS cookie 失败: {e}")

    # 启动 HTTP 心跳（维持登录态，每30秒请求一次课程列表）
    http_heartbeat = HttpHeartbeat(
        session=session,
        token=token,
        batch_id=batch_id,
        campus=campus,
        interval=30,
        on_relogin=on_http_relogin,
    )
    http_heartbeat.start()

    # 启动 WebSocket 心跳（用于确认真实选课成功）
    ws_heartbeat.start()

    try:
        # 1. 选择目标课程（只选1个）
        print("\n--- 第一步：选择要抢的目标课程 ---")
        result = choose_class(session, token, batch_id, campus, course_type)
        if result is None or result[0] is None:
            return
        if result[0] == "BACK":
            print("已返回上一级。")
            return

        selected_classes, all_classes = result
        if not isinstance(selected_classes, list) or not selected_classes:
            print("未选择有效目标课程。")
            return

        # 只取第一个作为目标课程
        target_class = selected_classes[0]

        def get_display_name(clazz):
            if course_type == COURSE_TYPE_TYKC:
                return f"{clazz.get('projectName', clazz.get('KCM', '未知'))}({clazz.get('classificationName', '')})"
            return clazz.get('KCM', '未知')

        print(f"\n目标课程: {get_display_name(target_class)} - {target_class['SKJS']} [{target_class['YXRS']}/{target_class['KRL']}]")
        print(f"上课地点: {target_class.get('teachingPlace', '未安排地点')}")

        # 2. 独立拉取已选课（不依赖目标课列表页 all_classes）
        print("\n--- 第二步：从已选课中选择要退掉的课 ---")
        print("正在拉取已选课程列表...")
        selected_only = fetch_selected_classes(
            session, _get_live_token(token), batch_id, campus, course_type,
            exclude_jxbid=target_class.get("JXBID"),
        )

        if not selected_only:
            print("\n[!] 未找到可退的已选课程（SFYX=1）。")
            print("    请确认：当前轮次下是否已有已选课；或列表分页/类型不匹配。")
            return

        for i, clazz in enumerate(selected_only, 1):
            place = clazz.get('teachingPlace') or '未安排地点'
            print(
                f"  [{i}] {get_display_name(clazz)} - {clazz.get('SKJS', '-')} "
                f"[{clazz.get('YXRS', '?')}/{clazz.get('KRL', '?')}]\n"
                f"      {place}"
            )

        while True:
            try:
                drop_choice = int(input("请输入要退掉的已选课程序号: ").strip())
                if 1 <= drop_choice <= len(selected_only):
                    drop_class_info = selected_only[drop_choice - 1]
                    break
                else:
                    print("无效的序号，请重新输入。")
            except ValueError:
                print("请输入有效的数字。")

        print(f"\n要退的课程: {get_display_name(drop_class_info)} - {drop_class_info['SKJS']}")
        print(f"上课地点: {drop_class_info.get('teachingPlace', '未安排地点')}")

        # 3. 确认
        print("\n" + "="*50)
        print("确认信息：")
        print(f"  目标课程: {get_display_name(target_class)} - {target_class['SKJS']}")
        print(f"  要退课程: {get_display_name(drop_class_info)} - {drop_class_info['SKJS']}")
        print("  注意：退课后会持续抢目标；多次未中会尝试回补旧课，Ctrl+C 也会回补。")
        print("="*50)

        confirm = input("\n确认开始监控？(y/n): ").strip().lower()
        if confirm != 'y':
            print("操作已取消。")
            return

        # 4. 开始监控
        start_monitoring(
            session, token, batch_id, campus, target_class, drop_class_info,
            course_type=course_type, ws_heartbeat=ws_heartbeat, http_heartbeat=http_heartbeat,
        )

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
        if not args.username or not args.password:
            print("[!] 提示: --skip-login 未提供 -u/-p，会话失效时无法自动重登")
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

    # 初始化日志系统（使用学号，如果有的话；skip-login 后续会用 login_data 补全）
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

    # skip-login 时从 login_data 补全学号，供 WS / 重登使用
    student_id = args.username or _extract_student_id(login_data)
    if student_id:
        login_state.username = student_id
        if not args.username:
            # 补全日志目录（原先用 unknown）
            setup_logging(student_id)
    else:
        print("错误: 无法获取学号（请提供 -u 或确保用户信息含 XH）")
        main_logger.error("学号无效，退出")
        return

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
        username=student_id,
        use_vpn=use_vpn,
        course_type=course_type
    )


if __name__ == "__main__":
    main()