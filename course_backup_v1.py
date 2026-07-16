import requests
import argparse
import base64
import json
import time
import sys
import datetime
import logging
from urllib.parse import urlencode, quote_plus
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import ddddocr
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

        print("\n--- 请选择要抢的课程 ---")
        for i, clazz in enumerate(all_classes):
            print(f"  [{i+1}] 课程: {clazz['KCM']} ({clazz['clazzName']})")
            print(f"      教师: {clazz['SKJS']}")
            print(f"      时间地点: {clazz['teachingPlace']}")
            print(f"      容量/已选: {clazz['KRL']}/{clazz['YXRS']}\n")

        while True:
            try:
                choice = int(input("请输入数字序号选择课程: "))
                if 1 <= choice <= len(all_classes):
                    return all_classes[choice - 1]
                else:
                    print("无效的输入，请输入列表中的数字。")
            except ValueError:
                print("请输入一个有效的数字。")

    except Exception as e:
        print(f"获取课程列表时发生错误: {e}")
        return None

def start_grabbing(session, token, batch_id, selected_class):
    """开始循环抢课"""
    class_id = selected_class['JXBID']
    secret_val = selected_class['secretVal']

    print("\n" + "="*50)
    print(f"准备抢课: {selected_class['KCM']} - {selected_class['SKJS']}")
    print("按 Ctrl+C 停止脚本。")
    print("="*50 + "\n")

    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "batchId": batch_id,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    body = {
        "clazzType": "FANKC",
        "clazzId": class_id,
        "secretVal": secret_val
    }
    # 对 Body 进行 URL 编码
    encoded_body = urlencode(body, quote_via=quote_plus)

    try:
        while True:
            try:
                response = session.post(URL_ADD_CLASS, headers=headers, data=encoded_body, timeout=5)
                # response.raise_for_status()
                
                result = response.json()
                code = result.get('code')
                msg = result.get('msg', '没有消息')

                logging.info(f"{code}: {msg}")

                # 根据 code 决定输出格式
                if code != 403:
                    # 403 通常是高频请求限制，不重要
                    dt = datetime.datetime.now()
                    print(f"状态: {code}, 信息: {msg} {dt}", end='\r')
                
                # 如果是成功或其他需要注意的错误，换行并打印完整 JSON
                if code not in [500, 403]: # 500 是常见系统错误, 403 是频率限制
                    print(f"\n收到关键响应: {json.dumps(result, ensure_ascii=False)}")
                
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
    if BASE_URL.startswith("https://client.vpn.nuist.edu.cn"):
        client = NuistVPNClient(username=args.username,password=args.password)
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

    # 3. 选择课程
    selected_class = choose_class(session, token, batch_id, campus)
    if not selected_class:
        return
    
    print(f"已选择: {selected_class['KCM']}")
    print(f"  课程: {selected_class['clazzName']}")
    print(f"  教师: {selected_class['SKJS']}")
    print(f"  时间地点: {selected_class['teachingPlace']}")
    print(f"  容量/已选: {selected_class['KRL']}/{selected_class['YXRS']}\n")
    confirm = input("是否确认提交选择？(y/n): ").strip().lower()
    if confirm != 'y':
        print("操作已取消。")
        return
    
    # 4. 开始抢课
    start_grabbing(session, token, batch_id, selected_class)


if __name__ == "__main__":
    main()