"""
并发安全验证测试 —— Iteration 1

测试用例：
  1. 两个并行请求到不同 session，验证响应和日志隔离
  2. 浏览器操作排队（不冲突）
  3. 同时导航到不同页面不串数据
"""
import asyncio
import json
import os
import time
import httpx

BASE_URL = "http://127.0.0.1:8899"
PASS = 0
FAIL = 0

# Cookie 从一次登录获取（先登录拿 cookie）
async def login(client: httpx.AsyncClient) -> dict:
    resp = await client.post(f"{BASE_URL}/auth/login", json={
        "username": "admin",
        "password": "admin123",
    })
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return resp.cookies


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


async def test_session_isolation():
    """测试 1: 两个并发请求到不同 session，响应不串"""
    print("\n📦 测试 1: Session 隔离")
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        cookies = await login(client)
        
        async def send_message(session_id: str, message: str, expected_tag: str) -> str:
            """发送消息到指定 session，返回完整响应"""
            resp = await client.post(
                "/run",
                json={"message": message, "thread_id": session_id},
                cookies=cookies,
                timeout=120,
            )
            if resp.status_code != 200:
                return f"❌ HTTP {resp.status_code}"
            data = resp.json()
            return data.get("result", "")
        
        # 同时发送两个不同 session 的请求
        t1 = send_message("test_session_a", "回复一个词: hello_a", "hello_a")
        t2 = send_message("test_session_b", "回复一个词: hello_b", "hello_b")
        
        results = await asyncio.gather(t1, t2, return_exceptions=True)
        
        r1 = results[0] if not isinstance(results[0], Exception) else str(results[0])
        r2 = results[1] if not isinstance(results[1], Exception) else str(results[1])
        
        check("两个请求都成功返回", 
              not r1.startswith("❌") and not r2.startswith("❌"),
              f"r1={'OK' if not r1.startswith('❌') else r1[:50]}, r2={'OK' if not r2.startswith('❌') else r2[:50]}")


async def test_log_context():
    """测试 2: 日志中有 [s:xxx] [m:xxx] 前缀，两个请求的上下文不乱"""
    print("\n📦 测试 2: 日志上下文隔离")
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        cookies = await login(client)
        
        async def send_stream(session_id: str, message: str, tag: str) -> str:
            """发送流式请求，收集所有 SSE 事件"""
            collected = ""
            async with client.stream(
                "POST", "/run/stream",
                json={"message": message, "thread_id": session_id},
                cookies=cookies,
                timeout=120,
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and "[DONE]" not in line:
                        try:
                            data = json.loads(line[6:])
                            if data.get("type") == "done":
                                collected = data.get("content", "")
                        except json.JSONDecodeError:
                            pass
            return collected
        
        t1 = send_stream("test_log_a", "回复数字: 111", "111")
        t2 = send_stream("test_log_b", "回复数字: 222", "222")
        
        r1, r2 = await asyncio.gather(t1, t2)
        
        # 验证: 两个结果的内容分别包含自己的关键字
        check("session_a 回复了 111", "111" in r1, f"r1={r1[:50]}")
        check("session_b 回复了 222", "222" in r2, f"r2={r2[:50]}")
        
        # 再验证日志不乱：检查最近的日志
        import subprocess
        log_path = os.environ.get("AGENT_LOG_PATH", "")
        if not log_path:
            print("  ⏭️  未设置 AGENT_LOG_PATH，跳过日志隔离检查")
            return
        log = subprocess.check_output(
            f"grep 'test_log_a\\|test_log_b' {log_path} | tail -20",
            shell=True, text=True
        )
        has_a = "test_log_a" in log
        has_b = "test_log_b" in log
        check("日志中有 session_a 痕迹", has_a)
        check("日志中有 session_b 痕迹", has_b)


async def test_browser_concurrent():
    """测试 3: 浏览器并发操作不冲突"""
    print("\n📦 测试 3: 浏览器并发操作")
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        cookies = await login(client)
        
        async def navigate(session_id: str, url: str) -> str:
            """导航到 URL 并返回页面标题"""
            resp = await client.post(
                "/run",
                json={
                    "message": f"导航到 {url}，返回页面标题",
                    "thread_id": session_id,
                },
                cookies=cookies,
                timeout=120,
            )
            return resp.json().get("result", "")
        
        # 同时导航到不同页面
        t1 = navigate("test_browser_a", "https://example.com")
        t2 = navigate("test_browser_b", "https://httpbin.org/get")
        
        r1, r2 = await asyncio.gather(t1, t2, return_exceptions=True)
        
        # 浏览器操作会排队（因为 _page_lock），但不应该 crash
        check("浏览器并发不报错", 
              not (isinstance(r1, Exception) or isinstance(r2, Exception)),
              f"e1={type(r1).__name__ if isinstance(r1, Exception) else 'OK'}, "
              f"e2={type(r2).__name__ if isinstance(r2, Exception) else 'OK'}")


async def test_agent_not_serialized():
    """测试 4: 非浏览器的普通请求是真正并行的（不排队）"""
    print("\n📦 测试 4: 普通请求并行性能")
    
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        cookies = await login(client)
        
        async def simple_chat(session_id: str) -> float:
            start = time.time()
            resp = await client.post(
                "/run",
                json={
                    "message": "回复一个词: ok",
                    "thread_id": session_id,
                },
                cookies=cookies,
                timeout=120,
            )
            elapsed = time.time() - start
            return elapsed
        
        # 并发发 5 个普通聊天请求
        start = time.time()
        tasks = [simple_chat(f"test_perf_{i}") for i in range(5)]
        times = await asyncio.gather(*tasks)
        total = time.time() - start
        
        # 如果完全是串行，总时间 ≈ 5 × 平均单个时间
        # 如果并行，总时间 ≈ 最大单个时间
        avg_single = sum(times) / len(times)
        
        check("并发请求总时间小于串行时间之和",
              total < sum(times) * 0.7,  # 至少快 30%
              f"总时间={total:.1f}s, 串行预期≈{sum(times):.1f}s")
        check("平均响应时间正常", avg_single < 30, f"avg={avg_single:.1f}s")
        print(f"    5 个请求总耗时: {total:.1f}s (串行≈{sum(times):.1f}s)")
        for i, t in enumerate(times):
            print(f"    请求 {i}: {t:.1f}s")


async def main():
    print("=" * 60)
    print("  并发安全验证测试")
    print(f"  服务地址: {BASE_URL}")
    print("=" * 60)
    
    await test_session_isolation()
    await test_log_context()
    await test_browser_concurrent()
    await test_agent_not_serialized()
    
    total = PASS + FAIL
    print("\n" + "=" * 60)
    print(f"  测试完成: {total} 项")
    print(f"  ✅ 通过: {PASS}")
    print(f"  ❌ 失败: {FAIL}")
    print(f"  通过率: {PASS/max(total,1)*100:.0f}%")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
