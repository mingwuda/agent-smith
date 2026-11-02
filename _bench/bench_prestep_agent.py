"""MOSS 桌面智能体 · 轻量评测任务集与驱动

用于「当前 agent 基线评测」（范围 C 对应能力档），每个任务记录：
  耗时 / 结果判别 / token 消耗 / 估算成本
输出 runs/bench/*/ 下的结构化结果 + Markdown 报告。

评测维度（范围 C 桌面 agent 核心能力档）：
  C1 代码执行   C2 文件读写   C3 数据分析/计算
  C4 信息检索   C5 网络访问   C6 多步规划
  C7 工具组合   C8 输出精确性

用法：python3 bench_prestep_agent.py  [-n 全部|N]  [-m 模型名]
环境：
  MOSS_BASE   http://127.0.0.1:8080   (默认)
  MOSS_USER   test                    (默认)
  MOSS_PASS   test123                 (默认)
模型单价（估算，USD/Mtok，写入报告时标注为估算值）：
  INPUT_PRICE / OUTPUT_PRICE 可通过环境变量覆盖。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

BASE = os.environ.get("MOSS_BASE", "http://127.0.0.1:8080")
USER = os.environ.get("MOSS_USER", "test")
PASSW = os.environ.get("MOSS_PASS", "test123")
COOKIE_OVERRIDE = os.environ.get("MOSS_COOKIE", "")  # 可选：直接注入登录 cookie
# 估算单价 USD / 1M tokens（参考费率，可覆盖）
IN_PRICE = float(os.environ.get("INPUT_PRICE", "0.15"))   # 输入 $/M
OUT_PRICE = float(os.environ.get("OUTPUT_PRICE", "0.60"))  # 输出 $/M
USAGE_DB = Path(os.environ.get("USAGE_DB", "/root/.desktop_agent/users/test/usage/usage.sqlite3"))

OUTDIR = Path(__file__).resolve().parent / "runs" / "bench"


_SESSION = {"cookie": COOKIE_OVERRIDE}


def _http(url, data=None, method=None, timeout=180, cookie=None):
    req = urllib.request.Request(url, method=method or ("POST" if data else "GET"))
    ck = cookie or _SESSION["cookie"]
    if ck:
        req.add_header("Cookie", ck)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if "set-cookie" in resp.headers and not _SESSION["cookie"]:
            setc = resp.headers.get("set-cookie")
            _SESSION["cookie"] = setc.split(";")[0]
        if body:
            return json.loads(body.decode())
        return {}


# 评测任务集（范围 C 桌面 agent 能力档）
TASKS = [
    {"id": "c1_py_exec", "cap": "C1 代码执行", "cost_dim": "中", "timeout": 120,
     "prompt": "用 Python 计算 1 到 100 的所有偶数之和，只输出最终数字。",
     "check": lambda r: str(r).strip() == "2550"},
    {"id": "c2_file_io", "cap": "C2 文件读写", "cost_dim": "中", "timeout": 180,
     "prompt": "在当前工作目录创建一个名为 bench_hello.txt 的文件，内容写一句话'hello from moss bench'，然后读取并返回该文件第一行的内容。",
     "check": lambda r: "hello from moss bench" in str(r)},
    {"id": "c3_analysis", "cap": "C3 数据分析/计算", "cost_dim": "高", "timeout": 180,
     "prompt": "已知某商品含税售价为 128 元（含 13% 增值税），请计算不含税金额，保留两位小数，只输出数字。",
     "check": lambda r: abs(float(str(r).strip().rstrip('元')) - round(128 / 1.13, 2)) < 0.01},
    {"id": "c4_retrieval", "cap": "C4 信息检索", "cost_dim": "中", "timeout": 180,
     "prompt": "请读取最近的一次 git 提交时间。在 /opt/desktop-agent 目录下执行 `git log -1 --format=%cs` 并返回结果。",
     "check": lambda r: len(str(r).strip()) >= 8},
    {"id": "c5_network", "cap": "C5 网络访问", "cost_dim": "高", "timeout": 180,
     "prompt": "请访问 http://127.0.0.1:8080/health 并返回其中的 status 字段值。",
     "check": lambda r: "ok" in str(r).lower()},
    {"id": "c6_planning", "cap": "C6 多步规划", "cost_dim": "高", "timeout": 240,
     "prompt": "完成以下多步任务并给出最终汇总：(1)建目录 /tmp/moss_bench_planner；(2)在其中创建文件 a.txt 内容为 5；(3)创建 b.txt 内容为 3；(4)用 python 读取两文件数字求和并写入 sum.txt；(5)返回 sum.txt 的内容。",
     "check": lambda r: "8" in str(r)},
    {"id": "c7_combo", "cap": "C7 工具组合", "cost_dim": "高", "timeout": 240,
     "prompt": "用 python 生成一个 1..10 的平方列表，把结果写入 /tmp/moss_bench_squares.json，然后用 python 读回并计算这些数的平均值，返回平均值（保留一位小数）。",
     "check": lambda r: abs(float(str(r).strip().rstrip('%')) - 38.5) < 0.2},
    {"id": "c8_precision", "cap": "C8 输出精确性", "cost_dim": "低", "timeout": 90,
     "prompt": "只输出下列字符串内的内容，不要添加任何其他文字或代码块标记：『AUTO_GRADE_41d3』",
     "check": lambda r: str(r).strip() == "AUTO_GRADE_41d3"},
]


def usage_between(db, start_ts, end_ts):
    """统计时间段内该用户的 token 与 cost 汇总。"""
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,"
            " COALESCE(SUM(cached_input_tokens),0) c, COALESCE(SUM(cost),0) cost, COUNT(*) n"
            " FROM usage_records WHERE timestamp >= ? AND timestamp <= ?",
            (start_ts, end_ts)).fetchone()
        return {"input_tokens": row[0], "output_tokens": row[1],
                "cached_tokens": row[2], "recorded_cost": row[3], "calls": row[4]}
    finally:
        con.close()


def est_cost(inp, out):
    return (inp / 1e6 * IN_PRICE) + (out / 1e6 * OUT_PRICE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--num", type=int, default=0, help="运行任务数（0=全部）")
    ap.add_argument("-m", "--model", default="agnes-3.0-flash")
    args = ap.parse_args()

    tasks = TASKS if args.num <= 0 else TASKS[:args.num]
    run_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTDIR / f"{run_stamp}_{args.model}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 登录建立会话（若未通过环境变量注入 cookie）
    if not _SESSION["cookie"]:
        try:
            _http(f"{BASE}/auth/login", {"username": USER, "password": PASSW})
            print(f"[bench] 已登录 {USER}", flush=True)
        except Exception as e:
            print(f"[bench] 登录失败: {e}", flush=True)
    else:
        print(f"[bench] 使用注入 cookie", flush=True)

    print(f"[bench] 模型={args.model} 任务数={len(tasks)} base={BASE} user={USER}", flush=True)
    results = []
    for i, t in enumerate(tasks, 1):
        print(f"  [{i}/{len(tasks)}] {t['id']} ...", end="", flush=True)
        start = dt.datetime.now()
        start_ts = start.isoformat()
        result, status = "", "ok"
        try:
            resp = _http(f"{BASE}/run",
                         {"message": t["prompt"], "thread_id": "bench_c_" + t["id"],
                          "project_id": "", "attachments": []},
                         timeout=t.get("timeout", 180))
            result = resp.get("result", "")
            try:
                ok = t["check"](result)
            except Exception:
                ok = False
            status = "pass" if ok else "fail"
        except Exception as e:
            status = "error"
            result = f"{type(e).__name__}: {e}"
        end = dt.datetime.now()
        elapsed = (end - start).total_seconds()
        usage = usage_between(USAGE_DB, start_ts, end.isoformat())
        if not usage:
            usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                     "recorded_cost": 0, "calls": 0}
        rec = {
            "id": t["id"], "cap": t["cap"], "cost_dim": t["cost_dim"],
            "status": status, "result": result[:500], "elapsed_s": round(elapsed, 2),
            "usage": usage,
            "est_cost_usd": round(est_cost(usage["input_tokens"], usage["output_tokens"]), 6),
        }
        results.append(rec)
        print(f" {rec['status']} ({rec['elapsed_s']}s)", flush=True)

    total_in = sum(r["usage"]["input_tokens"] for r in results)
    total_out = sum(r["usage"]["output_tokens"] for r in results)
    total_cost = sum(r["est_cost_usd"] for r in results)
    total_elapsed = round(sum(r["elapsed_s"] for r in results), 2)
    n_pass = sum(1 for r in results if r["status"] == "pass")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_err = sum(1 for r in results if r["status"] == "error")

    summary = {
        "model": args.model, "user": USER, "base": BASE, "run_stamp": run_stamp,
        "n": len(results), "pass": n_pass, "fail": n_fail, "error": n_err,
        "total_elapsed_s": total_elapsed,
        "total_input_tokens": total_in, "total_output_tokens": total_out,
        "est_cost_usd": round(total_cost, 6),
        "price_note": "估算价 IN=$%s/M OUT=$%s/M（可覆盖）" % (IN_PRICE, OUT_PRICE),
    }
    (run_dir / "results.json").write_text(
        json.dumps({"summary": summary, "tasks": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    md = [f"# MOSS Agent 基线评测报告", "",
          f"- 模型：`{args.model}`（估算单价 输入 ${IN_PRICE}/M · 输出 ${OUT_PRICE}/M）",
          f"- 时间：{run_stamp} · 用户：`{USER}` · base：`{BASE}`",
          f"- 任务数：{len(results)} · ✅ 通过 {n_pass} · ❌ 失败 {n_fail} · ⚠️ 错误 {n_err}",
          "",
          "| 任务 | 能力档 | 成本档 | 结果 | 耗时(s) | 输入tok | 输出tok | 估算$ |",
          "|---|---|---|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r['id']} | {r['cap']} | {r['cost_dim']} | {r['status']} | {r['elapsed_s']} | "
                  f"{r['usage']['input_tokens']} | {r['usage']['output_tokens']} | {r['est_cost_usd']} |")
    md += ["", "## 汇总", "",
           f"- 总耗时：{total_elapsed}s · 总 token：in {total_in} / out {total_out} · 估算成本 ${round(total_cost, 6)}",
           f"- 通过率：{n_pass}/{len(results)} = {100 * n_pass / len(results) if results else 0:.1f}%",
           f"- {summary['price_note']}", "", "## 明细", ""]
    for r in results:
        md.append(f"### {r['id']}（{r['cap']} · 结果 `{r['status']}` · {r['elapsed_s']}s）")
        md.append("```")
        md.append(r["result"])
        md.append("```")
        md.append("")
    (run_dir / "report.md").write_text("\n".join(md), encoding="utf-8")

    print("\n== 汇总 ==")
    print(f"  通过 {n_pass} / 失败 {n_fail} / 错误 {n_err} / 共 {len(results)}")
    print(f"  总耗时 {total_elapsed}s · in {total_in} / out {total_out} tok · 估算成本 ${round(total_cost, 6)}")
    print(f"  报告: {run_dir / 'report.md'}")


if __name__ == "__main__":
    main()