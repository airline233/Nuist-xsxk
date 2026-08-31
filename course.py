"""NUIST 正选抢课脚本。

核心认知：/add 只是进入选课队列，真正选上靠 WebSocket 推「选课成功」；
满员同样靠 WS 回调，立刻切备选。基建（登录/重登/双心跳等）在 common.py。
"""
import requests
import json
import time
import sys
import datetime
import threading
import re
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
    _is_hard_fail_msg,
    _extract_student_id,
    _extract_ws_clazz_id,
    handle_relogin,
    resolve_course_type,
    HttpHeartbeat,
    WebSocketHeartbeat,
    load_cookie_file,
    init_vpn_login,
    init_login,
    parse_arguments,
    validate_arguments,
)


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
    else:
        COLS = 4

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

        # 检查是否所有教学班都已满
        all_full = True
        all_ct = True
        if_yx = False
        unable_cnt = 0
        for clazz in tc_list:
            if clazz.get('SFYM', '0') == '1' or clazz.get('SFCT', '0') == '1':
                unable_cnt += 1
            if clazz.get('SFYM', '0') != '1':  # SFYM=1表示已满
                all_full = False
            if clazz.get('SFCT', '0') != '1':  # SFCT=1表示与已选冲突
                all_ct = False
            if clazz.get('SFYX', '0') == '1':  # SFYX=1表示已选
                if_yx = True

        text = Text()
        text.append(f"[{idx}]", style="bold cyan")
        text.append(f"{course_name}", style="bold")
        if if_yx:
            text.append(" (已选)", style="bold green")
        else:
            flag = 0
            if all_full and tc_list:
                text.append(" (已满)", style="bold red")
                flag = 1
            if all_ct and tc_list:
                text.append(" (冲突)", style="bold red")
                flag = 1
            if flag == 0 and unable_cnt == len(tc_list) and tc_list:
                text.append(f" (无可选课程)", style="bold red")

        text.append("\n")

        # 显示教学班数量
        text.append(f"({tc_count}个教学班)", style="dim")
        text.append("\n")
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


def choose_elective_batch(student_data, session=None, token=None):
    """让用户选择一个可选的选课轮次

    课程类型由服务端 menuList 决定（见 resolve_course_type），不按轮次名猜。

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
        print(f"  [{i+1}] {batch['name']} ({batch['beginTime']} - {batch['endTime']})")
    print("  [0] 退出程序")

    while True:
        try:
            choice = int(input("请输入数字序号选择轮次 (0退出): "))
            if choice == 0:
                print("已退出。")
                return None, None
            if 1 <= choice <= len(available_batches):
                selected_batch = available_batches[choice-1]
                course_type = resolve_course_type(
                    session, token, selected_batch['code'], selected_batch.get('name', '')
                )
                if not course_type:
                    # 用户在类型选择里选了返回，重新选轮次
                    print("\n返回轮次选择...")
                    continue
                return selected_batch, course_type
            else:
                print("无效的输入，请输入列表中的数字。")
        except ValueError:
            print("请输入一个有效的数字。")

def choose_class(session, token, batch_id, campus, course_type=COURSE_TYPE_FANKC):
    """获取课程列表并让用户选择

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
            return "BACK", None

        # 课程选择循环（支持返回上一级）
        while True:
            # 判断是否需要选择课程：
            # 1. 如果只有一门课程，直接展开
            # 2. 如果有多门不同的课程，检查总教学班数量
            # 3. 如果总教学班数量较少（<10个），自动全部展开展示
            should_expand_all = False
            if len(courses) == 1:
                # 只有一门课程，直接展开
                all_classes = courses[0].get("tcList", [])
                selected_course = None  # 标记为单课程模式
            else:
                # 多门课程
                total_classes = sum(len(course.get("tcList", [])) for course in courses)
                should_expand_all = total_classes < 10  # 总教学班少于10个时自动展开

                if should_expand_all:
                    # print(f"\n总计 {total_classes} 个教学班，自动全部展开展示")
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

            if not all_classes:
                print("在此轮次下未找到可选的课程。")
                return None, None

            # 选择教学班
            result = _choose_teaching_class(all_classes, course_type, has_multi_courses=(len(courses) > 1), is_auto_expand=should_expand_all)
            if result == "BACK":
                if len(courses) > 1 and not should_expand_all:
                    # 多课程且非自动展开模式，返回课程选择
                    continue
                else:
                    # 单课程模式或自动展开模式，返回上一级（轮次选择）
                    return "BACK", None
            return result, all_classes

    except Exception as e:
        print(f"-获取课程列表时发生错误: {e}")
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

        # 获取状态标记
        sfct = clazz.get('SFCT', '0')  # 是否冲突：1=冲突，0=不冲突
        sfyx = clazz.get('SFYX', '0')  # 是否已选：1=已选，0=未选
        sfym = clazz.get('SFYM', '0')  # 是否已满：1=已满，0=未满

        if is_pe_course:
            # 体育课：显示项目名和分类
            project_name = clazz.get('projectName', '')
            classification = clazz.get('classificationName', '')
            # 分类样式：选项课绿色，锻炼课黄色
            class_style = "green" if classification == "选项课" else "yellow"

            # 添加课程名称
            text.append(f"{project_name}", style="bold")

            # 添加状态标记
            if sfyx == '1':
                text.append("(已选)", style="green bold")
            elif sfym == '1':
                text.append("(已满)", style="red bold")
            elif sfct == '1':
                text.append("(冲突)", style="red bold")

            text.append(f" ({classification})\n", style=class_style)
            text.append(f"{clazz['SKJS']} ", style="green")
            text.append(f"[{clazz['YXRS']}/{clazz['KRL']}]\n", style="yellow")
        else:
            # 方案内课程：原有显示逻辑
            text.append(f"{clazz['KCM']}", style="bold")

            # 添加状态标记
            if sfyx == '1':
                text.append("(已选)", style="green bold")
            elif sfym == '1':
                text.append("(已满)", style="red bold")
            elif sfct == '1':
                text.append("(冲突)", style="red bold")
            text.append("\n")

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
            text.append("\n")
        except Exception as e:
            print(f"处理时间地点信息时发生错误: {e}")
        return text

    # 将课程分组并添加到表格
    for i in range(0, total, COLS):
        try:
            row_cells = []
            for j in range(COLS):
                idx = i + j
                if idx < total:
                    row_cells.append(build_cell(idx + 1, all_classes[idx]))
                else:
                    row_cells.append("")  # 空单元格
            table.add_row(*row_cells)
        except Exception as e:
            print(f"添加课程行时发生错误: {e}")

    console.print(table)
    back_hint = "返回上一级"
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
            else:
                print("未选择任何有效教学班，请重新输入。")

        except ValueError:
            print("输入格式错误，请输入数字序号。")

def start_grabbing(session, token, batch_id, selected_class, backup_classes=None,
                   course_type=COURSE_TYPE_FANKC, ws_heartbeat=None, http_heartbeat=None):
    """开始循环抢课，支持备选课程切换

    Args:
        course_type: 课程类型，COURSE_TYPE_FANKC(方案内课程) 或 COURSE_TYPE_TYKC(体育课)
        ws_heartbeat: WebSocketHeartbeat 实例，用于监听选课成功消息
        http_heartbeat: HttpHeartbeat 实例，重登后同步 token

    Note:
        /add 端点只是加入选课队列，真正选课成功时会通过 WebSocket 返回响应。
        因此需要同时监听 WebSocket 消息来判断选课是否成功。
        WS 等待有超时，超时后继续投递 /add，避免死等。
    """
    if backup_classes is None:
        backup_classes = []

    # 构建课程队列：主课程 + 备选课程
    course_queue = [selected_class] + backup_classes

    # 使用可变对象来在回调和主循环之间共享状态
    # last_switched_index: 已切离的下标，防止同 index 双通道连跳
    # awaiting_ws / awaiting_jxbid: 仅对当前入队等待的课响应迟到 WS，避免跳过备选
    state = {
        "current_index": 0,
        "need_switch": False,
        "last_switched_index": -1,
        "ws_fail": False,  # 非“已满”的其它失败，超时后继续投递
        "awaiting_ws": False,
        "awaiting_jxbid": None,
        "failed_jxbids": set(),
    }
    state_lock = threading.Lock()

    current_class = course_queue[state["current_index"]]

    # 获取课程显示名称（体育课用项目名，方案内课程用课程名）
    def get_display_name(clazz):
        if course_type == COURSE_TYPE_TYKC:
            return f"{clazz.get('projectName', clazz['KCM'])}({clazz.get('classificationName', '')})"
        return clazz['KCM']

    def switch_to_next(reason=""):
        """统一的备选切换（同一门课只切一次）"""
        with state_lock:
            idx = state["current_index"]
            if idx < len(course_queue):
                state["failed_jxbids"].add(str(course_queue[idx]["JXBID"]))
            if idx >= len(course_queue) - 1:
                print(f"[!] 所有课程（含备选）均已失败/已满，继续尝试最后一个课程... ({reason})")
                state["need_switch"] = False
                state["awaiting_ws"] = False
                state["awaiting_jxbid"] = None
                return False
            if state["last_switched_index"] == idx:
                # 已经因这门课切过，忽略重复触发
                state["need_switch"] = False
                return False
            state["last_switched_index"] = idx
            state["current_index"] = idx + 1
            state["need_switch"] = False
            state["awaiting_ws"] = False
            state["awaiting_jxbid"] = None
            new_class = course_queue[state["current_index"]]
            print(
                f"[->] 切换到备选课程 {state['current_index']}: "
                f"{get_display_name(new_class)} - {new_class['SKJS']} ({reason})"
            )
            main_logger.info(f"切换到备选课程({reason}): {get_display_name(new_class)}")
            return True

    print("\n" + "="*50)
    print(f"准备抢课: {get_display_name(current_class)} - {current_class['SKJS']}")
    if backup_classes:
        print(f"备选课程数量: {len(backup_classes)}")
    print("按 Ctrl+C 停止脚本。")
    print("="*50 + "\n")

    # 使用当前token（可能会在重登后更新）
    current_token = _get_live_token(token)

    # 用于存储 WebSocket 通知的选课成功信息
    ws_success_event = threading.Event()
    ws_success_info = {"clazz_id": None, "course_name": None}

    def on_ws_course_success(clazz_id, course_name, msg, data):
        """WebSocket 选课成功回调。

        必须 clazz_id 非空且在队列内才 set event；空 id 只 log，不 break。
        """
        all_clazz_ids = {str(c['JXBID']) for c in course_queue}
        cid = str(clazz_id) if clazz_id else ""
        if not cid:
            print(f"\n[WebSocket] 收到选课成功但 clazzId 为空，忽略: {course_name} ({msg})")
            main_logger.warning(f"[WebSocket] 空 clazzId 成功消息，忽略: course={course_name}, msg={msg}")
            return
        if cid in all_clazz_ids:
            ws_success_info["clazz_id"] = cid
            ws_success_info["course_name"] = course_name
            ws_success_event.set()
            print(f"\n[WebSocket] ✅ 确认选课成功: {course_name} (clazzId={cid})")
            main_logger.info(f"[WebSocket] 确认选课成功: {course_name}, clazzId={cid}")
        else:
            main_logger.info(f"[WebSocket] 忽略非队列课程成功: clazzId={cid}, course={course_name}")

    def on_ws_course_fail(code, msg, data):
        """WebSocket 选课失败回调。

        绑定失败课的 JXBID / 当前入队等待状态，避免
        「HTTP 已切到备选 + 迟到 WS 满消息」再次 switch 连跳。
        """
        fail_id = _extract_ws_clazz_id(data)
        if fail_id:
            fail_id = str(fail_id)

        with state_lock:
            idx = state["current_index"]
            current_clazz = course_queue[idx] if idx < len(course_queue) else None
            cur_id = str(current_clazz["JXBID"]) if current_clazz else None
            name = get_display_name(current_clazz) if current_clazz else "?"
            awaiting = state.get("awaiting_ws")
            await_id = state.get("awaiting_jxbid")
            if await_id is not None:
                await_id = str(await_id)

            # 迟到/错课消息：不是当前课、也不是正在等 WS 的那门 → 忽略切换
            if fail_id:
                if fail_id in state["failed_jxbids"] and fail_id != cur_id and fail_id != await_id:
                    main_logger.info(f"[WebSocket] 忽略已切离课程的失败: {fail_id} ({msg})")
                    return
                if fail_id != cur_id and fail_id != await_id:
                    state["failed_jxbids"].add(fail_id)
                    main_logger.info(
                        f"[WebSocket] 忽略非当前课失败: fail={fail_id}, current={cur_id}, awaiting={await_id}, msg={msg}"
                    )
                    return
            else:
                # 无 clazzId：仅在仍处于入队等待时采纳，防止切课后迟到空 id 再切
                if not awaiting:
                    main_logger.info(f"[WebSocket] 忽略非等待期空ID失败: {msg}")
                    return

            if _is_already_selected_msg(msg):
                # 已选上视作成功
                print(f"\n[WebSocket] ✅ 已选该课，视为成功: {msg}")
                main_logger.info(f"[WebSocket] 已选上视为成功: {name}, msg={msg}")
                if cur_id:
                    ws_success_info["clazz_id"] = cur_id
                ws_success_info["course_name"] = name
                ws_success_event.set()
                return

            if _is_full_msg(msg):
                print(f"\n[WebSocket] ⚠️ 当前课程 [{name}] 已满: {msg}")
                main_logger.warning(f"[WebSocket] 课容量已满: {name}")
                if cur_id:
                    state["failed_jxbids"].add(cur_id)
                state["need_switch"] = True
            elif _is_hard_fail_msg(msg):
                print(f"\n[WebSocket] ⚠️ 硬失败，切换备选: {msg}")
                main_logger.warning(f"[WebSocket] 硬失败: {name}, msg={msg}")
                if cur_id:
                    state["failed_jxbids"].add(cur_id)
                state["need_switch"] = True
            else:
                # 其它失败：不立刻切备选，标记后由超时路径继续投递
                print(f"\n[WebSocket] ⚠️ 选课失败: {msg}")
                main_logger.warning(f"[WebSocket] 选课失败: {msg}")
                state["ws_fail"] = True

    # 注册 WebSocket 回调
    if ws_heartbeat:
        ws_heartbeat.set_success_callback(on_ws_course_success)
        ws_heartbeat.set_fail_callback(on_ws_course_fail)
        ws_heartbeat.clear_success_messages()

    def build_headers(tk):
        return {
            **COMMON_HEADERS,
            "Authorization": tk,
            "batchId": batch_id,
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def build_body(clazz):
        secret = clazz.get('secretVal')
        if not secret:
            main_logger.warning(
                f"课程缺少 secretVal，跳过本轮: JXBID={clazz.get('JXBID')}, "
                f"KCM={clazz.get('KCM')}"
            )
            print(f"\n[!] 课程缺少 secretVal，跳过本轮: {clazz.get('JXBID')}")
            return None
        body = {
            "clazzType": course_type,
            "clazzId": clazz['JXBID'],
            "secretVal": secret
        }
        return urlencode(body, quote_via=quote_plus)

    def refresh_after_relogin(new_token):
        """重登后同步 token / headers / 心跳 / WS cookie"""
        nonlocal current_token, headers
        current_token = new_token
        login_state.token = new_token
        headers = build_headers(current_token)
        if http_heartbeat is not None:
            http_heartbeat.update_token(new_token)
        if ws_heartbeat and login_state.session is not None:
            ws_heartbeat.update_cookies(dict(login_state.session.cookies))

    headers = build_headers(current_token)
    encoded_body = build_body(current_class)

    # 连续JSON解析失败计数
    json_fail_count = 0
    MAX_JSON_FAIL = 3
    WS_WAIT_TIMEOUT = 5.0  # 入队后等待 WS 确认的最长时间

    try:
        while True:
            # 每轮同步最新 token（心跳重登后生效）
            live = _get_live_token(current_token)
            if live and live != current_token:
                refresh_after_relogin(live)

            current_class = course_queue[state["current_index"]]
            encoded_body = build_body(current_class)
            if encoded_body is None:
                # 缺 secretVal：切下一门，否则 sleep 继续
                if switch_to_next("缺secretVal"):
                    continue
                time.sleep(1)
                continue

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
                        main_logger.warning(f"连续{MAX_JSON_FAIL}次JSON解析失败，开始重登流程")
                        success, new_token = handle_relogin(response)
                        if success and new_token:
                            refresh_after_relogin(new_token)
                            json_fail_count = 0
                            print("[✓] 重新登录成功，继续抢课...")
                        else:
                            print("[✗] 重新登录失败，等待后重试...")
                            time.sleep(5)

                    time.sleep(0.5)
                    continue

                code = result.get('code')
                msg = str(result.get('msg', '没有消息'))
                main_logger.info(f"[{get_display_name(current_class)} - {current_class['SKJS']}] {code}: {msg}")

                # 鉴权失败 → 重登
                if _is_auth_failure(code, msg):
                    print(f"\n[!] 鉴权失败(code={code}): {msg}，尝试重登...")
                    success, new_token = handle_relogin(response)
                    if success and new_token:
                        refresh_after_relogin(new_token)
                        print("[✓] 重新登录成功，继续抢课...")
                    else:
                        print("[✗] 重新登录失败，等待后重试...")
                        time.sleep(5)
                    continue

                # 已选上 / 重复选课 → 视为成功
                if _is_already_selected_msg(msg):
                    print(f"\n[✓] 已选该课，视为成功: {msg}")
                    main_logger.info(f"已选上视为成功: {get_display_name(current_class)}, msg={msg}")
                    break

                # 课程已满：仅在业务失败语境（非 200/非鉴权 403）才 switch
                # 避免 200 入队文案或限频 403 误伤
                is_biz_fail_code = (code in (500,) or (code not in (200, None) and code != 403))
                if _is_full_msg(msg) and is_biz_fail_code:
                    print(f"\n[!] 当前课程 [{get_display_name(current_class)} - {current_class['SKJS']}] 已满 (code={code})")
                    if switch_to_next("HTTP满"):
                        continue
                    # 已是最后一门：继续投递
                    time.sleep(0.3)
                    continue

                # 硬失败（冲突/限选/先修/学分等）→ switch，不无限空转
                if _is_hard_fail_msg(msg) and is_biz_fail_code:
                    print(f"\n[!] 硬失败，切换备选: {msg} (code={code})")
                    main_logger.warning(f"硬失败: {get_display_name(current_class)}, msg={msg}")
                    if switch_to_next("HTTP硬失败"):
                        continue
                    # 已是最后一门：打印后 sleep 再试，避免 tight loop
                    time.sleep(1)
                    continue

                # 根据 code 决定输出格式
                if code != 403:
                    dt = datetime.datetime.now()
                    course_name = get_display_name(current_class)[:10]
                    print(f"[{course_name}] 状态: {code}, 信息: {msg} {dt}", end='\r')

                if code not in [500, 403, 0]:
                    print(f"\n收到关键响应: {json.dumps(result, ensure_ascii=False)}")

                # /add 的 code==200 只表示入队，等 WS 确认
                if code == 200:
                    print(f"\n[→] 已加入选课队列: {get_display_name(current_class)} - {current_class['SKJS']}")
                    main_logger.info(f"已加入选课队列: {get_display_name(current_class)}")
                    print(f"[⏳] 等待 WebSocket 响应（最长 {WS_WAIT_TIMEOUT:.0f}s）...")

                    with state_lock:
                        state["ws_fail"] = False
                        state["awaiting_ws"] = True
                        state["awaiting_jxbid"] = str(current_class["JXBID"])

                    try:
                        deadline = time.time() + WS_WAIT_TIMEOUT
                        while time.time() < deadline:
                            if ws_success_event.is_set():
                                break
                            with state_lock:
                                need_sw = state["need_switch"]
                                failed = state["ws_fail"]
                            if need_sw or failed:
                                break
                            # 轮询成功消息（防回调丢失）；忽略空 clazz_id
                            if ws_heartbeat:
                                for clazz_id in [str(c['JXBID']) for c in course_queue]:
                                    if not clazz_id:
                                        continue
                                    success_msgs = ws_heartbeat.check_success(clazz_id)
                                    if success_msgs:
                                        m = success_msgs[0]
                                        ws_success_info["clazz_id"] = clazz_id
                                        ws_success_info["course_name"] = m.get("course_name")
                                        ws_success_event.set()
                                        break
                                if ws_success_event.is_set():
                                    break
                            time.sleep(0.1)
                    finally:
                        with state_lock:
                            state["awaiting_ws"] = False
                            # 保留 awaiting_jxbid 到 switch 清掉，便于迟到带 id 的满消息仍可匹配当次入队课

                    if ws_success_event.is_set():
                        success_name = ws_success_info.get("course_name", get_display_name(current_class))
                        print(f"\n[✓] 选课成功！课程: {success_name} (通过WebSocket确认)")
                        main_logger.info(f"选课成功(WebSocket): {success_name}")
                        break

                    # 需要切备选
                    with state_lock:
                        need_sw = state["need_switch"]
                    if need_sw:
                        if switch_to_next("WS满/硬失败"):
                            continue
                        # 已是最后一门：继续投递
                        continue

                    # 超时或其它失败：继续下一轮 /add（不退出）
                    print(f"[!] WS 未确认成功，继续投递 /add ...")
                    main_logger.info("WS 等待超时/失败，继续投递")
                    continue

            except requests.exceptions.RequestException as e:
                print(f"请求失败: {e}", end='\r')

            # 处理 WS 异步触发的切换（非 200 入队等待路径）
            with state_lock:
                need_sw = state["need_switch"]
            if need_sw:
                if switch_to_next("WS异步满/硬失败"):
                    continue

            if ws_success_event.is_set():
                success_name = ws_success_info.get("course_name", get_display_name(current_class))
                print(f"\n[✓] 选课成功！课程: {success_name} (通过WebSocket确认)")
                main_logger.info(f"选课成功(WebSocket确认): {success_name}")
                break

            if ws_heartbeat:
                for clazz_id in [str(c['JXBID']) for c in course_queue]:
                    if not clazz_id:
                        continue
                    success_msgs = ws_heartbeat.check_success(clazz_id)
                    if success_msgs:
                        m = success_msgs[0]
                        print(f"\n[✓] 选课成功！课程: {m['course_name']} (通过WebSocket轮询确认)")
                        main_logger.info(f"选课成功(WebSocket轮询): {m['course_name']}")
                        ws_success_event.set()
                        break
                if ws_success_event.is_set():
                    break

            time.sleep(0.3)

    except KeyboardInterrupt:
        print("\n\n抢课已停止。")
        while True:
            choice = input("输入 0 返回主菜单，按回车键退出: ").strip()
            if choice == "0":
                return "BACK"
            elif choice == "":
                sys.exit(0)
            else:
                print("无效输入，请输入 0 或直接按回车。")
    finally:
        if ws_heartbeat:
            ws_heartbeat.set_success_callback(None)
            ws_heartbeat.set_fail_callback(None)

    # 抢课成功后，询问是否返回主菜单或退出
    while True:
        choice = input("\n抢课成功！输入 0 返回主菜单，按回车键退出: ").strip()
        if choice == "0":
            return "BACK"
        elif choice == "":
            sys.exit(0)
        else:
            print("无效输入，请输入 0 或直接按回车。")


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

    teaching_place = selected_class.get('teachingPlace') or '未安排地点'
    print(f"\n已选择主课程: {get_display_name(selected_class)} - {selected_class['SKJS']} [{selected_class['YXRS']}/{selected_class['KRL']}]\n\t{teaching_place}")

    if backup_classes:
        print(f"\n备选课程 ({len(backup_classes)} 个):")
        for i, bc in enumerate(backup_classes):
            bc_place = bc.get('teachingPlace') or '未安排地点'
            print(f"  备选{i+1}: {get_display_name(bc)} - {bc['SKJS']} [{bc['YXRS']}/{bc['KRL']}]\n\t{bc_place}")

    confirm = input("\n是否确认提交选择？(y/n): ").strip().lower()
    return confirm == 'y', selected_class, backup_classes


def run_course_selection(session, token, batch_id, campus, username, use_vpn, course_type=COURSE_TYPE_FANKC, login_data=None):
    """运行课程选择和抢课流程

    Args:
        session: requests.Session 对象
        token: 认证token
        batch_id: 选课批次ID
        campus: 校区
        username: 学号（用于WebSocket）
        use_vpn: 是否使用VPN
        course_type: 课程类型
        login_data: 登录数据（用于兜底提取学号）

    Returns:
        str or None: "BACK" 表示返回轮次选择，None 表示正常结束
    """
    # 解析有效学号：参数 > login_state > login_data 提取
    student_id = username or login_state.username or _extract_student_id(login_data)
    if not student_id or str(student_id).strip() in ("", "None", "unknown"):
        print("[✗] 无法确定学号，WebSocket 不能连接 /websocket/None。请提供 -u 或确保用户信息含 XH。")
        main_logger.error("无效学号，中止选课流程")
        return None
    student_id = str(student_id).strip()
    if not login_state.username:
        login_state.username = student_id

    # 启动 HTTP 心跳（维持登录态，每30秒请求一次课程列表）
    http_heartbeat = HttpHeartbeat(
        session=session,
        token=token,
        batch_id=batch_id,
        campus=campus,
        interval=30,
    )
    http_heartbeat.start()

    # 启动 WebSocket 心跳
    ws_heartbeat = WebSocketHeartbeat(
        student_id=student_id,
        cookies=dict(session.cookies),
        use_vpn=use_vpn
    )
    ws_heartbeat.start()

    # 心跳内重登成功后同步 http token + WS cookie
    def _on_http_relogin(new_token):
        login_state.token = new_token
        http_heartbeat.update_token(new_token)
        try:
            ws_heartbeat.update_cookies(dict(session.cookies))
        except Exception as e:
            heartbeat_logger.error(f"[HTTP心跳] 同步 WS cookie 失败: {e}")

    http_heartbeat.set_on_relogin(_on_http_relogin)

    try:
        # 选择课程（支持一次性选择多个，第一个为主选，后面为备选）
        result = choose_class(session, token, batch_id, campus, course_type)
        if result is None or result[0] is None:
            return None
        if result[0] == "BACK":
            return "BACK"  # 返回轮次选择
        selected_classes, all_classes = result

        # 确认课程选择
        confirmed, selected_class, backup_classes = confirm_course_selection(selected_classes, course_type)
        if not confirmed:
            print("操作已取消。")
            return None

        # 保持 HTTP 心跳贯穿抢课全过程（token 维持 / 鉴权失败重登）
        # 开始抢课（传入 ws/http 心跳，便于成功确认与重登同步）
        grab_result = start_grabbing(
            session, token, batch_id, selected_class, backup_classes, course_type,
            ws_heartbeat=ws_heartbeat, http_heartbeat=http_heartbeat,
        )
        if grab_result == "BACK":
            return "BACK"  # 返回轮次选择
        return None
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

    # skip-login 时回填学号（从用户信息提取）
    if not login_state.username:
        sid = _extract_student_id(login_data)
        if sid:
            login_state.username = sid
            print(f"[✓] 从用户信息回填学号: {sid}")
            main_logger.info(f"从用户信息回填学号: {sid}")
            # 若启动时用的 unknown 日志目录，可保持；学号主要用于 WS/重登
        else:
            print("[!] 未能从用户信息提取学号，WebSocket 可能无法连接")
            main_logger.warning("未能从用户信息提取学号")

    # 6. 选择轮次和课程（支持返回上一级）
    while True:
        batch, course_type = choose_elective_batch(login_data, session, token)
        if not batch:
            return
        batch_id = batch['code']
        login_state.batch_id = batch_id

        # 7. 运行课程选择和抢课流程
        result = run_course_selection(
            session=session,
            token=token,
            batch_id=batch_id,
            campus=login_state.campus,
            username=args.username or login_state.username,
            use_vpn=use_vpn,
            course_type=course_type,
            login_data=login_data,
        )

        if result == "BACK":
            # 返回轮次选择
            print("\n返回轮次选择...")
            continue
        else:
            # 正常结束或取消
            break


if __name__ == "__main__":
    main()
