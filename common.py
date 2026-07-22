"""course.py / course_cron.py 共享基建。

包含：全局常量、日志、登录状态、鉴权/文案判定、VPN与选课系统重登、
HTTP/WebSocket 双心跳、验证码 OCR、登录、cookie 加载、参数解析。

业务主流程（抢课循环 / 监控换课）仍在各自脚本中。
"""
import argparse
import base64
import json
import time
import os
import logging
import threading
import re
from urllib.parse import urlencode

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import ddddocr
import websocket
from bs4 import BeautifulSoup

from vpnlogin import NuistVPNClient

# --- 全局配置 ---

# AES 加密密钥 (来自前端代码)
AES_KEY = "MWMqg2tPcDkxcm11".encode('utf-8')

# 课程类型常量
COURSE_TYPE_FANKC = "FANKC"  # 方案内课程
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

# 全局 Logger（在 setup_logging 中初始化；logging.getLogger 按名单例，
# 两个脚本与本模块拿到的是同一对象）
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
    其它业务 code 不再靠「过期/失效」等宽泛关键词猜鉴权。
    """
    msg = str(msg or "")
    if code == 401:
        return True
    if code == 403:
        keywords = ("登录", "token", "Token", "授权", "认证", "未登录", "过期", "失效", "重新登录")
        return any(k in msg for k in keywords)
    return False


def _get_live_token(fallback=None):
    """优先使用 login_state 中最新 token，避免调用方用过期快照"""
    return login_state.token or fallback


def _is_full_msg(msg):
    """判断消息是否表示课程已满（避免过宽匹配「满」字）。

    不使用裸「已满」（会误伤「已满足先修条件」），也不用单字「满」。
    关键词取 course/cron 两版并集。
    """
    msg = str(msg or "")
    keywords = ("课容量已满", "人数已满", "容量已满", "名额已满", "课已满", "课容量已达上限")
    return any(k in msg for k in keywords)


def _is_already_selected_msg(msg):
    """判断消息是否表示课程已选上 / 重复选课（宁严勿宽）。

    关键词取 course/cron 两版并集。
    """
    msg = str(msg or "")
    keywords = (
        "已选上",
        "已选该",
        "已经选过",
        "已经选择",
        "已选择该",
        "您已选",
        "你已选",
        "重复选",
        "不能重复",
        "不可重复",
        "请勿重复",
        "重复提交",
        "已在选课结果",
        "已在选课名单",
        "已选中该",
    )
    return any(k in msg for k in keywords)


def _is_hard_fail_msg(msg):
    """判断无法靠重投解决的业务失败（宁严勿宽）。"""
    msg = str(msg or "")
    # 先排除「已满」类，避免与 full 判定交叉
    if _is_full_msg(msg) or _is_already_selected_msg(msg):
        return False
    keywords = (
        "时间冲突",
        "上课时间冲突",
        "选课冲突",
        "课程冲突",
        "限选",
        "不满足",
        "不满足先修",
        "先修条件不",
        "缺少先修",
        "未修读",
        "学分不足",
        "超过学分",
        "超出学分",
        "学分上限",
        "未开放",
        "不允许选",
        "不可选",
        "无权限",
        "性别限制",
        "年级限制",
        "专业限制",
    )
    return any(k in msg for k in keywords)


def _extract_student_id(login_data, fallback=None):
    """从登录/用户信息中提取学号（键取两版并集，student 优先、顶层兜底）。"""
    if not isinstance(login_data, dict):
        return fallback
    student = login_data.get("student") if isinstance(login_data.get("student"), dict) else {}
    for key in ("XH", "xh", "studentId", "student_id", "studentID",
                "loginName", "username", "USERID", "id"):
        val = student.get(key) if student else None
        if not val:
            val = login_data.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return fallback


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


def switch_batch(session, token, batch_id):
    """POST /elective/user 切换服务端「当前轮次」。

    服务端会话中保存了轮次状态（平台需要先切轮次后续接口才落在正确轮次），
    重登后该状态会丢失，因此重登成功后需要重放一次。

    Returns:
        bool: 是否切换成功
    """
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    try:
        response = session.post(
            URL_SWITCH_BATCH, headers=headers,
            data=f"batchId={batch_id}", timeout=10
        )
        data = response.json()
        if data.get("code") == 200:
            return True
        main_logger.warning(f"切换轮次失败: code={data.get('code')}, msg={data.get('msg')}")
        return False
    except Exception as e:
        main_logger.warning(f"切换轮次请求异常: {e}")
        return False


def relogin_system():
    """重新登录选课系统；成功后重放「切换轮次」恢复服务端轮次状态。"""
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
            # 重登后服务端「当前轮次」会重置，重放切换，避免后续 add/list 落在默认轮次
            if login_state.batch_id:
                if switch_batch(login_state.session, login_state.token, login_state.batch_id):
                    main_logger.info(f"重登后已重新切换轮次: {login_state.batch_id}")
                else:
                    print("[!] 重登后切换轮次失败，后续请求可能落在错误轮次")
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

    def set_on_relogin(self, callback):
        """设置重登成功回调：callback(new_token)"""
        self.on_relogin = callback

    def _notify_relogin(self, new_token):
        """重登成功后更新 token 并触发 on_relogin（如同步 WS cookies）"""
        self.token = new_token
        login_state.token = new_token
        if self.on_relogin:
            try:
                self.on_relogin(new_token)
            except Exception as e:
                heartbeat_logger.error(f"[HTTP心跳] on_relogin 回调异常: {e}")

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
    """WebSocket 心跳管理，每隔5秒发送 'hi' 保持连接，并监听选课成功/失败消息。

    连接由单一「连接循环」线程维护：run_forever 返回（断开）后循环重连，
    不在 on_close 里递归重连；心跳由单一线程发送，始终指向当前连接。
    避免旧实现「每次重连叠加一个心跳线程 + 调用栈越叠越深」的泄漏。
    """

    def __init__(self, student_id, cookies=None, use_vpn=False, on_course_success=None, on_course_fail=None):
        self.student_id = student_id
        self.cookies = cookies
        self.use_vpn = use_vpn
        self.ws = None
        self.running = False
        self.thread = None
        self.heartbeat_thread = None

        # 选课成功回调函数
        # 回调参数: (clazz_id, course_name, msg, data)
        self.on_course_success = on_course_success

        # 选课失败回调函数（如课容量已满）
        # 回调参数: (code, msg, data)
        self.on_course_fail = on_course_fail

        # 存储最近收到的选课成功消息（用于同步查询）
        self.success_messages = []  # list of dict
        self._lock = threading.Lock()

        # 根据是否使用VPN选择不同的WebSocket地址
        if use_vpn:
            # VPN 模式下的 WebSocket 地址
            self.ws_url = f"wss://client.vpn.nuist.edu.cn/https/webvpn3315a96df5a2811a49489fcebfe8b135dece10c6255d04cc36c652f60ee89b3a/xsxk/websocket/{student_id}"
        else:
            self.ws_url = f"wss://xsxk.nuist.edu.cn/xsxk/websocket/{student_id}"

    def _on_open(self, ws):
        heartbeat_logger.info(f"[WebSocket] 连接已建立: {self.student_id}")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            code = data.get("code")
            msg = data.get("msg", "")
            result_data = data.get("data")

            # 检查是否为心跳包响应: { "code": 200, "msg": "操作成功", "data": "heart" }
            if code == 200 and result_data == "heart":
                heartbeat_logger.debug(f"[WebSocket] 心跳包响应: {data}")
                return  # 心跳包不做进一步处理

            heartbeat_logger.info(f"[WebSocket] 解析消息: {data}")

            # 检查是否为选课成功消息
            # 成功消息格式: code=200, msg="选课成功:课程名", data包含clazzId和xkjgList
            if code == 200 and "选课成功" in str(msg):
                # 解析选课成功信息
                clazz_id = ""
                course_name = ""

                if isinstance(result_data, dict):
                    clazz_id = result_data.get("clazzId", "")
                    xkjg_list = result_data.get("xkjgList", [])

                    # 从 xkjgList 中查找刚选中的课程
                    # 通过 teachingClassID 匹配 clazzId（统一 str 比较）
                    clazz_id = str(clazz_id) if clazz_id else ""
                    for course in xkjg_list:
                        if str(course.get("teachingClassID") or "") == clazz_id:
                            course_name = course.get("KCM", "")
                            break

                # 如果在 xkjgList 中没找到，尝试从 msg 中提取课程名作为备用
                if not course_name:
                    course_name = msg.split(":", 1)[1] if ":" in str(msg) else str(msg)

                clazz_id = str(clazz_id) if clazz_id else ""
                print(f"\n[WebSocket] 🎉 选课成功: {course_name}")
                main_logger.info(f"[WebSocket] 选课成功: {course_name}, clazzId={clazz_id or '(空)'}")
                heartbeat_logger.info(f"[WebSocket] 选课成功响应: {data}")

                # 存储成功消息（空 clazz_id 也记录，但 check_success 轮询会忽略）
                with self._lock:
                    self.success_messages.append({
                        "clazz_id": clazz_id,
                        "course_name": course_name,
                        "msg": msg,
                        "data": result_data,
                        "timestamp": time.time()
                    })

                # 调用回调函数（空 id 由上层决定是否采纳）
                if self.on_course_success:
                    try:
                        self.on_course_success(clazz_id, course_name, msg, result_data)
                    except Exception as e:
                        heartbeat_logger.error(f"[WebSocket] 选课成功回调异常: {e}")

            # 检查是否为选课失败消息（课容量已满等）
            elif code == 500 and _is_full_msg(msg):
                print(f"\n[WebSocket] ⚠️ 选课失败: {msg}")
                main_logger.warning(f"[WebSocket] 选课失败 - 课容量已满: {msg}")
                heartbeat_logger.warning(f"[WebSocket] 选课失败响应: {data}")

                # 调用失败回调函数，触发上层处理（切备选/回补等）
                if self.on_course_fail:
                    try:
                        self.on_course_fail(code, msg, result_data)
                    except Exception as e:
                        heartbeat_logger.error(f"[WebSocket] 选课失败回调异常: {e}")

            elif code is not None and code != 200:
                print(f"\n[WebSocket 警告] code={code}, msg={msg}")
                heartbeat_logger.warning(f"[WebSocket] 非200响应: code={code}, msg={msg}")
                # 其它失败也通知上层，避免主循环死等
                if self.on_course_fail and code == 500:
                    try:
                        self.on_course_fail(code, msg, result_data)
                    except Exception as e:
                        heartbeat_logger.error(f"[WebSocket] 选课失败回调异常: {e}")

        except json.JSONDecodeError:
            # 非 JSON 消息（如心跳响应），忽略
            pass
        except Exception as e:
            heartbeat_logger.error(f"[WebSocket] 解析消息异常: {e}")

    def _on_error(self, ws, error):
        heartbeat_logger.error(f"[WebSocket] 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        heartbeat_logger.info(f"[WebSocket] 连接已关闭: {close_status_code} - {close_msg}")
        # 重连由 _connect_loop 统一处理，这里不做递归重连

    def _heartbeat_loop(self):
        """单一心跳线程：连接可用时每5秒发送一次 'hi'，断开时静默等待重连"""
        while self.running:
            try:
                ws = self.ws
                if ws and ws.sock and ws.sock.connected:
                    ws.send("hi")
                    heartbeat_logger.debug("[WebSocket] 发送心跳: hi")
                time.sleep(5)
            except Exception as e:
                # 发送失败通常意味着连接正在断开，交由连接循环重连
                heartbeat_logger.warning(f"[WebSocket] 心跳发送失败: {e}")
                time.sleep(2)

    def _connect_loop(self):
        """连接循环：断开后自动用最新 cookies 重连，直到 stop()"""
        while self.running:
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
                self.ws.run_forever()  # 阻塞至连接关闭
            except Exception as e:
                heartbeat_logger.error(f"[WebSocket] 连接失败: {e}")
            if self.running:
                heartbeat_logger.info("[WebSocket] 2秒后尝试重新连接...")
                time.sleep(2)

    def start(self):
        """启动 WebSocket 连接循环与心跳（各一个后台线程，全生命周期不叠加）"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._connect_loop, daemon=True)
        self.thread.start()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        heartbeat_logger.info("[WebSocket] 连接/心跳线程已启动")

    def stop(self):
        """停止 WebSocket 心跳"""
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        heartbeat_logger.info("[WebSocket] 心跳已停止")

    def set_success_callback(self, callback):
        """设置选课成功回调函数

        Args:
            callback: 回调函数，参数为 (clazz_id, course_name, msg, data)
        """
        self.on_course_success = callback

    def set_fail_callback(self, callback):
        """设置选课失败回调函数

        Args:
            callback: 回调函数，参数为 (code, msg, data)
        """
        self.on_course_fail = callback

    def check_success(self, clazz_id=None):
        """检查是否有选课成功消息

        Args:
            clazz_id: 可选，指定要检查的课程ID。如果不指定则返回非空 clazz_id 的成功消息。

        Returns:
            list: 匹配的成功消息列表（忽略空 clazz_id）
        """
        with self._lock:
            # 空 clazz_id 消息一律忽略，避免误判成功
            msgs = [m for m in self.success_messages if m.get("clazz_id")]
            if clazz_id is not None and str(clazz_id) != "":
                cid = str(clazz_id)
                return [m for m in msgs if str(m.get("clazz_id")) == cid]
            return list(msgs)

    def clear_success_messages(self, clazz_id=None):
        """清空成功消息列表。

        Args:
            clazz_id: 可选。传入时只清该教学班的消息——避免把其它课
                （如监控目标课）迟到的成功消息一并抹掉。
        """
        with self._lock:
            if clazz_id is None:
                self.success_messages.clear()
            else:
                cid = str(clazz_id)
                self.success_messages = [
                    m for m in self.success_messages
                    if str(m.get("clazz_id") or "") != cid
                ]

    def update_cookies(self, cookies):
        """重登后更新 cookie；关闭当前连接，连接循环会用新 cookie 重连"""
        self.cookies = cookies or {}
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


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
