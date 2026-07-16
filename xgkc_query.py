"""
通识选修课查询工具 (XGKC Query)
- 支持多关键词搜索（逗号分隔）
- 按竞争比 (一志愿/课容量) 升序排列
- 仅展示列表，不涉及选课
"""

import requests
import argparse
import base64
import json
import time
import sys
import os
import re
from urllib.parse import urlencode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import ddddocr
from rich.console import Console
from rich.table import Table
from bs4 import BeautifulSoup
from vpnlogin import NuistVPNClient

# --- 全局配置 ---

AES_KEY = "MWMqg2tPcDkxcm11".encode('utf-8')

# OCR 实例（全局复用，避免重复创建）
_ocr_instance = None

def _get_ocr():
    """获取或创建 OCR 实例（懒加载）"""
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance

# API 端点
BASE_URL = "https://client.vpn.nuist.edu.cn/https/webvpn3315a96df5a2811a49489fcebfe8b135dece10c6255d04cc36c652f60ee89b3a/xsxk"
URL_CAPTCHA = f"{BASE_URL}/auth/captcha?enlink-vpn"
URL_LOGIN = f"{BASE_URL}/auth/login?enlink-vpn"
URL_LIST_CLASSES = f"{BASE_URL}/elective/clazz/list?enlink-vpn"
URL_SWITCH_BATCH = f"{BASE_URL}/elective/user?enlink-vpn"
URL_GET_USER_INFO = f"{BASE_URL}/elective/user?enlink-vpn"

# 固定参数
COURSE_TYPE = "XGKC"  # 通识选修课
CAMPUS = "01"  # 固定校区
VPN_COOKIE_FILE = os.path.join(os.path.dirname(__file__), "vpn_cookie.txt")

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


# --- 辅助函数 ---

def get_char_width(char):
    """获取单个字符的显示宽度（中文/全角=2，英文=1）"""
    if '\u4e00' <= char <= '\u9fff' or '\u3000' <= char <= '\u303f' or '\uff00' <= char <= '\uffef':
        return 2
    return 1

def get_display_width(text):
    """计算字符串的实际显示宽度（中文=2，英文=1）"""
    return sum(get_char_width(char) for char in text)

def truncate_by_width(text, max_width, placeholder="..."):
    """按显示宽度截断字符串，确保不超过max_width
    
    Args:
        text: 要截断的字符串
        max_width: 最大显示宽度
        placeholder: 省略号（默认为三个英文点）
    
    Returns:
        截断后的字符串
    """
    if not text:
        return text
    
    current_width = get_display_width(text)
    if current_width <= max_width:
        return text
    
    # 需要为省略号预留空间
    placeholder_width = get_display_width(placeholder)
    target_width = max_width - placeholder_width
    
    if target_width <= 0:
        return placeholder[:max_width]
    
    # 逐字符累加，直到接近目标宽度
    result = ""
    current = 0
    for char in text:
        char_width = get_char_width(char)
        if current + char_width > target_width:
            break
        result += char
        current += char_width
    
    return result + placeholder

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


def save_vpn_cookies(cookies_dict):
    """保存 VPN cookies 到文件"""
    try:
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies_dict.items())
        with open(VPN_COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie_str)
        return True
    except Exception:
        return False


def load_vpn_cookies():
    """从文件加载 VPN cookies，失败返回 None"""
    try:
        if os.path.exists(VPN_COOKIE_FILE):
            with open(VPN_COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookie_str = f.read().strip()
            if cookie_str:
                return parse_cookies(cookie_str)
    except Exception:
        pass
    return None


def get_captcha(session):
    """获取并识别验证码"""
    ocr = _get_ocr()
    while True:
        try:
            headers = {**COMMON_HEADERS}
            response = session.post(URL_CAPTCHA, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("code") == 200:
                captcha_b64 = data["data"]["captcha"]
                uuid = data["data"]["uuid"]
                img_b64 = captcha_b64.split(',')[1]
                img_bytes = base64.b64decode(img_b64)
                
                captcha_text = ocr.classification(img_bytes)
                
                if len(captcha_text) == 4 and captcha_text.isalnum():
                    return captcha_text, uuid
                else:
                    pass
            else:
                pass

        except Exception:
            time.sleep(1)


def login(session, username, password):
    """登录系统并获取 token"""
    while True:
        session.get(BASE_URL) # 建立session
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
        
        try:
            response = session.post(URL_LOGIN, headers=headers, data=urlencode(body), timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") == 200:
                print(f"登录成功！欢迎你，{data['data']['student']['XM']} 同学")
                return data['data']
            else:
                print(f"登录失败，正在重试...")
                time.sleep(1)
        except Exception as e:
            print(f"网络出错，正在重试...")
            time.sleep(1)


def get_user_info_from_api(session, token):
    """通过 API 获取用户信息"""
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    batch_id = None
    try:
        profile_url = f"{BASE_URL}/profile/index.html"
        profile_response = session.get(profile_url, headers={**COMMON_HEADERS}, timeout=10)
        profile_response.raise_for_status()
        
        soup = BeautifulSoup(profile_response.text, 'html.parser')
        for script in soup.find_all('script'):
            script_text = script.string
            if script_text and 'var batch' in script_text:
                match = re.search(r'var\s+batch\s*=\s*(\{.*?\});', script_text, re.DOTALL)
                if match:
                    try:
                        batch_json_str = match.group(1)
                        batch_data = json.loads(batch_json_str)
                        batch_id = batch_data.get("code")
                        if batch_id:
                            break
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    
    try:
        body = f"batchId={batch_id}" if batch_id else ""
        response = session.post(URL_GET_USER_INFO, headers=headers, data=body, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") == 200:
            user_data = data.get("data", {})
            student_info = user_data.get("student", {})
            print(f"欢迎回来，{student_info.get('XM', '未知')} 同学")
            user_data["token"] = token
            return user_data
        else:
            print(f"Token 已失效，请重新登录")
            return None
    except Exception:
        return None


def choose_elective_batch(student_data):
    """让用户选择一个可选的选课轮次"""
    batches = student_data.get("student", {}).get("electiveBatchList", [])
    
    available_batches = [b for b in batches if b.get("canSelect") == "1"]
    
    if not available_batches:
        print("当前没有可选的选课轮次")
        return None

    print("\n可选的选课轮次：")
    for i, batch in enumerate(available_batches):
        batch_name = batch['name']
        type_name = batch.get('typeName', '正选')
        print(f"  [{i+1}] [{type_name}] {batch_name}")
    
    while True:
        try:
            choice = int(input("请输入轮次编号: "))
            if 1 <= choice <= len(available_batches):
                return available_batches[choice-1]
            print("输入无效，请重新选择")
        except ValueError:
            print("请输入正确的数字编号")


def switch_batch(session, token, batch_id):
    """切换到指定轮次"""
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    body = "batchId=" + batch_id
    
    try:
        response = session.post(URL_SWITCH_BATCH, headers=headers, data=body, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("code") == 200
    except Exception:
        return False


def query_courses_by_keyword(session, token, batch_id, keyword, page=1, page_size=200):
    """查询单个关键词的课程列表"""
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Batchid": batch_id,
        "Content-Type": "application/json;charset=UTF-8"
    }
    
    body = {
        "teachingClassType": COURSE_TYPE,
        "pageNumber": page,
        "pageSize": page_size,
        "orderBy": "",
        "campus": CAMPUS,
        "SFYX": "2",
        "KEY": keyword.strip()
    }
    
    try:
        response = session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=20)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 200:
            return [], 0
        
        courses = data.get("data", {}).get("rows", [])
        total = data.get("data", {}).get("total", len(courses))
        return courses, total
        
    except Exception:
        return [], 0


def query_all_courses(session, token, batch_id):
    """查询所有课程（不带关键词），一次性获取全部数据"""
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Batchid": batch_id,
        "Content-Type": "application/json;charset=UTF-8"
    }
    
    # 先查询总数
    body = {
        "teachingClassType": COURSE_TYPE,
        "pageNumber": 1,
        "pageSize": 1,
        "orderBy": "",
        "campus": CAMPUS,
        "SFYX": "2",
        "KEY": ""
    }
    
    try:
        response = session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=20)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 200:
            print(f"课程查询失败，请稍后重试")
            return []
        
        total = data.get("data", {}).get("total", 0)
        if total == 0:
            return []
        
        # 一次性获取所有课程
        print(f"正在获取 {total} 门课程数据...")
        body["pageSize"] = total
        response = session.post(URL_LIST_CLASSES, headers=headers, data=json.dumps(body), timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 200:
            print(f"课程查询失败，请稍后重试")
            return []
        
        courses = data.get("data", {}).get("rows", [])
        return courses
        
    except Exception:
        return []


def query_all_keywords(session, token, batch_id, keywords_str):
    """查询多个关键词并合并去重"""
    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
    
    if not keywords:
        return []
    
    all_courses = []
    seen_ids = set()
    
    for keyword in keywords:
        courses, _ = query_courses_by_keyword(session, token, batch_id, keyword)
        
        for course in courses:
            course_id = course.get("JXBID") or course.get("secretVal")
            if course_id and course_id not in seen_ids:
                seen_ids.add(course_id)
                all_courses.append(course)
        
        time.sleep(0.3)
    
    return all_courses


def calculate_competition_ratio(course):
    """计算竞争比 (一志愿报名人数 / 课容量)"""
    first_volunteer = course.get("numberOfFirstVolunteer", 0) or 0
    capacity = course.get("classCapacity", 1) or 1
    if course.get("SFYX", "") == "1" and (course.get("ZYDJ", "") == "1" or course.get("ZYDJ", "") == 1):
        #input(f"已选课程 {course.get('KCM', '未知课程')}，从一志愿中减去1人 原{first_volunteer/capacity:.2f} 后{(first_volunteer-1)/capacity:.2f}")
        first_volunteer -= 1
    if capacity == 0:
        return 0
    return first_volunteer / capacity


def display_results(courses, page_info=None):
    """展示查询结果
    
    Args:
        courses: 课程列表
        page_info: 分页信息，格式为 (当前页, 每页数量, 总数)
    """
    if not courses:
        print("\n未找到符合条件的课程。")
        return
    
    # 按竞争比升序排序
    sorted_courses = sorted(courses, key=calculate_competition_ratio)
    
    console = Console()
    
    # 构建标题
    if page_info:
        current_page, page_size, total = page_info
        total_pages = (total + page_size - 1) // page_size
        title = f"通识选修课查询结果（第 {current_page}/{total_pages} 页，共 {total} 门）"
    else:
        title = f"通识选修课查询结果（共 {len(sorted_courses)} 门）"
    
    table = Table(title=title, show_header=True)
    table.add_column("id", justify="center", style="cyan", width=3)
    table.add_column("课程名称", style="bold")
    table.add_column("通识类别", style="magenta", width=10)
    table.add_column("教师", width=8)
    table.add_column("  ", justify="center", width=2)
    table.add_column("  ", justify="center", width=2)
    table.add_column("一志愿", justify="right", style="yellow", width=6)
    table.add_column("课容量", justify="right", style="blue", width=6)
    table.add_column("竞争比", justify="right", width=6)
    table.add_column("上课时间地点", style="dim")
    
    # 计算序号偏移
    start_index = 0
    if page_info:
        current_page, page_size, _ = page_info
        start_index = (current_page - 1) * page_size
    
    for i, course in enumerate(sorted_courses):
        name = course.get("KCM", "未知")
        xgxklb = course.get("XGXKLB", "-")
        teacher = course.get("SKJS", "-")
        first_vol = course.get("numberOfFirstVolunteer", 0) or 0
        capacity = course.get("classCapacity", 0) or 0
        ratio = calculate_competition_ratio(course)
        place = course.get("teachingPlace", "-")
        
        # 是否已选 (SFYX: "1" = 已选)
        sfyx = course.get("SFYX", "")
        is_selected = sfyx == "1"
        selected_display = "[bold green]✓[/bold green]" if is_selected else "-"
        
        # 志愿等级
        zydj = course.get("ZYDJ", "")
        volunteer_display = f"[green]{zydj}[/green]" if zydj else "-"
        
        if is_selected and (zydj == "1" or zydj == 1):
            first_vol -= 1  # 已选课程从一志愿中减去

        # 根据竞争比设置颜色
        if ratio < 0.75:
            ratio_style = "green"
        elif ratio < 1.2:
            ratio_style = "yellow"
        else:
            ratio_style = "red"
        
        # 已选课程整行用绿色标注
        row_style = "green" if is_selected else None
        
        # 截断处理：按显示宽度
        name_display = truncate_by_width(name, 22)
        xgxklb_display = xgxklb[:5]  # 固定只显示5个字
        teacher_display = truncate_by_width(teacher, 8)
        place_display = truncate_by_width(place, 30)
        
        table.add_row(
            str(start_index + i + 1),
            f"[green]{name_display}[/green]" if is_selected else name_display,
            xgxklb_display,
            f"[green]{teacher_display}[/green]" if is_selected else teacher_display,
            selected_display,
            volunteer_display,
            str(first_vol),
            str(capacity),
            f"[{ratio_style}]{ratio:.3f}[/{ratio_style}]",
            place_display
        )
    
    os.system('cls' if os.name == 'nt' else 'clear')
    console.print()
    console.print(table)
    console.print("注意: 已选一志愿与竞争比均已剔除本人数据")
    
    # 分页提示
    if page_info:
        console.print("[dim]n=下一页 p=上一页 g<页码>=跳转 q=退出[/dim]")


def main():
    parser = argparse.ArgumentParser(description="通识选修课查询工具")
    parser.add_argument("-u", "--username", help="学号")
    parser.add_argument("-p", "--password", help="密码")
    parser.add_argument("-t", "--token", help="直接使用 Token（跳过登录）")
    parser.add_argument("-c", "--cookie", help="Cookie 文件路径")
    parser.add_argument("--use-vpn", action="store_true", default=True, help="使用 VPN（默认开启）")
    parser.add_argument("--no-vpn", action="store_true", help="不使用 VPN")
    args = parser.parse_args()
    
    use_vpn = not args.no_vpn
    
    session = requests.Session()
    session.verify = True
    
    # VPN 登录
    if use_vpn:
        # 尝试复用已保存的 VPN cookie
        vpn_cookies = load_vpn_cookies()
        if vpn_cookies:
            session.cookies.update(vpn_cookies)
            response = session.get(BASE_URL)  # 验证 cookie 是否有效
            if 'login' in response.url:
                print("保存的 VPN Cookie 已失效，重新登录 VPN...")
                os.remove(VPN_COOKIE_FILE)
                login_flag = 1
            else:
                print("[✓] 已加载保存的 VPN Cookie")
                login_flag = 0
        else:
            print("正在登录 VPN...")
            login_flag = 1
        if login_flag == 1:
            client = NuistVPNClient(args.username, args.password)
            cookies_dict = client.login_and_get_cookies()
            session.cookies.update(cookies_dict)
            save_vpn_cookies(cookies_dict)
            print("[✓] VPN 登录成功")
    
    # 加载 Cookie 文件
    if args.cookie:
        cookie_path = args.cookie if os.path.isabs(args.cookie) else os.path.join(os.path.dirname(__file__), args.cookie)
        try:
            with open(cookie_path, 'r', encoding='utf-8') as f:
                cookie_string = f.read().strip()
            cookies_dict = parse_cookies(cookie_string)
            session.cookies.update(cookies_dict)
            print(f"[✓] Cookie 文件已加载")
        except Exception:
            pass
    
    # 登录选课系统
    student_data = None
    token = None
    
    if args.token:
        token = args.token
        student_data = get_user_info_from_api(session, token)
        if not student_data:
            print("登录 Token 已失效，请重新登录或检查 Token 是否正确")
            return
    else:
        username = args.username or input("请输入学号: ")
        password = args.password or input("请输入密码: ")
        student_data = login(session, username, password)
        if not student_data:
            print("登录失败，请检查学号密码是否正确或网络连接是否正常")
            return
        token = student_data.get("token")
    
    # 选择轮次
    selected_batch = choose_elective_batch(student_data)
    if not selected_batch:
        return
    
    batch_id = selected_batch['code']
    
    # 切换轮次
    if not switch_batch(session, token, batch_id):
        print("切换选课轮次失败，请稍后重试")
        return
    
    # 查询循环
    PAGE_SIZE = 15
    current_page = 1
    is_paging_mode = False
    all_courses_cache = []
    
    while True:
        if is_paging_mode:
            total_courses = len(all_courses_cache)
            total_pages = (total_courses + PAGE_SIZE - 1) // PAGE_SIZE
            user_input = input(f"第 {current_page}/{total_pages} 页 (n=下一页, p=上一页, g<页码>=跳转, q=退出): ").strip()
        else:
            user_input = input("请输入课程关键词 (直接回车查看全部课程, q 退出): ").strip()
        
        if user_input.lower() == 'q':
            break
        
        # 分页模式下的导航
        if is_paging_mode:
            total_courses = len(all_courses_cache)
            total_pages = (total_courses + PAGE_SIZE - 1) // PAGE_SIZE
            
            if user_input == '' or user_input.lower() == 'n':
                if current_page < total_pages:
                    current_page += 1
            elif user_input.lower() == 'p':
                if current_page > 1:
                    current_page -= 1
            elif user_input.lower().startswith('g '):
                try:
                    target_page = int(user_input[2:].strip())
                    if 1 <= target_page <= total_pages:
                        current_page = target_page
                except ValueError:
                    pass
            else:
                is_paging_mode = False
                all_courses_cache = []
        
        if not is_paging_mode:
            if user_input == '':
                raw_courses = query_all_courses(session, token, batch_id)
                if not raw_courses:
                    print("当前没有可选的通识选修课程")
                    continue
                all_courses_cache = sorted(raw_courses, key=calculate_competition_ratio)
                is_paging_mode = True
                current_page = 1
            else:
                courses = query_all_keywords(session, token, batch_id, user_input)
                if not courses:
                    print("未找到匹配的课程，请尝试其他关键词")
                    continue
                sorted_courses = sorted(courses, key=calculate_competition_ratio)
                if len(sorted_courses) > PAGE_SIZE:
                    all_courses_cache = sorted_courses
                    is_paging_mode = True
                    current_page = 1
                else:
                    display_results(sorted_courses)
                    continue
        
        # 分页模式：从缓存中取当前页数据
        if is_paging_mode and all_courses_cache:
            total_courses = len(all_courses_cache)
            start_idx = (current_page - 1) * PAGE_SIZE
            end_idx = start_idx + PAGE_SIZE
            page_courses = all_courses_cache[start_idx:end_idx]
            display_results(page_courses, (current_page, PAGE_SIZE, total_courses))


if __name__ == "__main__":
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    main()
