"""cron 定时任务：自然语言解析 + 匹配 + 存储 CRUD 的最小自检（无框架）。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent_core"))

from cron_service import parse_cron_expr, cron_matches
from datetime import datetime

fails = []

def eq(got, want, msg):
    if got != want:
        fails.append(f"{msg}: got={got!r} want={want!r}")

# ── 自然语言 → cron ──
cases = [
    ("每天 8:30", "30 8 * * *"),
    ("每天早上9点", "0 9 * * *"),
    ("每小时", "0 * * * *"),
    ("每30分钟", "*/30 * * * *"),
    ("每两小时", "0 */2 * * *"),
    ("每周一 9:00", "0 9 * * 1"),
    ("每周一三五 8:00", "0 8 * * 1,3,5"),
    ("每周一到周五 9:00", "0 9 * * 1,2,3,4,5"),
    ("工作日 9:00", "0 9 * * 1,2,3,4,5"),
    ("周末 9:00", "0 9 * * 0,6"),
    ("每月1号 10:00", "0 10 1 * *"),
    ("每月1号和15号 10:00", "0 10 1,15 * *"),
    ("0 9 * * 1", "0 9 * * 1"),  # 直接 cron
]
for text, want in cases:
    try:
        eq(parse_cron_expr(text), want, f"parse({text!r})")
    except Exception as e:
        fails.append(f"parse({text!r}) raised {e}")

# ── cron 匹配 ──
eq(cron_matches("0 9 * * 1", datetime(2026, 9, 21, 9, 0)), True, "周一9点匹配")      # 2026-09-21 周一
eq(cron_matches("0 9 * * 1", datetime(2026, 9, 22, 9, 0)), False, "周二9点不匹配")
eq(cron_matches("30 8 * * *", datetime(2026, 9, 20, 8, 30)), True, "每天8:30匹配")
eq(cron_matches("30 8 * * *", datetime(2026, 9, 20, 9, 30)), False, "9:30不匹配")
eq(cron_matches("*/30 * * * *", datetime(2026, 9, 20, 10, 0)), True, "每30分钟:00匹配")
eq(cron_matches("*/30 * * * *", datetime(2026, 9, 20, 10, 15)), False, "每30分钟:15不匹配")
eq(cron_matches("0 9 * * 0,6", datetime(2026, 9, 20, 9, 0)), True, "周末9点(周日)匹配")

# ── 存储 CRUD ──
import session_store, tempfile
# 用 isolated 用户，避免污染真实数据
try:
    TMP = session_store.DATA_DIR
    uid = "_cron_selftest"
    # 清除遗留
    for t in session_store.list_cron_tasks(uid):
        session_store.delete_cron_task(uid, t["id"])
    proj = session_store.create_project(uid, "selftest")
    pid = proj["id"]
    t = session_store.create_cron_task(uid, pid, "测试任务", "0 9 * * 1", "做点什么")
    eq(t["cron"], "0 9 * * 1", "创建 cron")
    eq(t["enabled"], True, "默认启用")
    got = session_store.get_cron_task(uid, t["id"])
    eq(got["name"], "测试任务", "读取")
    ok = session_store.update_cron_task(uid, t["id"], enabled=False, last_status="ok")
    eq(ok, True, "更新")
    got2 = session_store.get_cron_task(uid, t["id"])
    eq(got2["enabled"], False, "停用生效")
    lst = session_store.list_cron_tasks(uid, pid)
    eq(len(lst), 1, "按项目列出")
    eq(len(session_store.list_enabled_cron_tasks(uid)), 0, "启用任务为空")
    session_store.update_cron_task(uid, t["id"], enabled=True)
    eq(len(session_store.list_enabled_cron_tasks(uid)), 1, "启用任务1")
    eq(session_store.delete_cron_task(uid, t["id"]), True, "删除")
    eq(session_store.get_cron_task(uid, t["id"]), None, "删除后读取为空")
    session_store.delete_project(uid, pid)
except Exception as e:
    fails.append("存储CRUD异常: %s" % e)

if fails:
    print("FAIL:", len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")