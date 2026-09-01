# NUIST 选课脚本

南信大选课系统的自动化小工具。登录、VPN、验证码 OCR 这些基建都写好了，剩下就是对着不同场景挑对脚本。

> 校内直连或走 WebVPN 都行（`BASE_URL` 里切）。VPN 登录用同目录下的 `vpnlogin.py`。

```bash
pip install -r requirements.txt
```

注：如遇问题，欢迎随时issue区反馈（直接PR更喜欢）。让AI审查并修了几个问题，没真机测过
仓库初始提交的版本应当是正常能用的，可在遇到问题时先尝试那个

---

## 我该用哪个？

日常真正有用的就四个：

| 你想干什么 | 用这个 |
|-----------|--------|
| 正选开抢，一门不行换备选 | **`course.py`** |
| 已经选上了，想盯着空位换更好的 | **`course_cron.py`** |
| 通识课退改选蹲空位自动抢（不要求先退课） | **`xgkc_monitor.py`** |
| 通识选修哪门竞争小一点 | **`xgkc_query.py`** |

剩下的 `course_backup_v1/v2`、`course_desperated` 都是演进过程里留下来的，能跑但别当主力。`xgkc_common.py` 是通识课共享筛选模块（网课识别/时间解析/筛选器）。

---

## 快速上手

```bash
# 正选抢课
python course.py -u 学号 -p 密码

# 监控换课（先选目标课，再选要退的课）
python course_cron.py -u 学号 -p 密码

# 通识课监控抢选（纯监控；--swap 退旧换新；--one-per-category 每类抢一门）
python xgkc_monitor.py -u 学号 -p 密码
python xgkc_monitor.py -u 学号 -p 密码 --one-per-category   # 每个大类只抢一门，抢到即停该类
python xgkc_monitor.py -u 学号 -p 密码 --swap          # 退旧换新模式

# 查通识选修竞争比
python xgkc_query.py -u 学号 -p 密码
```

已经有 cookie / token 的话可以跳过登录：

```bash
python course.py --skip-login -ck ck.txt
python xgkc_query.py -t <token> -c ck.txt
```

日志会按学号丢到 `logs/{学号}/` 下面，主操作一份、心跳一份。

---

## 脚本对比

从最早的裸循环抢课，一路迭代到现在这几份，大致是这么走的：

```
v1 单课硬抢
 └─ v2 + WebSocket 保活 + 备选队列
      └─ desperated  啥都想塞进去（多课类、志愿……）
           ├─ course.py       正选抢课（终态）
           ├─ course_cron.py  监控空位换课（终态）
           ├─ xgkc_monitor.py 通识监控抢选（course_cron 的通识版）
           └─ xgkc_query.py   通识查询（旁支）
```

### 功能一览

| 能力 | v1 | v2 | desperated | course | cron | monitor | xgkc |
|------|:--:|:--:|:----------:|:------:|:----:|:-------:|:----:|
| 登录 / OCR / VPN | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| cookie 跳过登录 | | | ✓ | ✓ | ✓ | ✓ | ✓ |
| 课程类型 | FANKC 写死 | 同左 | 6 种 | FANKC + 体育 | 同左 | 仅通识 | 仅通识 |
| 备选课切换 | | ✓ | ✓ | ✓ | | 多目标同监 | — |
| WebSocket | | 保活 | 保活 | **成功/失败回调** | 保活 | **成功/失败回调** | |
| HTTP 心跳 + 自动重登 | | | ✓ | ✓ | ✓ | ✓ | |
| 志愿预选 | | | ✓ | | | — | — |
| 退课 / 容量监控换课 | | | | | ✓ | ✓(--swap) | |
| 竞争比排序查询 | | | | | | | ✓ |

### 各自在干什么

**`course_backup_v1.py`**（373 行）  
最初能跑通的原型。选一门课，0.3s 一轮死磕 `/add`。没有备选、没有 WS、类型写死。留着当考古。

**`course_backup_v2.py`**（611 行）  
在 v1 上加了 WebSocket 心跳，以及主选 + 备选队列（满了就切下一门）。成功还是看 HTTP 返回码，基建也比较糙。

**`course_desperated.py`**（1465 行）  
功能最杂的那一版：六种课类、预选志愿模式（按顺序填 1~N 志愿，满了跳过）、LoginState、分日志、自动重登……东西都堆齐了，但正选和志愿搅在一起，后面干脆拆开。文件名拼成 `desperated` 也挺应景。

**`course.py`**（1691 行）— 正选主力  
方案内课 + 体育课。核心认知是：`/add` 只是进队列，真正选上靠 WebSocket 推 `"选课成功"`；满员同样靠 WS 回调，立刻切备选。HTTP 心跳挂着保登录态，掉了会自动重登。支持返回上一级重选轮次。

**`course_cron.py`**（1611 行）— 换课专用  
基建和 `course.py` 差不多，主流程完全不同：

1. 选一门目标课  
2. 再选一门要退掉的已选课  
3. 每 5 秒查一次容量，有空位就：退旧 → 选新

所以它有退课接口、`get_course_capacity`，没有多备选切换。适合「已经有课兜底，想换更好的」这种场景。

**`xgkc_query.py`**（707 行）— 只查不选  
通识选修专用。多关键词逗号搜、合并去重，按 **一志愿人数 / 课容量** 算竞争比，升序排出来给你挑。颜色大致是：绿 < 0.75、黄 < 1.2、红再往上。VPN cookie 会缓存在 `vpn_cookie.txt`。

**`xgkc_monitor.py`** — 通识课监控抢选  
结构与 `course_cron.py` 对应（choose_class / drop_class / start_monitoring 等），差异：

1. 默认「纯监控抢选」：不要求先退课，多目标同时监控，有空位就抢（0.3s 连抢，满员转下一个目标）
2. `--swap` 退旧换新：先退已选旧课再抢，失败自动回补
3. `--one-per-category`：每个大类抢到一门后该大类全部停止，其它大类继续

选择界面按 `f` 筛选：仅网课 / 排除时间冲突 / 通识类别（大类，多选）/ 教师 / 星期；冲突目标会自动移出监控。筛选逻辑在 `xgkc_common.py`。

---

## 小提示

- 默认走 WebVPN。人在校内可以直接把 `BASE_URL` 改成注释里那行校内地址。
- 抢课别太猛，403 基本就是被限频了，脚本里已经会吞掉这类噪音。
- `xgkc_monitor.py` 与其它脚本一样把日志写入 `logs/{学号}/`；监控期间终端**一行状态实时覆盖刷新**（不保留旧状态行），空位/选课/退课/回补/重登等关键事件另起一行打印，状态变化与事件细节全部在日志文件里。
- cookie 文件格式就是浏览器里复制那种：`key=value; key2=value2; ...`
- 备份脚本和压缩包可以无视，不影响正常使用。
- `ck.txt`、`vpn_cookie.txt`、日志这些已经写进 `.gitignore` 了，别手贱 push 上去。

有问题就翻日志，`logs/` 里按学号分好了。
