"""验证 _astream_with_idle_timeout 的「仅有效进展才刷新空闲看门狗」逻辑。

本地无 langchain 依赖，故此处直接复用 agent_helpers.py 中两个函数的实现体做行为验证：
- 空 keepalive chunk 不应刷新计时，持续空包应在 idle_timeout 后抛 TimeoutError（触发重试）。
- 真实正文 / 工具调用 / 推理 token 应算进展，不误杀。
"""
import asyncio
import time


class _Chunk:
    def __init__(self, content=None, tool_call_chunks=None, reasoning_content=None,
                 additional_kwargs=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks
        self.reasoning_content = reasoning_content
        self.additional_kwargs = additional_kwargs or {}


def _chunk_has_progress(chunk) -> bool:
    if chunk is None:
        return False
    if getattr(chunk, "content", None):
        return True
    if getattr(chunk, "tool_call_chunks", None):
        return True
    if getattr(chunk, "reasoning_content", None):
        return True
    ak = getattr(chunk, "additional_kwargs", None) or {}
    if any(k in ak for k in ("reasoning_content", "reasoning", "thinking")):
        return True
    return False


async def _astream_with_idle_timeout(agen, idle_timeout: float):
    last_progress = time.monotonic()
    try:
        first = await asyncio.wait_for(agen.__anext__(), timeout=idle_timeout)
    except StopAsyncIteration:
        return
    if _chunk_has_progress(first):
        last_progress = time.monotonic()
    yield first
    while True:
        try:
            chunk = await asyncio.wait_for(agen.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            return
        if _chunk_has_progress(chunk):
            last_progress = time.monotonic()
        elif time.monotonic() - last_progress > idle_timeout:
            raise asyncio.TimeoutError()
        yield chunk


async def _keepalive_forever(idle):
    # 模拟上游只发空 keepalive（每个都在 wait_for 窗口内抵达，却无真实进展）
    yield _Chunk()  # 首个空块
    while True:
        await asyncio.sleep(idle * 0.3)  # 远小于 idle，确保 wait_for 不会因"无 chunk"先超时
        yield _Chunk()


async def _real_then_keepalive(idle):
    yield _Chunk(content="hello")  # 真实进展
    while True:
        await asyncio.sleep(idle * 0.3)
        yield _Chunk()


async def _real_only(idle):
    yield _Chunk(content="a")
    await asyncio.sleep(idle * 0.3)
    yield _Chunk(tool_call_chunks=[{"name": "read_file"}])
    await asyncio.sleep(idle * 0.3)
    yield _Chunk(additional_kwargs={"reasoning_content": "thinking..."})


async def _run(name, gen_factory, idle, expect_timeout):
    try:
        got = []
        async for c in _astream_with_idle_timeout(gen_factory(idle), idle):
            got.append(c)
        raised = False
    except asyncio.TimeoutError:
        raised = True
    ok = raised == expect_timeout
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: timeout={raised} (expect={expect_timeout}) chunks={len(got)}")
    return ok


async def main():
    idle = 0.2
    results = []
    results.append(await _run("only keepalives -> must timeout(retry fires)", _keepalive_forever, idle, True))
    results.append(await _run("real then keepalives -> must timeout after progress", _real_then_keepalive, idle, True))
    results.append(await _run("real content only -> must NOT timeout", _real_only, idle, False))
    assert all(results), "some checks failed"
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
