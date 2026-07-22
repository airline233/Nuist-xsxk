import requests
import json
import time
import datetime
import threading
import truststore
from urllib.parse import urlencode, quote_plus
from rich.console import Console
from rich.table import Table
from rich.text import Text

from common import (
    COURSE_TYPE_FANKC,
    COURSE_TYPE_TYKC,
    BASE_URL,
    URL_LIST_CLASSES,
    URL_ADD_CLASS,
    URL_DEL_CLASS,
    URL_SWITCH_BATCH,
    COMMON_HEADERS,
    main_logger,
    heartbeat_logger,
    setup_logging,
    login_state,
    _is_auth_failure,
    _get_live_token,
    _is_full_msg,
    _is_already_selected_msg,
    _extract_student_id,
    _extract_ws_clazz_id,
    handle_relogin,
    HttpHeartbeat,
    WebSocketHeartbeat,
    load_cookie_file,
    init_vpn_login,
    init_login,
    parse_arguments,
    validate_arguments,
)



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