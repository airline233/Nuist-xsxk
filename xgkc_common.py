"""xgkc_common.py — 通识课(XGKC)数据结构与筛选逻辑共享模块。

纯数据函数，无网络请求，供 xgkc_monitor.py（及后续需要通识课筛选的脚本）共用。

基于探针确认的真实字段（probe_output/xgkc_structure.json）：
- 行结构扁平：每行一个教学班，JXBID/secretVal 直接在行上
- SKSJ: list[dict] 结构化上课时间块：
    SKXQ("1"~"7" 星期) / KSJC(开始节次) / JSJC(结束节次) / SKZCMC("2-17周") / YPSJDD(地点)
- 网课标识：teachingMethod=="网络教学"；兜底 SKJS=="网络教师" 或 KCM 含 "（网络课程）"
- 服务端冲突：SFCT=="1" 或 conflictDesc 非空
- 容量：YXRS(已选) / KRL(容量) / SFYM("1"=已满) / SFYX("1"=已选)
"""
from __future__ import annotations

import re

# 星期名称映射：SKXQ "1"~"7" -> 中文简称
WEEKDAY_SHORT_CN = {
    "1": "周一", "2": "周二", "3": "周三", "4": "周四",
    "5": "周五", "6": "周六", "7": "周日",
}

# 网课识别关键词（除 teachingMethod 外的兜底）
ONLINE_KCM_MARKERS = ("（网络课程）", "(网络课程)")
ONLINE_TEACHER_MARKERS = ("网络教师",)

# 通识类别大类（XGXKLB 取「（」之前的大类，供交互菜单展示；多选）
# 实际数据里大类只有三种：人文社科类 / 公共艺术类 / 自然科学类
XGXKLB_MAJOR_CATEGORIES = ["人文社科类", "公共艺术类", "自然科学类"]

# 教学方式（来自实际数据 teachingMethod 的不同取值，供交互菜单展示）
TEACHING_METHODS = ["网络教学", "理论教学", "实验教学"]


def is_online_course(row) -> bool:
    """判断是否为网络课程（以 teachingMethod 为准，多重兜底）。"""
    if not isinstance(row, dict):
        return False
    if str(row.get("teachingMethod", "")).strip() == "网络教学":
        return True
    if str(row.get("SKJS", "")).strip() in ONLINE_TEACHER_MARKERS:
        return True
    kcm = str(row.get("KCM", ""))
    return any(m in kcm for m in ONLINE_KCM_MARKERS)


def is_conflict(row) -> bool:
    """是否与已选课时间冲突（服务端标记）。"""
    if not isinstance(row, dict):
        return False
    if str(row.get("SFCT", "")) == "1":
        return True
    return bool(str(row.get("conflictDesc", "")).strip())


def is_full(row) -> bool:
    """是否已满（优先 SFYM，其次人数对比）。"""
    if not isinstance(row, dict):
        return True
    if str(row.get("SFYM", "")) == "1":
        return True
    try:
        return int(row.get("YXRS", 0)) >= int(row.get("KRL", 0))
    except (TypeError, ValueError):
        return False


def is_selected(row) -> bool:
    return str(row.get("SFYX", "")) == "1"


def xgxklb_major(row):
    """取通识类别大类：XGXKLB 中「（」之前的部分；无括号时返回原值。

    例: "人文社科类（劳动与生活）" -> "人文社科类"；"在线开放课" -> "在线开放课"
    """
    value = str(row.get("XGXKLB", "") or "").strip()
    if "（" in value:
        return value.split("（", 1)[0].strip()
    if "(" in value:
        return value.split("(", 1)[0].strip()
    return value


def display_width(text):
    """计算字符串的显示宽度（中文/全角/emoji=2，英文=1）。"""
    width = 0
    for ch in text:
        code = ord(ch)
        if ('\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f'
                or '\uff00' <= ch <= '\uffef' or code > 0x2E7F):
            width += 2
        else:
            width += 1
    return width


def truncate_by_width(text, max_width, placeholder="…"):
    """按显示宽度截断，保证不超过 max_width 列（中文按 2 列计）。"""
    if display_width(text) <= max_width:
        return text
    if max_width <= 1:
        return ""
    out = ""
    width = 0
    for ch in text:
        w = 2 if display_width(ch) == 2 else 1
        if width + w > max_width - 1:  # 预留 1 列给省略号
            break
        out += ch
        width += w
    return out + placeholder


def time_blocks(row):
    """从 SKSJ 提取结构化上课时间块。

    Returns:
        list of dict: {weekday:int, ksjc:int, jsjc:int, weeks:str, place:str}
        解析失败或无 SKSJ 时返回 []
    """
    if not isinstance(row, dict):
        return []
    blocks = []
    for item in row.get("SKSJ") or []:
        if not isinstance(item, dict):
            continue
        try:
            weekday = int(str(item.get("SKXQ", "")).strip())
            ksjc = int(str(item.get("KSJC", "")).strip())
            jsjc = int(str(item.get("JSJC", "")).strip())
        except (TypeError, ValueError):
            continue
        blocks.append({
            "weekday": weekday,
            "ksjc": ksjc,
            "jsjc": jsjc,
            "weeks": str(item.get("SKZCMC", "")),
            "place": str(item.get("YPSJDD", "")).strip(),
        })
    return blocks


def row_weekdays(row):
    """该课程涉及的所有星期（去重，升序）。"""
    return sorted({b["weekday"] for b in time_blocks(row)})


def format_time_blocks(row, sep=", "):
    """把上课时间块渲染成『5-14周 周二 第9-10节』样式的短串。"""
    parts = []
    for b in time_blocks(row):
        week = WEEKDAY_SHORT_CN.get(str(b["weekday"]), f"周{b['weekday']}")
        parts.append(f"{b['weeks']} {week} 第{b['ksjc']}-{b['jsjc']}节")
    return sep.join(parts) if parts else "-"


def format_places(row, sep=", "):
    """上课地点串（网课为空时给『网络』）。"""
    places = [b["place"] for b in time_blocks(row) if b["place"]]
    if not places:
        return "网络" if is_online_course(row) else "-"
    return sep.join(places)


# --- 筛选器 ---

def parse_weekday_input(text):
    """解析星期输入：支持 '2' / '二' / '周二' / '星期二' / '2,3' / '2-4'。

    Returns:
        set of int(1..7)；无法解析返回 set()
    """
    text = (text or "").strip()
    if not text:
        return set()
    result = set()

    def add_num(n):
        if 1 <= n <= 7:
            result.add(n)

    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) == 2 and bounds[0].strip().isdigit() and bounds[1].strip().isdigit():
                lo, hi = int(bounds[0]), int(bounds[1])
                for n in range(min(lo, hi), max(lo, hi) + 1):
                    add_num(n)
                continue
        # 中文：周一/星期一/一
        for num, names in (
            (1, ("1", "一", "周一", "星期一")),
            (2, ("2", "二", "周二", "星期二")),
            (3, ("3", "三", "周三", "星期三")),
            (4, ("4", "四", "周四", "星期四")),
            (5, ("5", "五", "周五", "星期五")),
            (6, ("6", "六", "周六", "星期六")),
            (7, ("7", "日", "天", "周日", "周天", "星期日", "星期天")),
        ):
            if part in names:
                add_num(num)
                break
        else:
            if part.isdigit():
                add_num(int(part))
    return result


def parse_period_input(text):
    """解析节次输入：支持 '9' / '9-10' / '9,10'。

    Returns:
        (start, end) 或 None：start/end 为 int，单节时两者相同
    """
    text = (text or "").strip()
    if not text:
        return None
    if "-" in text:
        bounds = text.split("-")
        if len(bounds) == 2 and bounds[0].strip().isdigit() and bounds[1].strip().isdigit():
            return int(bounds[0]), int(bounds[1])
    if text.isdigit():
        n = int(text)
        return n, n
    return None


def build_filters(
    categories=None, teacher=None, weekdays=None, period=None,
    online_only=False, no_conflict=False, not_full=False,
    exclude_selected=False, xf=None, xq=None, kkdw=None,
    teaching_methods=None,
):
    """构造筛选条件字典（值为 None/空表示不过滤）。"""
    return {
        "categories": [c for c in (categories or []) if c],
        "teacher": (teacher or "").strip(),
        "weekdays": set(weekdays or []),
        "period": period,  # (start, end) or None
        "online_only": bool(online_only),
        "no_conflict": bool(no_conflict),
        "not_full": bool(not_full),
        "exclude_selected": bool(exclude_selected),
        "xf": [x for x in (xf or []) if x],
        "xq": (xq or "").strip(),
        "kkdw": (kkdw or "").strip(),
        "teaching_methods": [m for m in (teaching_methods or []) if m],
    }


def matches_filters(row, filters) -> bool:
    """判断一行是否满足全部筛选条件。"""
    f = filters or {}
    if not isinstance(row, dict):
        return False

    # 通识类别：按大类匹配（大类未命中时再尝试全名精确匹配，兼容无括号的取值）
    if f.get("categories"):
        raw_cat = str(row.get("XGXKLB", ""))
        major = xgxklb_major(row)
        if not (major in f["categories"] or raw_cat in f["categories"]):
            return False

    # 教师：子串匹配
    if f.get("teacher"):
        if f["teacher"] not in str(row.get("SKJS", "")):
            return False

    # 星期：任一时间块命中
    if f.get("weekdays"):
        if not (set(row_weekdays(row)) & f["weekdays"]):
            return False

    # 节次：任一时间块与目标区间重叠
    if f.get("period"):
        p_start, p_end = f["period"]
        hit = any(
            b["ksjc"] <= p_end and b["jsjc"] >= p_start
            for b in time_blocks(row)
        )
        if not hit:
            return False

    if f.get("online_only") and not is_online_course(row):
        return False
    if f.get("no_conflict") and is_conflict(row):
        return False
    if f.get("not_full") and is_full(row):
        return False
    if f.get("exclude_selected") and is_selected(row):
        return False

    # 教学方式：精确匹配任一
    if f.get("teaching_methods"):
        if str(row.get("teachingMethod", "")).strip() not in f["teaching_methods"]:
            return False

    # 学分：精确匹配任一
    if f.get("xf"):
        if str(row.get("XF", "")) not in f["xf"]:
            return False

    # 校区：包含匹配
    if f.get("xq"):
        if f["xq"] not in str(row.get("XQ", "")):
            return False

    # 开课单位：子串匹配
    if f.get("kkdw"):
        if f["kkdw"] not in str(row.get("KKDW", "")):
            return False

    return True


def apply_filters(rows, filters):
    """对行列表应用筛选。"""
    return [r for r in rows if matches_filters(r, filters)]


def describe_filters(filters) -> str:
    """把筛选条件渲染成一行说明文本（用于结果标题/提示）。"""
    parts = []
    if filters.get("categories"):
        parts.append("类别:" + "/".join(filters["categories"]))
    if filters.get("teacher"):
        parts.append(f"教师含:{filters['teacher']}")
    if filters.get("weekdays"):
        names = [WEEKDAY_SHORT_CN.get(str(w), str(w)) for w in sorted(filters["weekdays"])]
        parts.append("星期:" + "/".join(names))
    if filters.get("period"):
        s, e = filters["period"]
        parts.append(f"节次:{s}-{e}")
    if filters.get("online_only"):
        parts.append("仅网课")
    if filters.get("no_conflict"):
        parts.append("无冲突")
    if filters.get("not_full"):
        parts.append("未满")
    if filters.get("exclude_selected"):
        parts.append("排除已选")
    if filters.get("teaching_methods"):
        parts.append("教学方式:" + "/".join(filters["teaching_methods"]))
    if filters.get("xf"):
        parts.append("学分:" + "/".join(filters["xf"]))
    if filters.get("xq"):
        parts.append(f"校区:{filters['xq']}")
    if filters.get("kkdw"):
        parts.append(f"单位含:{filters['kkdw']}")
    return " | ".join(parts) if parts else "无"


def filter_menu(filters):
    """交互式设置筛选条件（菜单风格与 resolve_course_type 一致），原地修改 filters。

    交互约定：
    - 一次输入可多选（逗号/空格分隔），如 "1,2" 同时切换多个开关；
    - 开关类（仅网络课程/排除时间冲突）：选一次开启，再选一次关闭；
    - 通识类别（大类）：已选项带 ✓，再选一次删除，0 清空。
    """
    COL = 26  # 菜单列宽（显示宽度，中文按 2 列计）

    def pad(text):
        return text + " " * max(0, COL - display_width(text))

    def mark(on):
        return "[✓]" if on else "[ ]"

    while True:
        print("\n--- 筛选条件（当前生效）---")
        print(f"  {describe_filters(filters) or '无'}")
        print("  " + pad(f"[1] 仅网络课程{mark(filters.get('online_only'))}")
              + pad(f"[2] 排除时间冲突{mark(filters.get('no_conflict'))}")
              + "[3] 通识类别(大类)")
        print("  " + pad("[4] 教师") + pad("[5] 星期") + "[0] 返回")

        value = input("请输入序号（多个用逗号/空格分隔，0返回）: ").strip()
        try:
            choices = [int(x) for x in re.split(r"[,\s]+", value) if x.strip()]
        except ValueError:
            print("请输入一个有效的数字。")
            continue
        if not choices:
            continue
        if 0 in choices:
            return filters

        for choice in choices:
            if choice == 1:
                filters["online_only"] = not filters.get("online_only", False)
            elif choice == 2:
                filters["no_conflict"] = not filters.get("no_conflict", False)
            elif choice == 3:
                current = list(filters.get("categories") or [])
                print("通识类别（大类，已选带 ✓，再选一次删除，0 清空）:")
                for i, cat in enumerate(XGXKLB_MAJOR_CATEGORIES, 1):
                    print(f"  [{i}] {'[✓]' if cat in current else '[ ]'} {cat}")
                value2 = input("请输入类别序号（多个用逗号分隔）: ").strip()
                if "0" in re.split(r"[,\s]+", value2):
                    filters["categories"] = []
                else:
                    for part in re.split(r"[,\s]+", value2):
                        if part.isdigit() and 1 <= int(part) <= len(XGXKLB_MAJOR_CATEGORIES):
                            cat = XGXKLB_MAJOR_CATEGORIES[int(part) - 1]
                            if cat in current:
                                current.remove(cat)
                            else:
                                current.append(cat)
                    filters["categories"] = current
            elif choice == 4:
                filters["teacher"] = input("请输入教师姓名关键字（空=清空）: ").strip()
            elif choice == 5:
                value2 = input("请输入星期（如 2 / 周二 / 2,4 / 2-4，空=清空）: ").strip()
                weekdays = parse_weekday_input(value2)
                filters["weekdays"] = weekdays if weekdays else set()
                if not filters["weekdays"]:
                    print("[!] 未识别出有效星期，已清空该条件")
            else:
                print(f"无效的选择: {choice}，已跳过。")
