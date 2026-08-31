"""probe_xgkc.py — 通识课(XGKC)接口结构探针。

用途：在本机运行，把通识课列表接口的【脱敏】结构报告写到 probe_output/ 目录，
供开发者确认字段格式（teachingPlace 的星期/节次写法、网课标识字段等）。
账号密码只在本机命令行使用，绝不写入任何产物文件。

用法：
    python probe_xgkc.py -u 学号 -p 密码
    python probe_xgkc.py -u 学号 -p 密码 --no-vpn          # 校内直连，不走 WebVPN
    python probe_xgkc.py -t <token> -c ck.txt               # 跳过登录（需已有有效 cookie）
    python probe_xgkc.py -u 学号 -p 密码 --batch <code>     # 直接指定轮次，跳过交互

产物（probe_output/）：
    batches.json              轮次列表（仅 code/name/typeName/canSelect/起止时间，公共信息）
    xgkc_envelope.json        响应外层结构（data 下有哪些键）
    xgkc_structure.json       字段结构报告：字段名/类型/样例值(截断)/覆盖行数
    xgkc_sample_rows.json     最多 5 条【未选】课程完整原始行（公开课程信息）
    xgkc_online_markers.json  含"网络/线上/在线"字样的字段匹配情况（定位网课标识字段）
    xgkc_selected_masked.json 已选课(SFYX=1)：仅字段名+值类型，值全部打码

运行前请自行浏览这些文件，确认无敏感内容后再交给 AI 分析。
"""
import argparse
import json
import os
import sys

try:
    import requests
    import truststore
except ImportError:
    sys.exit("缺少依赖，请先执行: pip install -r requirements.txt")

from vpnlogin import NuistVPNClient

# 复用 xgkc_query 的登录/查询逻辑
try:
    from xgkc_query import (
        BASE_URL, COURSE_TYPE, CAMPUS,
        load_vpn_cookies, save_vpn_cookies, parse_cookies,
        login, get_user_info_from_api, switch_batch, query_all_courses,
    )
except ImportError as e:
    sys.exit(f"导入 xgkc_query 失败: {e}\n请先执行: pip install -r requirements.txt")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_output")

MAX_STRING_LEN = 60      # 样例值截断长度
MAX_SAMPLE_ROWS = 5      # 完整样例行数
MAX_DISTINCT = 30        # 每个字段最多统计多少个不同取值


def truncate_value(value):
    """样例值展示前截断，避免超长字符串。"""
    if isinstance(value, str):
        return value[:MAX_STRING_LEN] + ("..." if len(value) > MAX_STRING_LEN else "")
    return value


def describe_type(value):
    """返回值的类型描述字符串。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        inner = describe_type(value[0]) if value else "?"
        return f"list[{inner}]"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def mask_value(value):
    """已选课数据打码：只保留类型和长度，不保留任何实际值。"""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "str", "len": len(value)}
    if isinstance(value, list):
        return {"type": f"list", "len": len(value),
                "item_type": describe_type(value[0]) if value else "?"}
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(value.keys())}
    return {"type": type(value).__name__}


def scan_online_markers(rows):
    """递归扫描所有行的字符串字段，找出含网课特征词的字段与样例。

    Returns:
        list of {field_path, samples: [...]}
    """
    markers = ("网络", "线上", "在线", "慕课", "MOOC", "mooc", "直播", "录播")
    hits = {}

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            for m in markers:
                if m in obj:
                    entry = hits.setdefault(path, {"matched_keywords": set(), "samples": []})
                    entry["matched_keywords"].add(m)
                    if len(entry["samples"]) < 10:
                        entry["samples"].append(truncate_value(obj))
                    break

    for idx, row in enumerate(rows):
        walk(row, f"rows[{idx}]")

    result = []
    for path, info in hits.items():
        result.append({
            "field_path": path,
            "matched_keywords": sorted(info["matched_keywords"]),
            "samples": info["samples"],
        })
    result.sort(key=lambda x: x["field_path"])
    return result


def build_structure_report(rows):
    """汇总所有行出现过的字段：类型、样例、覆盖行数、不同取值。"""
    fields = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            info = fields.setdefault(key, {
                "type": describe_type(value),
                "null_count": 0,
                "count": 0,
                "distinct": set(),
                "example": None,
            })
            info["count"] += 1
            if value is None:
                info["null_count"] += 1
                continue
            if info["example"] is None:
                info["example"] = truncate_value(value)
            info["distinct"].add(repr(value)[:MAX_STRING_LEN])
            if len(info["distinct"]) > MAX_DISTINCT:
                info["distinct"].add("...(更多)")

    report = {}
    for key in sorted(fields):
        info = fields[key]
        report[key] = {
            "type": info["type"],
            "count": info["count"],
            "null_count": info["null_count"],
            "example": info["example"],
            "distinct_values": sorted(info["distinct"]),
        }
    return report


def save_json(name, payload):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def pick_batch(batches, batch_arg):
    """选择轮次：--batch 指定 > 唯一可选自动 > 交互选择。"""
    selectable = [b for b in batches if str(b.get("canSelect")) == "1"]
    if batch_arg:
        for b in batches:
            if str(b.get("code")) == batch_arg:
                return b
        print(f"[!] 未找到轮次 code={batch_arg}，将交互选择")
    if not batch_arg and len(selectable) == 1:
        return selectable[0]

    print("\n可用的选课轮次：")
    for i, b in enumerate(batches, 1):
        flag = "✓可选" if str(b.get("canSelect")) == "1" else "×不可选"
        print(f"  [{i}] [{flag}] [{b.get('typeName', '')}] {b.get('name', '')} "
              f"(code={b.get('code')}) {b.get('beginTime', '')}~{b.get('endTime', '')}")
    while True:
        try:
            choice = int(input("请输入轮次编号: ").strip())
            if 1 <= choice <= len(batches):
                return batches[choice - 1]
        except ValueError:
            pass
        print("输入无效，请重新输入编号。")


def main():
    parser = argparse.ArgumentParser(description="通识课(XGKC)接口结构探针（本地运行，产出脱敏报告）")
    parser.add_argument("-u", "--username", help="学号")
    parser.add_argument("-p", "--password", help="密码")
    parser.add_argument("-t", "--token", help="直接使用 Token（跳过选课系统登录）")
    parser.add_argument("-c", "--cookie", help="Cookie 文件路径")
    parser.add_argument("--no-vpn", action="store_true", help="不走 WebVPN（校内直连）")
    parser.add_argument("--batch", help="直接指定轮次 code，跳过交互")
    args = parser.parse_args()

    try:
        truststore.inject_into_ssl()
    except Exception:
        pass

    session = requests.Session()
    use_vpn = not args.no_vpn

    # 1. VPN（复用 xgkc_query 的 cookie 缓存逻辑）
    if use_vpn:
        vpn_cookies = load_vpn_cookies()
        login_flag = 0
        if vpn_cookies:
            session.cookies.update(vpn_cookies)
            resp = session.get(BASE_URL, timeout=20)
            if "login" in resp.url:
                print("保存的 VPN Cookie 已失效，重新登录 VPN...")
                login_flag = 1
            else:
                print("[✓] 已复用保存的 VPN Cookie")
        else:
            login_flag = 1
        if login_flag:
            username = args.username or input("请输入学号: ")
            password = args.password or input("请输入密码: ")
            client = NuistVPNClient(username=username, password=password)
            session.cookies.update(client.login_and_get_cookies())
            save_vpn_cookies({c.name: c.value for c in session.cookies})
            print("[✓] VPN 登录成功")

    # 2. cookie 文件
    if args.cookie:
        cookie_path = args.cookie if os.path.isabs(args.cookie) \
            else os.path.join(os.path.dirname(os.path.abspath(__file__)), args.cookie)
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                session.cookies.update(parse_cookies(f.read().strip()))
            print("[✓] Cookie 文件已加载")
        except Exception as e:
            print(f"[!] Cookie 文件加载失败: {e}")

    # 3. 选课系统登录
    if args.token:
        token = args.token
        student_data = get_user_info_from_api(session, token)
        if not student_data:
            sys.exit("Token 已失效，请重新登录或检查 Token")
    else:
        username = args.username or input("请输入学号: ")
        password = args.password or input("请输入密码: ")
        print("正在登录选课系统（验证码自动识别，可能需要多次重试）...")
        student_data = login(session, username, password)
        if not student_data:
            sys.exit("登录失败")
        token = student_data.get("token")

    batches = (student_data.get("student") or {}).get("electiveBatchList") or []
    if not batches:
        sys.exit("未获取到任何轮次信息")

    batch = pick_batch(batches, args.batch)
    batch_id = str(batch.get("code"))
    print(f"[✓] 已选择轮次: [{batch.get('typeName', '')}] {batch.get('name')} (code={batch_id})")

    if not switch_batch(session, token, batch_id):
        print("[!] 切换轮次失败，仍继续尝试查询（部分轮次切换失败也能拉到数据）")

    # 4. 查询全部通识课
    print("正在拉取通识课全量列表...")
    rows = query_all_courses(session, token, batch_id)
    if not rows:
        print("[!] 当前轮次查询不到通识课数据。可能原因：")
        print("    1. 该轮次不是通识课(XGKC)轮次，试试用 --batch 换一个轮次")
        print("    2. 该轮次尚未开放课程查询")
        sys.exit(1)
    print(f"[✓] 拉到 {len(rows)} 行数据，开始生成报告...")

    unselected = [r for r in rows if str(r.get("SFYX", "")) != "1"]
    selected = [r for r in rows if str(r.get("SFYX", "")) == "1"]

    # 5. 生成产物
    produced = []

    produced.append(save_json("batches.json", [
        {k: b.get(k) for k in ("code", "name", "typeName", "canSelect", "beginTime", "endTime")}
        for b in batches
    ]))

    produced.append(save_json("xgkc_envelope.json", {
        "note": "list 接口响应 data 下有哪些键（rows 为课程行列表）",
        "row_count": len(rows),
        "sample_row_keys": sorted(rows[0].keys()) if rows else [],
    }))

    produced.append(save_json("xgkc_structure.json", build_structure_report(rows)))

    sample = unselected[:MAX_SAMPLE_ROWS]
    produced.append(save_json("xgkc_sample_rows.json", {
        "note": f"最多 {MAX_SAMPLE_ROWS} 条【未选】课程的完整原始行（公开课程信息）",
        "sample_count": len(sample),
        "rows": sample,
    }))

    produced.append(save_json("xgkc_online_markers.json", {
        "note": "含『网络/线上/在线/慕课/直播/录播』字样的字段路径与样例，用于定位网课标识字段",
        "total_rows_scanned": len(rows),
        "hits": scan_online_markers(rows),
    }))

    produced.append(save_json("xgkc_selected_masked.json", {
        "note": f"已选课(SFYX=1)共 {len(selected)} 行：仅保留字段名与值类型，值全部打码",
        "selected_count": len(selected),
        "rows": [{k: mask_value(v) for k, v in r.items()} for r in selected],
    }))

    print("\n" + "=" * 60)
    print("探针完成！产物文件（请先自行检查内容，再发给 AI 分析）：")
    for p in produced:
        print(f"  - {p}")
    print("=" * 60)
    print(f"统计：共 {len(rows)} 行 | 未选 {len(unselected)} | 已选 {len(selected)}")
    print("提示：密码/Token 未写入任何文件；已选课数据已全部打码。")


if __name__ == "__main__":
    main()
