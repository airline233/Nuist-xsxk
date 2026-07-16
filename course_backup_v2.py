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
from urllib.parse import urlencode, quote_plus
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import ddddocr
import websocket
from rich.console import Console
from rich.table import Table
from rich.text import Text
from vpnlogin import NuistVPNClient

# --- 全局配置 ---

# AES 加密密钥 (来自前端代码)
AES_KEY = "MWMqg2tPcDkxcm11".encode('utf-8')

# API 端点
BASE_URL = "https://client.vpn.nuist.edu.cn/https/webvpn3315a96df5a2811a49489fcebfe8b135dece10c6255d04cc36c652f60ee89b3a/xsxk"
# BASE_URL = "http://xsxk.nuist.edu.cn/xsxk"
URL_CAPTCHA = f"{BASE_URL}/auth/captcha?enlink-vpn"
URL_LOGIN = f"{BASE_URL}/auth/login?enlink-vpn"
URL_LIST_CLASSES = f"{BASE_URL}/elective/clazz/list?enlink-vpn"
URL_ADD_CLASS = f"{BASE_URL}/elective/clazz/add?enlink-vpn"
URL_SWITCH_BATCH = f"{BASE_URL}/elective/user?enlink-vpn"
# COOKIE_FILE = "ck.txt"

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='course_grab.log', encoding='utf-8')

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
        print(f"[WebSocket] 连接已建立: {self.student_id}")
        logging.info(f"[WebSocket] 连接已建立: {self.student_id}")
        # 启动心跳线程
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
    
    def _on_message(self, ws, message):
        logging.debug(f"[WebSocket] 收到消息: {message}")
        try:
            data = json.loads(message)
            logging.info(f"[WebSocket] 解析消息: {data}")
            code = data.get("code")
            if code is not None and code != 200:
                msg = data.get("msg", "未知错误")
                print(f"\n[WebSocket 警告] code={code}, msg={msg}")
                logging.warning(f"[WebSocket] 非200响应: code={code}, msg={msg}")
        except json.JSONDecodeError:
            # 非 JSON 消息（如心跳响应），忽略
            pass
        except Exception as e:
            logging.error(f"[WebSocket] 解析消息异常: {e}")
    
    def _on_error(self, ws, error):
        logging.error(f"[WebSocket] 错误: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[WebSocket] 连接已关闭")
        logging.info(f"[WebSocket] 连接已关闭: {close_status_code} - {close_msg}")
        # 如果还在运行状态，尝试重连
        if self.running:
            print("[WebSocket] 尝试重新连接...")
            time.sleep(2)
            self._connect()
    
    def _heartbeat_loop(self):
        """心跳循环，每5秒发送一次 'hi'"""
        while self.running and self.ws:
            try:
                if self.ws.sock and self.ws.sock.connected:
                    self.ws.send("hi")
                    logging.debug("[WebSocket] 发送心跳: hi")
                time.sleep(5)
            except Exception as e:
                logging.error(f"[WebSocket] 心跳发送失败: {e}")
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
            logging.error(f"[WebSocket] 连接失败: {e}")
    
    def start(self):
        """启动 WebSocket 心跳（在后台线程运行）"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._connect, daemon=True)
        self.thread.start()
        print(f"[WebSocket] 心跳线程已启动")
    
    def stop(self):
        """停止 WebSocket 心跳"""
        self.running = False
        if self.ws:
            self.ws.close()
        print("[WebSocket] 心跳已停止")


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
    session.get(BASE_URL)  # 访问首页以建立会话
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

def choose_elective_batch(student_data):
    """让用户选择一个可选的选课轮次"""
    print("\n--- 请选择一个选课轮次 ---")
    batches = student_data.get("student", {}).get("electiveBatchList", [])
    
    # 筛选出 canSelect 为 "1" 的可选轮次
    available_batches = [b for b in batches if b.get("canSelect") == "1"]
    
    if not available_batches:
        print("未找到当前可用的选课轮次。")
        return None

    for i, batch in enumerate(available_batches):
        print(f"  [{i+1}] {batch['name']} ({batch['beginTime']} - {batch['endTime']})")
    
    while True:
        try:
            choice = int(input("请输入数字序号选择轮次: "))
            if 1 <= choice <= len(available_batches):
                return available_batches[choice-1]
            else:
                print("无效的输入，请输入列表中的数字。")
        except ValueError:
            print("请输入一个有效的数字。")
            
def choose_class(session, token, batch_id, campus):
    """获取课程列表并让用户选择"""
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
        "teachingClassType": "FANKC",
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
            
        all_classes = []
        courses = data.get("data", {}).get("rows", [])
        for course in courses:
            all_classes.extend(course.get("tcList", []))
        
        if not all_classes:
            print("在此轮次下未找到可选的课程。")
            return None, None

        # 根据课程数量动态决定列数 (3-5列)
        total = len(all_classes)
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

        print("\n请输入课程序号（多个用逗号或空格分隔，如 1,3,5 或 1 3 5）")
        print("第一个为主选课程，后面的为备选课程（主课程已满时自动切换）")
        
        while True:
            choice_str = input("课程序号: ").strip()
            if not choice_str:
                print("请输入至少一个课程序号。")
                continue
            
            try:
                # 支持逗号或空格分隔
                import re
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

def start_grabbing(session, token, batch_id, selected_class, backup_classes=None):
    """开始循环抢课，支持备选课程切换"""
    if backup_classes is None:
        backup_classes = []
    
    # 构建课程队列：主课程 + 备选课程
    course_queue = [selected_class] + backup_classes
    current_index = 0
    
    current_class = course_queue[current_index]
    class_id = current_class['JXBID']
    secret_val = current_class['secretVal']

    print("\n" + "="*50)
    print(f"准备抢课: {current_class['KCM']} - {current_class['SKJS']}")
    if backup_classes:
        print(f"备选课程数量: {len(backup_classes)}")
    print("按 Ctrl+C 停止脚本。")
    print("="*50 + "\n")

    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "batchId": batch_id,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    def build_body(clazz):
        body = {
            "clazzType": "FANKC",
            "clazzId": clazz['JXBID'],
            "secretVal": clazz['secretVal']
        }
        return urlencode(body, quote_via=quote_plus)

    encoded_body = build_body(current_class)

    try:
        while True:
            try:
                response = session.post(URL_ADD_CLASS, headers=headers, data=encoded_body, timeout=5)
                
                result = response.json()
                code = result.get('code')
                msg = result.get('msg', '没有消息')

                logging.info(f"[{current_class['KCM']} - {current_class['SKJS']}] {code}: {msg}")

                # 课程已满，尝试切换到备选课程
                if msg.find("满") != -1:
                    print(f"\n[!] 当前课程 [{current_class['KCM']} - {current_class['SKJS']}] 已满")
                    
                    # 尝试切换到下一个备选课程
                    if current_index < len(course_queue) - 1:
                        current_index += 1
                        current_class = course_queue[current_index]
                        encoded_body = build_body(current_class)
                        print(f"[->] 切换到备选课程 {current_index}: {current_class['KCM']} - {current_class['SKJS']}")
                        logging.info(f"切换到备选课程: {current_class['KCM']}")
                    else:
                        print("[!] 所有课程（含备选）均已满，继续尝试最后一个课程...")

                # 根据 code 决定输出格式
                if code != 403:
                    # 403 通常是高频请求限制，不重要
                    dt = datetime.datetime.now()
                    course_name = current_class['KCM'][:10]  # 截断显示
                    print(f"[{course_name}] 状态: {code}, 信息: {msg} {dt}", end='\r')
                
                # 如果是成功或其他需要注意的错误，换行并打印完整 JSON
                if code not in [500, 403, 0]: # 500 是常见系统错误, 403 是频率限制, 0 是已满
                    print(f"\n收到关键响应: {json.dumps(result, ensure_ascii=False)}")
                
                # 成功选课 code == 200
                if code == 200:
                    print(f"\n[✓] 选课成功！课程: {current_class['KCM']} - {current_class['SKJS']}")
                    logging.info(f"选课成功: {current_class['KCM']}")
                    break
                
            except requests.exceptions.RequestException as e:
                print(f"请求失败: {e}", end='\r')
            except json.JSONDecodeError:
                print("无法解析服务器响应。", end='\r')

            time.sleep(0.3) # 可调整请求间隔，避免过快

    except KeyboardInterrupt:
        print("\n\n脚本已由用户手动停止。")
        input("按回车键退出...")
        sys.exit(0)

# --- 主程序 ---
def main():
    parser = argparse.ArgumentParser(description="NUIST 教务系统自动选课脚本")
    parser.add_argument("-u", "--username", required=True, help="学号")
    parser.add_argument("-p", "--password", required=True, help="密码")
    args = parser.parse_args()

    # 使用 Session 保持登录状态
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)

    """
    # 从文件中读取cookies
    try:
        with open(COOKIE_FILE, 'r') as f:
            cookie_string = f.read().strip()
        if not cookie_string:
            print(f"错误: {COOKIE_FILE} 文件为空或不存在。")
            return
        cookies_dict = parse_cookies(cookie_string)
        if not cookies_dict:
            print("错误: 未能从文件中成功解析出任何cookies。请检查格式。")
            return
        print("Cookies已成功加载。")
    except FileNotFoundError:
        print(f"错误: 未找到 {COOKIE_FILE} 文件。请确保它和脚本在同一目录下。")
        return
    except Exception as e:
        print(f"读取或解析cookie文件时出错: {e}")
        return
    
    session.cookies.update(cookies_dict)
    """

    # 0. 登录Vpn
    use_vpn = BASE_URL.startswith("https://client.vpn.nuist.edu.cn")
    if use_vpn:
        client = NuistVPNClient(username=args.username, password=args.password)
        cookies_dict = client.login_and_get_cookies()
        session.cookies.update(cookies_dict)
    

    # 1. 登录
    login_data = login(session, args.username, args.password)
    if not login_data:
        return
        
    token = login_data.get("token")
    campus = login_data.get("student", {}).get("campus")

    # 2. 选择轮次
    batch = choose_elective_batch(login_data)
    if not batch:
        return
    batch_id = batch['code']

    # 3. 启动 WebSocket 心跳
    ws_heartbeat = WebSocketHeartbeat(
        student_id=args.username,
        cookies=dict(session.cookies),
        use_vpn=use_vpn
    )
    ws_heartbeat.start()
    time.sleep(0.3)  # 等待 WebSocket 连接建立

    # 4. 选择课程（支持一次性选择多个，第一个为主选，后面为备选）
    result = choose_class(session, token, batch_id, campus)
    if result is None or result[0] is None:
        return
    selected_classes, all_classes = result
    
    # 第一个为主选课程，其余为备选
    selected_class = selected_classes[0]
    backup_classes = selected_classes[1:] if len(selected_classes) > 1 else []
    
    print(f"\n已选择主课程: {selected_class['KCM']}")
    print(f"  课程: {selected_class['clazzName']}")
    print(f"  教师: {selected_class['SKJS']}")
    print(f"  时间地点: {selected_class['teachingPlace']}")
    print(f"  容量/已选: {selected_class['KRL']}/{selected_class['YXRS']}")
    
    if backup_classes:
        print(f"\n备选课程 ({len(backup_classes)} 个):")
        for i, bc in enumerate(backup_classes):
            print(f"  备选{i+1}: {bc['KCM']} - {bc['SKJS']}")
    
    confirm = input("\n是否确认提交选择？(y/n): ").strip().lower()
    if confirm != 'y':
        print("操作已取消。")
        ws_heartbeat.stop()
        return
    
    # 6. 开始抢课
    try:
        start_grabbing(session, token, batch_id, selected_class, backup_classes)
    finally:
        ws_heartbeat.stop()


if __name__ == "__main__":
    main()