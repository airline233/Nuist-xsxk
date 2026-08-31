"""xgkc_monitor.py — 通识课(XGKC)容量监控抢选脚本。

结构与 course_cron.py 保持一致（函数一一对应），差异点：
- 通识课列表是扁平行结构（每行一个教学班，JXBID/secretVal 直接在行上），
  所以 get_course_capacity / fetch_selected_classes 改为扁平行版本；
- 默认「纯监控抢选」：不要求先退课（drop_class_info=None 时不进退课流程）；
  --swap 打开退旧换新模式，状态机与 course_cron 完全一致（退→抢→失败回补）；
- 支持筛选（网课/类别/教师/星期/节次/冲突等），筛选逻辑在 xgkc_common.py。

核心认知沿用 course.py：/add 只是入队，真正选上靠 WebSocket 推「选课成功」；
满员靠 WS 回调确认。基建（登录/重登/双心跳/OCR）复用 common.py。

用法：
    python xgkc_monitor.py -u 学号 -p 密码                 # 纯监控抢选（选择界面按 f 筛选）
    python xgkc_monitor.py -u 学号 -p 密码 --one-per-category  # 每个大类只抢一门，抢到即停该类
    python xgkc_monitor.py -u 学号 -p 密码 --swap         # 退旧换新模式
    python xgkc_monitor.py -u 学号 -p 密码 --batch <code> # 指定轮次
"""
import argparse
import json
import re
import time
import datetime
import threading

import requests
import truststore
from urllib.parse import urlencode, quote_plus
from rich.console import Console
from rich.table import Table
from rich.text import Text

from common import (
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
    resolve_course_type,
    HttpHeartbeat,
    WebSocketHeartbeat,
    load_cookie_file,
    init_vpn_login,
    init_login,
    validate_arguments,
)
from xgkc_common import (
    is_online_course,
    is_conflict,
    is_full,
    is_selected,
    xgxklb_major,
    format_time_blocks,
    format_places,
    build_filters,
    apply_filters,
    filter_menu,
    display_width,
    truncate_by_width,
)

COURSE_TYPE_XGKC = "XGKC"
PAGE_SIZE = 500           # 一次拉取的教学班上限
POLL_INTERVAL = 3         # 容量轮询间隔（秒）：多目标监控建议 3~5s，太快易触发 403 限频
GRAB_INTERVAL = 0.3       # 抢课重投间隔（秒），与 course.py 抢课循环一致
MAX_GRAB_RETRIES = 15     # 检测到空位后的连抢上限（约 4.5s），超过回到容量轮询


# ---------------------------------------------------------------- 列表获取（扁平行）

def fetch_xgkc_rows(session, token, batch_id, campus, page_size=PAGE_SIZE):
    """拉取通识课全量列表（扁平行结构：每行一个教学班）。

    鉴权失败时自动重登并重试一次。

    Returns:
        tuple: (rows: list, ok: bool)
    """
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Batchid": batch_id,
        "Content-Type": "application/json;charset=UTF-8"
    }
    base_body = {
        "teachingClassType": COURSE_TYPE_XGKC,
        "pageNumber": 1,
        "pageSize": page_size,
        "orderBy": "",
        "campus": campus,
        "SFYX": "2",
        "KEY": "",
    }

    def _do_fetch(tk):
        # 先探总数，再一次性拉全量（对应 xgkc_query.query_all_courses 的做法）
        body = dict(base_body)
        body["pageSize"] = 1
        try:
            resp = session.post(URL_LIST_CLASSES, headers={**headers, "Authorization": tk},
                                data=json.dumps(body), timeout=15)
            data = resp.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            main_logger.warning(f"通识课列表请求异常: {e}")
            return "error", []
        code = data.get("code")
        msg = str(data.get("msg", ""))
        if code != 200:
            if _is_auth_failure(code, msg):
                return "auth", []
            main_logger.warning(f"通识课列表失败: code={code}, msg={msg}")
            return "fail", []

        rows = data.get("data", {}).get("rows") or []
        total = data.get("data", {}).get("total")
        if not total or total <= 1:
            return "ok", rows

        body2 = dict(base_body)
        body2["pageSize"] = max(page_size, total)
        try:
            resp2 = session.post(URL_LIST_CLASSES, headers={**headers, "Authorization": tk},
                                 data=json.dumps(body2), timeout=30)
            data2 = resp2.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            main_logger.warning(f"通识课全量拉取异常: {e}")
            return "ok", rows
        if data2.get("code") != 200:
            return "ok", rows
        return "ok", data2.get("data", {}).get("rows") or rows

    status, rows = _do_fetch(token)
    if status == "auth":
        main_logger.warning("通识课列表鉴权失败，尝试重登...")
        success, new_token = handle_relogin(None)
        if success and new_token:
            login_state.token = new_token
            status2, rows2 = _do_fetch(new_token)
            return rows2, status2 == "ok"
        return [], False
    return rows, status == "ok"


def _find_row(rows, jxbid):
    """在扁平行列表中按 JXBID 找教学班。"""
    target = str(jxbid)
    for row in rows or []:
        if str(row.get("JXBID", "")) == target:
            return row
    return None


# ---------------------------------------------------------------- 课程选择（对应 course_cron 的两级菜单）

def _group_rows_by_course(rows):
    """把扁平教学班行按 KCH 聚合成「课程」结构（兼容 course_cron 的 courses/tcList 展示）。

    Returns:
        list of {"KCH": ..., "KCM": ..., "tcList": [row, ...]}
    """
    groups = {}
    for row in rows:
        key = row.get("KCH") or row.get("KCM") or row.get("JXBID")
        group = groups.setdefault(key, {
            "KCH": key,
            "KCM": row.get("KCM", "未知课程"),
            "XGXKLB": row.get("XGXKLB", ""),
            "tcList": [],
        })
        group["tcList"].append(row)
    return list(groups.values())


def choose_course_from_list(courses):
    """当有多门课程时，让用户先选择课程（对应 course_cron.choose_course_from_list）。

    Returns:
        dict or str: 选中的课程对象，或 "BACK"/"FILTER"
    """
    console = Console()

    print("\n--- 该轮次下有多门课程，请先选择要抢的课程 ---")

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
    for _ in range(COLS):
        table.add_column(justify="left", no_wrap=False, overflow="fold")

    def build_course_cell(idx, course):
        tc_list = course.get("tcList", [])
        course_name = course.get("KCM", "未知课程")
        text = Text()
        text.append(f"[{idx}] ", style="bold cyan")
        if is_online_course(tc_list[0]) if tc_list else False:
            text.append("🌐 ", style="magenta")
        text.append(f"{course_name}\n", style="bold")
        text.append(f"({len(tc_list)}个教学班", style="dim")
        if course.get("XGXKLB"):
            text.append(f" | {course['XGXKLB'][:6]}", style="dim")
        text.append(")", style="dim")
        return text

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
    console.print(f"共 [bold]{total}[/bold] 门课程，输入 [bold cyan]0[/bold cyan] 返回上一级，"
                  f"[bold cyan]f[/bold cyan] 筛选\n")

    while True:
        choice = input("请输入课程序号 (0返回, f筛选): ").strip()
        if choice.lower() == "f":
            return "FILTER"
        if choice == "0":
            return "BACK"
        if choice.isdigit() and 1 <= int(choice) <= len(courses):
            selected = courses[int(choice) - 1]
            print(f"已选择课程: {selected.get('KCM', '未知课程')}")
            return selected
        print("无效的输入，请输入列表中的数字。")


def _choose_teaching_class(all_classes, has_multi_courses=False, is_auto_expand=False):
    """选择教学班（对应 course_cron._choose_teaching_class，XGKC 扁平行展示）。

    Returns:
        list or str: 选中的教学班列表，或 "BACK"/"FILTER"
    """
    total = len(all_classes)
    if total <= 9:
        COLS = 2
    elif total <= 20:
        COLS = 3
    else:
        COLS = 4

    console = Console()
    table = Table(title="请选择要抢的教学班", show_header=False,
                  box=None, padding=(0, 1), collapse_padding=True)
    for _ in range(COLS):
        table.add_column(justify="left", no_wrap=False, overflow="fold")

    def build_cell(idx, clazz):
        text = Text()
        text.append(f"[{idx}] ", style="bold cyan")

        parent_course = clazz.get("_parent_course")
        if parent_course:
            text.append(f"【{parent_course}】\n", style="magenta")

        if is_online_course(clazz):
            text.append("🌐 ", style="magenta")
        text.append(f"{clazz.get('KCM', '未知课程')}\n", style="bold")
        if is_selected(clazz):
            text.append("已选 ", style="green")
        text.append(f"{clazz.get('SKJS', '-')} ", style="green")
        text.append(f"[{clazz.get('YXRS', '?')}/{clazz.get('KRL', '?')}]", style="yellow")
        if is_full(clazz):
            text.append(" 已满", style="red")
        if is_conflict(clazz):
            text.append(" 冲突", style="yellow")
        text.append("\n")

        time_str = format_time_blocks(clazz) or "未安排时间"
        text.append(time_str, style="dim")
        place = format_places(clazz)
        if place and place != time_str:
            text.append(f" {place}", style="dim")
        return text

    for i in range(0, total, COLS):
        row_cells = []
        for j in range(COLS):
            idx = i + j
            if idx < total:
                row_cells.append(build_cell(idx + 1, all_classes[idx]))
            else:
                row_cells.append("")
        table.add_row(*row_cells)

    console.print(table)
    if is_auto_expand:
        back_hint = "返回上一级"
    else:
        back_hint = "返回课程选择" if has_multi_courses else "返回上一级"
    console.print(f"共 [bold]{total}[/bold] 个教学班，输入 [bold cyan]0[/bold cyan] {back_hint}，"
                  f"[bold cyan]f[/bold cyan] 筛选\n")

    while True:
        choice_str = input("请输入教学班序号（多个用逗号或空格分隔，0返回, f筛选）: ").strip()
        if not choice_str:
            print("请输入至少一个序号。")
            continue
        if choice_str.lower() == "f":
            return "FILTER"
        if choice_str == "0":
            return "BACK"

        try:
            choices = [int(x.strip()) for x in re.split(r'[,\s]+', choice_str) if x.strip()]
            if 0 in choices:
                return "BACK"
            if not choices:
                print("请输入至少一个序号。")
                continue

            selected_classes = []
            invalid_choices = []
            for choice in choices:
                if 1 <= choice <= len(all_classes):
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


def choose_class(session, token, batch_id, campus, course_type=COURSE_TYPE_XGKC, filters=None):
    """获取课程列表并让用户选择（对应 course_cron.choose_class）。

    通识课行是扁平的，先按 KCH 聚合出课程层级，再选择教学班；
    教学班很少时自动全部展开（沿用 course_cron 的 should_expand_all 思路）。

    Returns:
        tuple: (selected_classes, all_classes) 或 ("BACK", None) / (None, None)
    """
    filters = filters if filters is not None else build_filters()

    print("\n正在获取课程列表中...")
    headers = {
        **COMMON_HEADERS,
        "Authorization": token,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    # 先切换轮次（与 course_cron 一致）
    try:
        response0 = session.post(URL_SWITCH_BATCH, headers=headers,
                                 data=f"batchId={batch_id}", timeout=10)
        response0.raise_for_status()
        data0 = response0.json()
        if data0.get("code") != 200:
            print(f"切换轮次失败: {data0.get('msg')}")
            return None, None
    except Exception as e:
        print(f"切换轮次时发生错误: {e}")
        return None, None

    rows, ok = fetch_xgkc_rows(session, token, batch_id, campus)
    if not ok or not rows:
        print("在此轮次下未找到可选的课程。")
        return None, None

    while True:
        visible_rows = apply_filters(rows, filters)
        if not visible_rows:
            print("[!] 当前筛选条件下没有课程，自动重置筛选。")
            filters = build_filters()
            visible_rows = rows

        courses = _group_rows_by_course(visible_rows)
        total_classes = len(visible_rows)

        # 自动展开：只有一门课 / 教学班少 / 每门课都只有一个班（扁平化的通识列表）
        should_expand_all = (len(courses) <= 1 or total_classes < 10
                             or len(courses) == total_classes)

        if should_expand_all:
            print(f"\n总计 {total_classes} 个教学班，自动全部展开展示")
            all_classes = []
            for course in courses:
                for clazz in course.get("tcList", []):
                    clazz["_parent_course"] = course.get("KCM", "") if len(courses) > 1 else ""
                    all_classes.append(clazz)
            selected_course = None
        else:
            selected_course = choose_course_from_list(courses)
            if selected_course == "FILTER":
                filter_menu(filters)
                continue
            if selected_course in ("BACK", None):
                return "BACK", None
            all_classes = selected_course.get("tcList", [])

        if not all_classes:
            print("在此轮次下未找到可选的课程。")
            return None, None

        result = _choose_teaching_class(
            all_classes,
            has_multi_courses=(len(courses) > 1),
            is_auto_expand=should_expand_all,
        )
        if result == "FILTER":
            filter_menu(filters)
            continue
        if result == "BACK":
            if len(courses) > 1 and not should_expand_all:
                continue
            return "BACK", None
        return result, all_classes


# ---------------------------------------------------------------- 容量 / 已选 / 退课（扁平行版本）

def get_course_capacity(session, token, batch_id, campus, target_class_id,
                        course_type=COURSE_TYPE_XGKC):
    """查询指定课程的当前容量信息（对应 course_cron.get_course_capacity，扁平行版本）。

    Returns:
        tuple: (已选人数, 课容量, 课程行) 或 (None, None, None)
    """
    rows, ok = fetch_xgkc_rows(session, token, batch_id, campus)
    if not ok:
        return None, None, None
    row = _find_row(rows, target_class_id)
    if not row:
        return None, None, None
    try:
        return int(row["YXRS"]), int(row["KRL"]), row
    except (KeyError, TypeError, ValueError):
        return None, None, None


def fetch_selected_classes(session, token, batch_id, campus,
                           course_type=COURSE_TYPE_XGKC, exclude_jxbid=None):
    """拉取当前轮次下已选教学班（SFYX==1，对应 course_cron.fetch_selected_classes）。

    Returns:
        list: 已选教学班行列表；失败返回 []
    """
    rows, ok = fetch_xgkc_rows(session, token, batch_id, campus)
    if not ok:
        print("[!] 拉取已选课失败")
        return []
    selected = []
    for row in rows:
        jxbid = row.get("JXBID")
        if not jxbid or not is_selected(row):
            continue
        if exclude_jxbid and jxbid == exclude_jxbid:
            continue
        selected.append(row)
    main_logger.info(f"已选课拉取完成: {len(selected)} 门")
    return selected


def drop_class(session, token, batch_id, course_to_drop, course_type=COURSE_TYPE_XGKC):
    """退掉指定课程（与 course_cron.drop_class 完全一致）。

    鉴权失败或 JSON 解析失败时尝试重登并用新 token 再试一次。

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
            main_logger.info(f"退课成功: {course_to_drop.get('KCM', '未知')} - {msg}")
            return True

        if status in ("auth", "auth_or_parse"):
            main_logger.warning(f"退课鉴权失败，尝试重登: status={status}")
            success, new_token = handle_relogin(response)
            if success and new_token:
                login_state.token = new_token
                status2, _, result2 = _do_drop(new_token)
                if status2 == "ok":
                    msg = (result2 or {}).get('msg', '无消息')
                    main_logger.info(f"退课成功(重登后): {course_to_drop.get('KCM', '未知')} - {msg}")
                    return True
                msg2 = (result2 or {}).get('msg', '无消息') if result2 else '解析失败'
                main_logger.warning(f"退课失败(重登后): {msg2}")
                return False
            main_logger.error("退课重登失败")
            return False

        code = (result or {}).get('code')
        msg = (result or {}).get('msg', '无消息')
        main_logger.warning(f"退课失败: {code} - {msg}")
        return False
    except Exception as e:
        main_logger.error(f"退课请求异常: {e}")
        return False


def _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
    """列表复核：目标教学班是否已选上（对应 course_cron._confirm_selected_by_list）。"""
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
    return False


def _try_add_class(session, token, batch_id, select_class, course_type,
                   ws_heartbeat=None, wait_timeout=5.0):
    """提交选课并尽量用 WebSocket / 列表复核确认真实成功。

    与 course_cron._try_add_class 完全一致（_confirm_selected_by_list 为扁平行版本）。

    Returns:
        tuple: (ok: bool, reason: str)
    """
    secret = select_class.get('secretVal')
    if not secret:
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
        if clazz_id and str(clazz_id) == target_id_str:
            ws_success_event.set()

    def on_fail(code, msg, data):
        fail_id = _extract_ws_clazz_id(data)
        if fail_id and str(fail_id) != target_id_str:
            main_logger.info(f"[WebSocket] 忽略非目标课失败: fail={fail_id}, target={target_id_str}, msg={msg}")
            return
        if _is_full_msg(msg):
            ws_fail_reason["value"] = "full"
        else:
            ws_fail_reason["value"] = "other_fail"
        ws_fail_event.set()

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
            success, new_token = handle_relogin(response)
            if success and new_token:
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
            if _is_already_selected_msg(msg):
                if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                    main_logger.info(f"接口提示已选且列表复核通过: {msg}")
                    return True, "success"

                main_logger.info(f"接口提示已选，但列表未确认；等待目标课程 WS 确认（最长 {wait_timeout:.0f}s）...")
                deadline = time.time() + wait_timeout
                while time.time() < deadline:
                    if ws_success_event.is_set():
                        main_logger.info(f"接口提示已选且 WS 确认目标教学班: {msg}")
                        return True, "success"
                    if ws_heartbeat:
                        msgs = ws_heartbeat.check_success(target_id_str)
                        if msgs:
                            main_logger.info(f"接口提示已选且 WS 轮询确认目标教学班: {msg}")
                            return True, "success"
                    if ws_fail_event.is_set():
                        break
                    time.sleep(0.1)

                if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                    main_logger.info(f"接口提示已选，延迟列表复核通过: {msg}")
                    return True, "success"
                main_logger.warning(f"已选文案未获 WS/列表确认: target={target_id_str}, msg={msg}")
                return False, "other_fail"
            if _is_full_msg(msg):
                return False, "full"
            if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                main_logger.info(f"接口失败但列表复核已选上: {msg}")
                return True, "success"
            return False, "http_fail"

        # code==200 仅表示入队，等 WS 确认
        main_logger.info(f"已加入选课队列，等待 WebSocket 确认（最长 {wait_timeout:.0f}s）...")
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            if ws_success_event.is_set():
                return True, "success"
            if ws_fail_event.is_set():
                reason = ws_fail_reason["value"]
                if reason == "full":
                    return False, "full"
                if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
                    return True, "success"
                return False, reason
            if ws_heartbeat:
                msgs = ws_heartbeat.check_success(target_id_str)
                if msgs:
                    return True, "success"
            time.sleep(0.1)

        if _confirm_selected_by_list(session, token, batch_id, target_id, course_type):
            main_logger.info("WebSocket 超时，但列表复核确认已选上")
            return True, "success"

        main_logger.warning("选课入队后 WS 超时未确认")
        return False, "timeout"
    except Exception as e:
        main_logger.error(f"选课请求异常: {e}")
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


# ---------------------------------------------------------------- 监控主循环（对应 course_cron.start_monitoring）

def start_monitoring(session, token, batch_id, campus, target_classes, drop_class_info=None,
                     course_type=COURSE_TYPE_XGKC, ws_heartbeat=None, http_heartbeat=None,
                     one_per_category=False):
    """监控目标课程容量（可同时监控多个），检测到空位后抢课。

    状态机与 course_cron.start_monitoring 一致：
      watching           监控容量，有空位才行动
      dropped_pending_add 已退旧课，只抢目标，绝不再退

    差异：
    - target_classes 为列表（无优先级，全部目标同时监控）：每轮检查所有目标，谁有空位抢谁；
    - drop_class_info 为 None 时为「纯监控抢选」（不要求先退课），
      检测到空位直接抢，不进 dropped_pending_add 状态；
    - 容量轮询每 POLL_INTERVAL 秒一次（多个目标共用一次全量列表请求，不增加请求量）；
      抢课投递与 course.py 一致：检测到空位后按 0.3 秒间隔连续重投，
      满员(被别人抢走)则立刻转下一个有空位的目标（等价 course.py 切备选）；
    - one_per_category=True（仅纯监控模式）：某大类抢到一门后该大类全部停止，
      其它大类继续，直到每类都有或 Ctrl+C；默认抢到一门即全部结束；
    - 纯监控模式下，与已选课时间冲突（SFCT）且未选上的目标会自动移出监控列表
      （抢到新课后产生的冲突同样生效，被移出的目标会在终端提示）；
    - 终端保留一行最新状态（每轮覆盖刷新，不保留旧状态行）；常规状态在变化时
      写入日志文件（logs/course_grab.log），关键事件（空位/选课/退课/回补/重登）
      另起一行打印。
    """
    targets = [dict(t) for t in target_classes]
    total_categories = len({xgxklb_major(t) for t in targets})
    swap_mode = bool(drop_class_info)
    drop_id = drop_class_info['JXBID'] if drop_class_info else None
    drop_snapshot = dict(drop_class_info) if drop_class_info else None

    def get_display_name(clazz):
        name = clazz.get('KCM', '未知')
        return f"🌐 {name}" if is_online_course(clazz) else name

    def short_name(clazz):
        """状态行用短名，避免长行折行刷屏。"""
        name = get_display_name(clazz)
        return name[:12] + "…" if len(name) > 12 else name

    last_status_width = 0

    def show_status(text):
        """终端单行刷新：按显示宽度截断到 60 列并补空格清除残留，不保留旧状态行。"""
        nonlocal last_status_width
        text = truncate_by_width(text, 60)
        pad = max(0, last_status_width - display_width(text))
        print(f"\r{text}{' ' * pad}", end="", flush=True)
        last_status_width = display_width(text)

    def log_detail(text):
        """常规轮询状态/细节：只写日志文件，不刷终端。"""
        main_logger.info(text)

    def announce(text):
        """关键事件：写日志文件 + 另起一行打印到终端（先结束状态行）。"""
        nonlocal last_status_width
        main_logger.info(text)
        print(f"\n{text}")
        last_status_width = 0

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
        """目标未选上时，尝试把旧课加回（仅换课模式使用）。"""
        nonlocal drop_snapshot
        live = _get_live_token(current_token)
        tag = f"({reason_tag})" if reason_tag else ""
        announce(f"[↩] 尝试回补旧课{tag}: {get_display_name(drop_snapshot)} - {drop_snapshot.get('SKJS', '-')}")
        main_logger.info(f"尝试回补旧课{tag}: JXBID={drop_id}")
        if _confirm_selected_by_list(session, live, batch_id, drop_id, course_type):
            main_logger.info(f"回补跳过(列表已选): {drop_id}")
            return True

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
            main_logger.error(f"回补缺少 secretVal: {drop_id}")
            return False

        ok, reason = _try_add_class(
            session, live, batch_id, restore_class, course_type,
            ws_heartbeat=ws_heartbeat, wait_timeout=5.0,
        )
        if ok:
            main_logger.info(f"旧课回补成功: {drop_id}")
            return True

        if reason == "relogin":
            live2 = _get_live_token(live)
            if live2:
                sync_after_relogin(live2)
                live = live2
            if _confirm_selected_by_list(session, live, batch_id, drop_id, course_type):
                return True

        announce(f"[✗] 旧课回补未成功({reason})，将继续抢目标（课表可能暂时为空）")
        main_logger.warning(f"旧课回补失败: reason={reason}, JXBID={drop_id}")
        return False

    def handle_target_fail_then_restore(current_token, reason, force=False):
        """目标抢失败后：累计失败次数，达阈值或 force 时尝试回补（仅换课模式）。"""
        nonlocal state, pending_fail_streak, last_restore_ok_at
        if reason == "relogin":
            live = _get_live_token(token)
            if live:
                sync_after_relogin(live)
                current_token = live

        pending_fail_streak += 1
        should_restore = force or pending_fail_streak >= RESTORE_AFTER_FAILS
        if not should_restore:
            show_status(
                f"[已退待选] 目标未成功({reason})，继续抢目标 "
                f"({pending_fail_streak}/{RESTORE_AFTER_FAILS} 次后尝试回补旧课)"
            )
            main_logger.warning(
                f"目标未成功暂不回补: reason={reason}, streak={pending_fail_streak}"
            )
            return "keep"

        main_logger.warning(f"目标未成功，尝试回补: {reason}, streak={pending_fail_streak}")
        if attempt_restore_dropped(current_token, reason_tag=reason):
            state = "watching"
            pending_fail_streak = 0
            last_restore_ok_at = time.time()
            announce(
                f"[→] 已回补旧课，回到监控（{RESTORE_COOLDOWN_SEC:.0f}s 内不重复退课，"
                "之后有空位再换）"
            )
            main_logger.info("状态切换: dropped_pending_add -> watching (回补成功)")
            return "restored"
        pending_fail_streak = 0
        announce("[!] 回补失败，保持「已退待选」，继续抢目标（不会再退课）")
        return "keep"

    drop_name = get_display_name(drop_class_info) if drop_class_info else None

    # watching | dropped_pending_add（纯抢选模式不会进入后者）
    RESTORE_AFTER_FAILS = 3
    RESTORE_COOLDOWN_SEC = 20.0
    state = "watching"
    check_count = 0
    auth_fail_streak = 0
    pending_fail_streak = 0
    last_restore_ok_at = 0.0
    missing_warned = set()
    last_status_key = None  # 终端状态行去重：仅在容量/状态变化时打印

    print("\n" + "=" * 60)
    print("监控目标课程:")
    for i, t in enumerate(targets, 1):
        print(f"  [{i}] {get_display_name(t)} - {t['SKJS']} [{t.get('YXRS', '?')}/{t.get('KRL', '?')}]")
    if swap_mode:
        print(f"准备退掉课程: {drop_name} - {drop_class_info['SKJS']}")
        print(f"每 {POLL_INTERVAL} 秒检测一次课容量；空位时退课→选课。")
        print(
            f"状态保护：退课后持续抢目标；连续 {RESTORE_AFTER_FAILS} 次未中则尝试回补旧课，"
            f"回补成功后 {RESTORE_COOLDOWN_SEC:.0f}s 内不再退。"
        )
        print("Ctrl+C 中断时会立即尝试回补旧课。")
    else:
        print("模式：纯监控抢选（不退课），检测到空位立即抢。")
        print(f"每 {POLL_INTERVAL} 秒检测一次课容量。")
        if one_per_category:
            print("每个通识大类抢到一门后，该大类停止监控，其它大类继续。")
    print("=" * 60 + "\n")

    main_logger.info(
        f"开始监控: 目标={[t.get('KCM') for t in targets]}, 退课={drop_name or '(无)'}"
    )

    try:
        while True:
            check_count += 1
            current_token = _get_live_token(token)
            if current_token and current_token != token:
                sync_after_relogin(current_token)

            rows, ok = fetch_xgkc_rows(session, current_token, batch_id, campus)
            if not ok:
                # 已退待选：查询失败也不跳过，用快照继续抢（所有目标全部投递）
                if state == "dropped_pending_add":
                    auth_fail_streak += 1
                    main_logger.warning(f"查询失败(已退待选) streak={auth_fail_streak}")
                    if auth_fail_streak >= 3:
                        main_logger.warning("连续查询失败，尝试重新登录...")
                        success, new_token = handle_relogin(None)
                        if success and new_token:
                            sync_after_relogin(new_token)
                            auth_fail_streak = 0
                            current_token = _get_live_token(token) or new_token
                            main_logger.info("重新登录成功，继续抢课")
                        else:
                            main_logger.error("重新登录失败，稍后仍会用现有会话尝试选课")
                    dt = datetime.datetime.now().strftime("%H:%M:%S")
                    any_success = False
                    for t in targets:
                        show_status(f"[{dt}] [已退待选] 查询失败，用快照抢 {short_name(t)} ...")
                        main_logger.info(f"查询失败，用快照继续抢: JXBID={t.get('JXBID')}")
                        ok_add, reason = _try_add_class(
                            session, current_token, batch_id, t, course_type,
                            ws_heartbeat=ws_heartbeat, wait_timeout=5.0
                        )
                        if ok_add:
                            print(f"\n[✓] 选课成功！{get_display_name(t)} - {t['SKJS']}")
                            main_logger.info(f"选课成功(已退待选/查询失败路径): {t.get('KCM')}")
                            any_success = True
                            break
                        handle_target_fail_then_restore(current_token, reason)
                        time.sleep(GRAB_INTERVAL)
                    if any_success:
                        break
                    continue

                # watching：查询失败走 streak 重登，不投递
                auth_fail_streak += 1
                dt = datetime.datetime.now().strftime("%H:%M:%S")
                main_logger.warning(f"容量查询失败 streak={auth_fail_streak}")
                show_status(f"[{dt}] 查询失败，重试中... ({auth_fail_streak})")
                if auth_fail_streak >= 3:
                    main_logger.warning("连续查询失败，尝试重新登录...")
                    success, new_token = handle_relogin(None)
                    if success and new_token:
                        sync_after_relogin(new_token)
                        auth_fail_streak = 0
                        main_logger.info("重新登录成功，继续监控")
                    else:
                        main_logger.error("重新登录失败，稍后重试")
                time.sleep(POLL_INTERVAL)
                continue

            auth_fail_streak = 0
            dt = datetime.datetime.now().strftime("%H:%M:%S")

            # 刷新所有目标的最新快照/容量，并找出有空位的目标（无优先级）
            slot_targets = []   # [(target_snapshot, latest_row)]
            status_parts = []
            status_key = []     # 用于终端去重：[(JXBID, yxrs, krl), ...]
            conflict_removed = []
            for t in list(targets):
                latest = _find_row(rows, t["JXBID"])
                if not latest:
                    if t["JXBID"] not in missing_warned:
                        missing_warned.add(t["JXBID"])
                        main_logger.warning(f"目标课不在列表中: JXBID={t.get('JXBID')} {t.get('KCM')}")
                    status_parts.append(f"{short_name(t)}:?/?")
                    status_key.append((str(t["JXBID"]), None, None))
                    continue
                t.update(latest)  # 快照刷新（secretVal 等）
                # 纯监控模式：与已选课时间冲突（且未选上）的目标直接移出监控列表
                if not swap_mode and is_conflict(latest) and not is_selected(latest):
                    conflict_removed.append(t)
                    targets.remove(t)
                    main_logger.warning(
                        f"目标因时间冲突移出监控: {t.get('KCM')} JXBID={t.get('JXBID')}"
                    )
                    continue
                yxrs = int(latest.get("YXRS", 0) or 0)
                krl = int(latest.get("KRL", 0) or 0)
                status_parts.append(f"{short_name(t)}:{yxrs}/{krl}")
                status_key.append((str(t["JXBID"]), yxrs, krl))
                if yxrs < krl:
                    slot_targets.append((t, latest))
            status_key = tuple(status_key)

            if conflict_removed:
                for t in conflict_removed:
                    announce(f"[{dt}] 时间冲突，已移出监控: {get_display_name(t)}")
                if not targets:
                    announce("所有目标均因时间冲突被移出，监控结束。")
                    break

            # ---- 状态：已退后只抢目标（全部投递）；失败则回补 ----
            if state == "dropped_pending_add":
                any_success = False
                for t in targets:
                    latest = _find_row(rows, t["JXBID"]) or t
                    yxrs = int(latest.get("YXRS", 0) or 0)
                    krl = int(latest.get("KRL", 0) or 0)
                    show_status(f"[{dt}] [已退待选] 抢 {short_name(t)} ({yxrs}/{krl})")
                    main_logger.info(f"[已退待选] 抢目标: JXBID={t.get('JXBID')} ({yxrs}/{krl})")
                    ok_add, reason = _try_add_class(
                        session, current_token, batch_id, latest, course_type,
                        ws_heartbeat=ws_heartbeat, wait_timeout=5.0
                    )
                    if ok_add:
                        print(f"\n[✓] 选课成功！{get_display_name(t)} - {t['SKJS']}")
                        main_logger.info(f"选课成功(已退待选): {t.get('KCM')}")
                        any_success = True
                        break
                    handle_target_fail_then_restore(current_token, reason)
                    time.sleep(GRAB_INTERVAL)
                if any_success:
                    break
                continue

            # ---- 状态：watching ----
            if slot_targets:
                exit_all = False
                for t, latest in slot_targets:
                    if t not in targets:
                        continue  # 该目标所属大类已完成，跳过
                    yxrs = int(latest.get("YXRS", 0) or 0)
                    krl = int(latest.get("KRL", 0) or 0)

                    # 刚回补成功后的冷却：避免「回补→立刻又退」抖动（仅换课模式）
                    if swap_mode and last_restore_ok_at and (time.time() - last_restore_ok_at) < RESTORE_COOLDOWN_SEC:
                        remain = RESTORE_COOLDOWN_SEC - (time.time() - last_restore_ok_at)
                        main_logger.info(
                            f"有空位但回补冷却中({remain:.0f}s): JXBID={t.get('JXBID')} {yxrs}/{krl}"
                        )
                        show_status(
                            f"[{dt}] 有空位，但回补冷却中({remain:.0f}s)，暂不退课 "
                            f"{short_name(t)}: {yxrs}/{krl}"
                        )
                        time.sleep(min(POLL_INTERVAL, max(1, remain)))
                        break

                    announce(f"[{dt}] 检测到空位！{short_name(t)} {yxrs}/{krl}")
                    main_logger.info(f"检测到空位: {t.get('JXBID')} {yxrs}/{krl}")

                    # 1. 换课模式：退课（每个监控周期只退一次）；纯抢选模式跳过
                    if swap_mode and state == "watching":
                        announce(f"[{dt}] 正在退课: {drop_name}...")
                        main_logger.info(f"开始退课: {drop_id}")
                        drop_info = drop_class_info
                        try:
                            latest_drop = _find_row(rows, drop_class_info['JXBID'])
                            if latest_drop and latest_drop.get('secretVal'):
                                drop_info = latest_drop
                                drop_snapshot = dict(latest_drop)
                                if not drop_snapshot.get("KCM") and drop_class_info.get("KCM"):
                                    drop_snapshot["KCM"] = drop_class_info.get("KCM")
                        except Exception:
                            pass

                        drop_success = drop_class(session, current_token, batch_id, drop_info, course_type)
                        live_after_drop = _get_live_token(current_token)
                        if live_after_drop and live_after_drop != current_token:
                            sync_after_relogin(live_after_drop)
                            current_token = live_after_drop
                        if not drop_success:
                            announce("[!] 退课失败，继续监控（不进入待选状态）...")
                            main_logger.warning("退课失败，继续监控")
                            time.sleep(POLL_INTERVAL)
                            break

                        drop_snapshot = dict(drop_info)
                        state = "dropped_pending_add"
                        pending_fail_streak = 0
                        announce(
                            f"[✓] 退课成功，进入「已退待选」"
                            f"（连续 {RESTORE_AFTER_FAILS} 次目标失败会尝试回补）"
                        )
                        main_logger.info("状态切换: watching -> dropped_pending_add")

                    # 2. 立刻选课：与 course.py 一致按 0.3 秒间隔连续重投；
                    #    满员(被别人抢走)则立刻转下一个有空位的目标（等价 course.py 切备选）
                    log_detail(f"正在选课: {short_name(t)}...")
                    show_status(f"[{dt}] 正在选课: {short_name(t)}...")
                    grabbed = False
                    grab_attempts = 0
                    while True:
                        if grab_attempts > 0:
                            time.sleep(GRAB_INTERVAL)
                        grab_attempts += 1
                        latest_now = _find_row(rows, t["JXBID"]) or latest
                        ok_add, reason = _try_add_class(
                            session, current_token, batch_id, latest_now, course_type,
                            ws_heartbeat=ws_heartbeat, wait_timeout=5.0
                        )
                        if ok_add:
                            print(f"\n[✓] 选课成功！{get_display_name(t)} - {t['SKJS']}")
                            main_logger.info(f"选课成功: {t.get('KCM')}")
                            grabbed = True
                            break
                        if state == "dropped_pending_add":
                            # 已退待选：交由下一轮 dropped 分支继续投递
                            handle_target_fail_then_restore(current_token, reason)
                            break
                        if reason == "full":
                            main_logger.warning(f"抢课未成功(满员): JXBID={t.get('JXBID')}")
                            announce(f"[{dt}] 抢课未成功({reason})，尝试下一个有空位的目标")
                            break
                        main_logger.warning(
                            f"抢课未成功: JXBID={t.get('JXBID')} reason={reason} "
                            f"第 {grab_attempts} 次，{GRAB_INTERVAL:.1f}s 后重试"
                        )
                        if grab_attempts == 1:
                            announce(f"[{dt}] 抢课未成功({reason})，按 {GRAB_INTERVAL:.1f}s 间隔重试")
                        else:
                            show_status(f"[{dt}] 抢课未成功({reason}) 第 {grab_attempts} 次")
                        if grab_attempts >= MAX_GRAB_RETRIES:
                            log_detail(f"{short_name(t)} 连续 {grab_attempts} 次未成功，回到容量轮询")
                            break
                    if grabbed:
                        if one_per_category and not swap_mode and len(targets) > 1:
                            # 抢到一门：该大类全部目标停止，其它大类继续监控
                            got_cat = xgxklb_major(t)
                            targets[:] = [x for x in targets if xgxklb_major(x) != got_cat]
                            remaining_cats = {xgxklb_major(x) for x in targets}
                            done_cats = total_categories - len(remaining_cats)
                            announce(
                                f"[✓] 已抢到 {get_display_name(t)}（{got_cat}，"
                                f"{done_cats}/{total_categories} 类完成），"
                                f"该类别停止监控，继续监控剩余 {len(targets)} 个目标..."
                            )
                            continue  # 同轮继续尝试下一个有空位的目标
                        exit_all = True
                        break
                    if state == "dropped_pending_add":
                        break
                if exit_all:
                    break
                if not targets:
                    announce("🎉 每个大类均已抢到一门，监控结束。")
                    break
                continue

            # 已满：终端单行覆盖刷新（按显示宽度截断，不保留旧状态行）；日志文件仅在状态变化时记录
            status_line = f"[{dt}] [{check_count}] " + " | ".join(status_parts) + f" (已满) [{state}]"
            show_status(status_line)
            if status_key != last_status_key:
                last_status_key = status_key
                main_logger.info(f"容量监控: {status_line}")
            time.sleep(POLL_INTERVAL)

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


# ---------------------------------------------------------------- 主流程（对应 course_cron.run_course_selection / main）

def run_course_selection(session, token, batch_id, campus, username, use_vpn,
                         course_type=COURSE_TYPE_XGKC, swap=False, one_per_category=False):
    """运行监控抢课流程

    流程：
    1. 选择目标课程（支持多选，全部目标同时监控，无优先级）
    2. --swap 时：从已选课程中选择要退的课（没有已选课则退化为纯抢选）
    3. 确认后开始监控
    4. 检测到空位：抢课（换课模式先退课）；
       --one-per-category 时，抢到一门后该大类停止、其它大类继续

    筛选（仅网课/类别大类/教师/星期/节次/教学方式等）在第一步的选择界面按 f 设置。
    """
    student_id = username
    if not student_id:
        print("错误: 学号无效，无法建立 WebSocket（请提供 -u 或确保 login_data 含学号）")
        main_logger.error("WebSocket student_id 无效，退出")
        return

    ws_heartbeat = WebSocketHeartbeat(
        student_id=student_id,
        cookies=dict(session.cookies),
        use_vpn=use_vpn,
        log_only=True,  # WS 消息只写日志文件，不打断终端单行状态
    )

    def on_http_relogin(new_token):
        """HTTP 心跳重登成功后同步 token 与 WS cookies"""
        login_state.token = new_token
        try:
            ws_heartbeat.update_cookies(dict(session.cookies))
        except Exception as e:
            heartbeat_logger.warning(f"on_relogin 同步 WS cookie 失败: {e}")

    http_heartbeat = HttpHeartbeat(
        session=session,
        token=token,
        batch_id=batch_id,
        campus=campus,
        interval=30,
        on_relogin=on_http_relogin,
    )
    http_heartbeat.start()
    ws_heartbeat.start()

    def get_display_name(clazz):
        name = clazz.get('KCM', '未知')
        return f"🌐 {name}" if is_online_course(clazz) else name

    try:
        # 1. 选择目标课程（可多选，全部目标同时监控）
        print("\n--- 第一步：选择要抢的目标课程 ---")
        filters = build_filters()
        result = choose_class(session, token, batch_id, campus, course_type, filters=filters)
        if result is None or result[0] is None:
            return
        if result[0] == "BACK":
            print("已返回上一级。")
            return

        selected_classes, all_classes = result
        if not isinstance(selected_classes, list) or not selected_classes:
            print("未选择有效目标课程。")
            return

        # 所有选中的教学班都是监控目标（无优先级，全部同时监控）
        target_classes = selected_classes
        target_ids = {str(t["JXBID"]) for t in target_classes}

        print(f"\n目标课程（{len(target_classes)} 个，同时监控）:")
        for i, t in enumerate(target_classes, 1):
            print(f"  [{i}] {get_display_name(t)} - {t['SKJS']} "
                  f"[{t['YXRS']}/{t['KRL']}]")
            if is_conflict(t):
                if swap:
                    print("      [!] 注意: 与已选课时间冲突（服务端标记），抢到时可能被拒")
                else:
                    print("      [!] 注意: 与已选课时间冲突，监控时会自动移出该目标")
            if is_selected(t):
                print("      [!] 注意: 已在选课结果中(SFYX=1)")

        # 2. 换课模式：独立拉取已选课，选择要退的课
        drop_class_info = None
        if swap:
            print("\n--- 第二步：从已选课中选择要退掉的课 ---")
            print("正在拉取已选课程列表...")
            selected_only = fetch_selected_classes(
                session, _get_live_token(token), batch_id, campus, course_type,
            )
            # 排除所有监控目标自身
            selected_only = [
                c for c in selected_only if str(c.get("JXBID")) not in target_ids
            ]

            if not selected_only:
                print("\n[!] 未找到可退的已选课程（SFYX=1），退化为纯监控抢选模式。")
            else:
                for i, clazz in enumerate(selected_only, 1):
                    print(
                        f"  [{i}] {get_display_name(clazz)} - {clazz.get('SKJS', '-')} "
                        f"[{clazz.get('YXRS', '?')}/{clazz.get('KRL', '?')}]\n"
                        f"      {format_time_blocks(clazz) or '未安排时间'} {format_places(clazz)}"
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

                if drop_class_info:
                    print(f"\n要退的课程: {get_display_name(drop_class_info)} - {drop_class_info['SKJS']}")
                    print(f"上课时间地点: {format_time_blocks(drop_class_info) or '未安排时间'} "
                          f"{format_places(drop_class_info)}")

        # 3. 确认
        print("\n" + "=" * 50)
        print("确认信息：")
        for i, t in enumerate(target_classes, 1):
            print(f"  目标[{i}]: {get_display_name(t)} - {t['SKJS']} "
                  f"[{t['YXRS']}/{t['KRL']}]")
        if drop_class_info:
            print(f"  要退课程: {get_display_name(drop_class_info)} - {drop_class_info['SKJS']}")
            print("  注意：退课后会持续抢目标；多次未中会尝试回补旧课，Ctrl+C 也会回补。")
        else:
            print("  模式：纯监控抢选（不退课）")
        print("=" * 50)

        confirm = input("\n确认开始监控？(y/n): ").strip().lower()
        if confirm != 'y':
            print("操作已取消。")
            return

        # 4. 开始监控
        start_monitoring(
            session, token, batch_id, campus, target_classes, drop_class_info,
            course_type=course_type, ws_heartbeat=ws_heartbeat,
            http_heartbeat=http_heartbeat, one_per_category=one_per_category,
        )

    finally:
        http_heartbeat.stop()
        ws_heartbeat.stop()


def main():
    # 0. 信任系统证书目录
    truststore.inject_into_ssl()
    print("[✓] 已信任系统证书目录(truststore)")

    # 1. 解析并验证参数（在 common 基础上增加通识监控专用参数）
    parser = argparse.ArgumentParser(description="NUIST 通识课(XGKC)容量监控抢选脚本")
    parser.add_argument("-u", "--username", required=False, help="学号")
    parser.add_argument("-p", "--password", required=False, help="密码")
    parser.add_argument("-ck", "--cookie-file", default=None,
                        help="可选：cookie 文件路径。文件格式：key=value; key2=value2; ...")
    parser.add_argument("--skip-login", action="store_true",
                        help="跳过登录流程，从cookie文件读取Authorization token并通过API获取用户信息")
    parser.add_argument("--batch", default=None, help="直接指定轮次 code（跳过轮次选择）")
    parser.add_argument("--swap", action="store_true",
                        help="换课模式：检测到空位先退掉已选旧通识课再抢")
    parser.add_argument("--one-per-category", action="store_true",
                        help="每个通识大类只抢一门：抢到后该大类目标全部停止，其它大类继续（仅纯监控模式）")
    args = parser.parse_args()

    valid, cookie_file_path = validate_arguments(args)
    if not valid:
        return

    # 初始化日志系统
    if args.username:
        setup_logging(args.username)
    else:
        setup_logging("unknown")

    # 2. 初始化 Session
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)

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
    campus = login_data.get("student", {}).get("campus") or "01"
    login_state.campus = campus

    student_id = args.username or _extract_student_id(login_data)
    if student_id:
        login_state.username = student_id
        if not args.username:
            setup_logging(student_id)
    else:
        print("错误: 无法获取学号（请提供 -u 或确保用户信息含 XH）")
        main_logger.error("学号无效，退出")
        return

    # 6. 选择轮次（优先名字含「通识」的可选轮次）
    batches = login_data.get("student", {}).get("electiveBatchList") or []
    available = [b for b in batches if str(b.get("canSelect")) == "1"]
    if args.batch:
        picked = next((b for b in batches if str(b.get("code")) == args.batch), None)
        if not picked:
            print(f"[✗] 未找到轮次 code={args.batch}")
            return
        print(f"[✓] 使用指定轮次: {picked.get('name')}")
    elif len(available) == 1:
        picked = available[0]
    else:
        tongshi = [b for b in available if "通识" in str(b.get("name", ""))]
        if len(tongshi) == 1:
            picked = tongshi[0]
            print(f"[✓] 自动选择通识轮次: {picked.get('name')}")
        else:
            print("\n--- 可选轮次 ---")
            for i, b in enumerate(available, 1):
                print(f"  [{i}] [{b.get('typeName', '')}] {b.get('name')} "
                      f"({b.get('beginTime')} - {b.get('endTime')})")
            while True:
                try:
                    choice = int(input("请选择轮次序号: ").strip())
                    if 1 <= choice <= len(available):
                        picked = available[choice - 1]
                        break
                    print("无效的序号，请重新输入。")
                except ValueError:
                    print("请输入一个有效的数字。")

    batch_id = str(picked.get("code"))
    login_state.batch_id = batch_id
    print(f"[✓] 已选择轮次: {picked.get('name')} (code={batch_id})")

    # 课程类型：以服务端 menuList 为准，兜底 XGKC
    course_type = resolve_course_type(session, token, batch_id, picked.get("name", ""))
    if not course_type:
        print("已取消。")
        return
    if course_type != COURSE_TYPE_XGKC:
        print(f"[!] 提示: 服务端菜单解析出类型 {course_type}，通识监控默认期望 XGKC，仍按 {course_type} 提交")

    # 7. 运行课程选择和抢课流程
    run_course_selection(
        session=session,
        token=token,
        batch_id=batch_id,
        campus=login_state.campus,
        username=student_id,
        use_vpn=use_vpn,
        course_type=course_type,
        swap=args.swap,
        one_per_category=args.one_per_category,
    )

    input("回车键退出")


if __name__ == "__main__":
    main()
