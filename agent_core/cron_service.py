"""定时任务服务 —— 自然语言 → cron 解析 + 调度执行。

不引入第三方 cron 依赖，用标准库手工实现：
  - parse_cron_expr()：中文自然语言 → 5 段 cron（分 时 日 月 周）
  - cron_matches()：判断某时间点是否符合 cron（支持分/时/日/月/周，含 * / 列表 与 周-日互配）
  - run_scheduler_loop()：asyncio 后台循环，扫启用任务，到点触发 agent 执行

cron 5 段（标准）：
  分钟(0-59) 小时(0-23) 日(1-31) 月(1-12) 周(0-7, 0和7均为周日)
"""
import asyncio
import re
from datetime import datetime

import session_store

# ------------------------------------------------------------------
# 自然语言 → cron
# ------------------------------------------------------------------

_WEEKDAY_CN = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 0,
}
_MONTH_CN = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "10": 10, "11": 11, "12": 12,
}


_CN_NUM = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}


def _cn_to_int(s: str):
    """把常见中文数字转为 int（支持 一到九、十、两、几十）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        parts = s.split("十")
        a = _CN_NUM.get(parts[0], 0) if parts[0] else 1
        b = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return a * 10 + b
    if s in _CN_NUM:
        return _CN_NUM[s]
    return None


def _parse_time(txt: str) -> tuple | None:
    """解析时间 HH:MM / H点(M分) / HH:MM:SS，返回 (小时, 分钟) 或 None。"""
    txt = txt.strip()
    m = re.search(r"(\d{1,2})[:：点时](\d{0,2})", txt)
    if not m:
        return None
    h = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else 0
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return None
    return (h, mm)


def parse_cron_expr(text: str) -> str:
    """将中文自然语言解析成 5 段 cron 表达式。

    支持（示例）：
      每天 8:30        -> 30 8 * * *
      每天早上9点      -> 0 9 * * *
      每小时            -> 0 * * * *
      每30分钟          -> */30 * * * *
      每2小时           -> 0 */2 * * *
      每周一 9:00       -> 0 9 * * 1
      每周一三五 8:00   -> 0 8 * * 1,3,5
      每月1号 10:00     -> 0 10 1 * *
      每月1号和15号 10:00 -> 0 10 1,15 * *
      每月的第5天      -> 0 0 5 * *
      每天中午12点      -> 0 12 * * *
      每周周末 9:00     -> 0 9 * * 0,6
      凌晨2点           -> 0 2 * * *
    直接传入合法 cron（如 "0 9 * * 1"）则原样返回。
    """
    t = re.sub(r"\s+", " ", text.strip())
    if not t:
        raise ValueError("执行时机不能为空")

    # 直接是 5 段 cron
    if re.match(r"^[0-9*/,]+ [0-9*/,]+ [0-9*/,]+ [0-9*/,]+ [0-9*/,]+$", t):
        # 校验字段范围
        fields = t.split(" ")
        _validate_cron(fields)
        return t

    # ── 普通 `* * * * *` 校验
    # 每 N 分钟/小时（N 支持阿拉伯数字或中文一/两/三…，如「每两小时」「每30分钟」）
    m = re.search(r"每\s*([0-9]+|[\u4e00-\u9fa5]{1,2})\s*分钟", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n is None or n <= 0 or n > 59:
            raise ValueError("每隔分钟数需在 1~59 之间")
        return f"*/{n} * * * *"
    m = re.search(r"每\s*([0-9]+|[\u4e00-\u9fa5]{1,2})\s*(?:个)?小时", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n is None or n <= 0 or n > 23:
            raise ValueError("每隔小时数需在 1~23 之间")
        return f"0 */{n} * * *"
    if "每小时" in t:
        return "0 * * * *"

    # 默认时间 = 每天 0 点，除非后面解析出具体时间
    hm = _parse_time(t)
    if hm:
        minute, hour = str(hm[1]), str(hm[0])
    else:
        minute, hour = "0", "0"

    # 每周几（支持「每周一三五」「每周一到周五」区间；区间优先于列表）
    week_days = None
    rng = re.search(r"周([一二三四五六日天])(?:到|至|[~\-])周?([一二三四五六日天])", t)
    if rng:
        a = _WEEKDAY_CN.get(rng.group(1)); b = _WEEKDAY_CN.get(rng.group(2))
        if a is not None and b is not None:
            if a < b:
                week_days = list(range(a, b + 1))
            elif a == 6 and b == 0:  # 周六到周日
                week_days = [6, 0]
    if week_days is None:
        wd_match = re.search(r"周([一二三四五六日天0-7])(?:[一二三四五六日天0-7])*", t)
        if wd_match:
            seg = t[wd_match.start():wd_match.end()]
            week_days = [_WEEKDAY_CN[c] for c in seg[1:] if c in _WEEKDAY_CN]
    if not week_days and "周末" in t:
        week_days = [0, 6]
    elif not week_days and "工作日" in t:
        week_days = [1, 2, 3, 4, 5]

    # 每月几号（支持「每月1号」「每月1号和15号」「每月5、10号」「每月的第5天」「每月第5天」「每月 1,15 号」）
    mday = None
    # 优先整体提取「每月...号/日/天」片段，再取其中所有数字
    m_month = re.search(r"月[^号日天]{0,20}?([0-9]+(?:\D*[0-9]+)*)\s*[号日天]", t)
    if m_month:
        seg = t[m_month.start():m_month.end()]
        days = [int(x) for x in re.findall(r"\d+", seg)]
        days = [d for d in days if 1 <= d <= 31]
        if days and len(days) <= 20:
            mday = [str(d) for d in days]
    # 「每周几」优先于「每月几号」（两者互斥时，星期几更常指代每周）
    if week_days:
        mday = None

    # 组装
    mday_s = ",".join(mday) if mday else "*"
    dow_s = ",".join(str(d) for d in week_days) if week_days else "*"
    # 如果指定了星期几，日期用 *（cron 语义）
    if dow_s != "*":
        mday_s = "*"
    return f"{minute} {hour} {mday_s} * {dow_s}"


def _validate_cron(fields: list) -> None:
    """校验 5 段 cron 字段范围（含步进/列表）。"""
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for i, (lo, hi) in enumerate(ranges):
        for part in fields[i].split(","):
            base = part.split("/")[0]
            if base == "*":
                continue
            try:
                val = int(base)
            except ValueError:
                raise ValueError(f"cron 字段 {i+1} 格式错误: {part}")
            if not (lo <= val <= hi):
                raise ValueError(f"cron 字段 {i+1} 超出范围: {val}")

# ------------------------------------------------------------------
# cron 匹配
# ------------------------------------------------------------------

def _field_matches(value: int, expr: str, lo: int, hi: int) -> bool:
    """判断 value 是否命中 cron 单个字段（支持 * / 列表）。周日归一为 6。"""
    if expr == "*":
        return True
    if value == 7:  # 周日
        value = 0
    for part in expr.split(","):
        part = part.strip()
        if part == "*":
            return True
        if "/" in part:
            base, step = part.split("/", 1)
            try:
                step = int(step)
            except ValueError:
                continue
            if base == "*":
                low, high = lo, hi
            else:
                # 形如 0/15 或 10/30（区间起点步进）
                try:
                    low = int(base)
                except ValueError:
                    low = lo
                high = hi
            if value >= low and (value - low) % step == 0 and value <= high:
                return True
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def cron_matches(cron: str, dt: datetime) -> bool:
    """判断 dt 是否命中 cron（5 段）。支持周-日互配：
    标准 cron 中，若周字段为 * 则只看月日，否则若月日字段非 * 需同时满足其一。
    """
    fields = cron.split(" ")
    if len(fields) != 5:
        return False
    minute, hour, mday, month, dow = fields
    if not _field_matches(dt.minute, minute, 0, 59):
        return False
    if not _field_matches(dt.hour, hour, 0, 23):
        return False
    if not _field_matches(dt.month, month, 1, 12):
        return False
    dow_m = _field_matches((dt.weekday() + 1) % 7, dow, 0, 6)  # ISO(周一0) -> cron(周一1)
    mday_m = _field_matches(dt.day, mday, 1, 31)
    if dow == "*" and mday == "*":
        return True
    if dow == "*":
        return mday_m
    if mday == "*":
        return dow_m
    return dow_m or mday_m

# ------------------------------------------------------------------
# 调度执行
# ------------------------------------------------------------------

async def run_scheduler_loop(interval: float = 30.0):
    """后台调度循环：每 interval 秒扫描一次所有用户的启用任务。

    触发逻辑：基于当前分钟匹配 cron。为避免同一分钟内重复触发同一任务，
    记录每个任务的上一次触发分钟（in-memory，重启后基于 last_run_at 兜底）。
    """
    logger = None
    try:
        from logger import get_logger
        logger = get_logger(__name__)
    except Exception:
        pass
    last_triggered = {}  # task_id -> "YYYY-MM-DD HH:MM"

    # 任务执行锁（每用户一把），防止 cron 分钟跨度跨越时并发重入
    locks = {}

    while True:
        try:
            now = datetime.now()
            key_min = now.strftime("%Y-%m-%d %H:%M")
            users = _list_users_with_bots()
            for uid in users:
                try:
                    tasks = session_store.list_enabled_cron_tasks(uid)
                except Exception:
                    continue
                for task in tasks:
                    try:
                        if not cron_matches(task["cron"], now):
                            continue
                        if last_triggered.get(task["id"]) == key_min:
                            continue
                        last_triggered[task["id"]] = key_min
                        lock = locks.setdefault(uid, asyncio.Lock())
                        asyncio.create_task(_exec_task(uid, task["id"], lock, logger))
                    except Exception as e:
                        if logger:
                            logger.warning("[cron] 任务 %s 匹配异常: %s", task.get("id"), e)
            # 兜底：任务被删除或禁用后清理内存占用
            for tid in [t for t in last_triggered if _no_longer_active(t)]:
                last_triggered.pop(tid, None)
        except Exception as e:
            if logger:
                logger.warning("[cron] 调度循环异常: %s", e)
        await asyncio.sleep(interval)


def _no_longer_active(task_id: str) -> bool:
    """供调度循环判断任务是否已删除/停用（跨用户粗查）。"""
    try:
        for uid in _list_users_with_bots():
            t = session_store.get_cron_task(uid, task_id)
            if t and not t["enabled"]:
                return True
            if t:
                return False
        return True
    except Exception:
        return False


def _list_users_with_bots() -> list:
    """返回需要为其调度定时任务的用户列表。

    定时任务按「登录用户」运行；这里回落到用户管理器 + default，保证至少 default 可跑。
    """
    users = ["default"]
    try:
        from user_manager import list_users
        for u in list_users():
            uid = u.get("id", "")
            if uid and uid not in users:
                users.append(uid)
    except Exception:
        pass
    return users


async def _exec_task(uid: str, task_id: str, lock: asyncio.Lock, logger, require_enabled: bool = True):
    """执行单个定时任务：在项目下驱动 agent 跑一次 content 并落盘结果。

    require_enabled=False 时跳过启用检查（供「立即执行」按钮使用）。
    """
    async with lock:
        task = session_store.get_cron_task(uid, task_id)
        if not task:
            return
        if require_enabled and not task["enabled"]:
            return
        if logger:
            logger.info("[cron] 触发任务 %s(%s) uid=%s", task["name"], task_id, uid)
        try:
            result = await _run_agent_once(uid, task)
            status = "ok"
        except Exception as e:
            result = f"定时任务执行失败：{e}"
            status = "error"
            if logger:
                logger.warning("[cron] 任务 %s 执行异常: %s", task_id, e)
        now = datetime.now().isoformat()
        session_store.update_cron_task(
            uid, task_id, last_run_at=now, last_status=status,
        )


async def run_task_now(uid: str, task_id: str) -> bool:
    """手动立即执行一次定时任务（API 用）。异常时返回 False。"""
    try:
        logger = None
        try:
            from logger import get_logger
            logger = get_logger(__name__)
        except Exception:
            pass
        await _exec_task(uid, task_id, asyncio.Lock(), logger, require_enabled=False)
        return True
    except Exception:
        return False


async def _run_agent_once(uid: str, task: dict) -> str:
    """在新协程中用 agent 执行 task['content']。

    复用非流式 agent.run（async），走与 /run 相同的落盘路径。
    短任务直接在事件循环内 await（调度循环串行同用户任务，天然互斥）。
    """
    try:
        from app_state import get_agent
    except Exception:
        return "(调度器未就绪)"
    agent = get_agent()
    if agent is None:
        # 冷启动兜底：agent 尚未初始化时尝试拉起（best effort）
        try:
            from api.routes.agent import init_agent
            init_agent(caller="cron.scheduler")
            agent = get_agent()
        except Exception:
            pass
    if agent is None:
        raise RuntimeError("Agent 未初始化")
    content = task.get("content", "").strip()
    if not content:
        return "(空内容)"
    session_id = _session_for_task(uid, task)
    s = session_store.get_session(uid, session_id)
    history = (s.get("messages", []) or []) if s else []
    # 复用 /run 同款的工作区切换逻辑（项目目录优先 + 同步 agent 系统提示）
    try:
        from services.agent_service import _apply_session_workspace
        _apply_session_workspace(uid, session_id, task["project_id"])
    except Exception:
        pass
    result, steps = await agent.run(content, history=history, thread_id=session_id)
    # 直接落盘用户指令 + 助手结果（含 steps），供会话历史回放；不再重复 add_message
    try:
        from services.agent_service import _save_assistant_result
        session_store.add_message(uid, session_id, "user", content)
        _save_assistant_result(uid, session_id, content, result, steps=steps or [])
    except Exception:
        session_store.add_message(uid, session_id, "assistant", result or "")
    return result or ""


def _session_for_task(uid: str, task: dict) -> str:
    """为每个定时任务维护一个固定会话（稳定 id），结果持续累积可回放。"""
    session_id = "cron_sess_" + task["id"]
    s = session_store.get_session(uid, session_id)
    if s is None:
        try:
            session_store.create_session(
                uid, title=f"定时任务·{task.get('name', '')}",
                session_id=session_id,
                project_id=task["project_id"],
            )
        except Exception:
            pass
    return session_id
