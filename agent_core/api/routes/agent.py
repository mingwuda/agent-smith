"""Agent 运行路由"""
import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.deps import _get_current_user
from services.workspace import _safe_attachments, _display_user_message, _user_image_urls, _append_artifact_links
from services.agent_service import (
    _ensure_session,
    _is_skill_inventory_query,
    _image_model_override,
    _format_loaded_skills,
    _save_assistant_result,
    _resolve_user,
    _apply_session_workspace,
    _async_reflect,
    _reflect_from_feedback,
    _strip_screenshot_urls,
)
from logger import set_log_context, get_logger
from config import AgentConfig
from agent import DesktopAgent
import session_store
import stream_log
from memory.local_memory import get_memory

logger = get_logger(__name__)

router = APIRouter(tags=["agent"])

# 当前活跃的 Python 工具进度 WebSocket 连接
_active_tool_progress_ws: set[WebSocket] = set()

# ---------- API 模型 ----------

class AttachmentRequest(BaseModel):
    name: str = "pasted-image.png"
    mime_type: str = "image/png"
    data_url: str


class RunRequest(BaseModel):
    message: str
    thread_id: str = "default"
    attachments: list[AttachmentRequest] = Field(default_factory=list)
    project_id: str = ""
    provider: str = ""  # 可选：指定本次请求使用的 provider id（覆盖全局 active_provider，仅本次生效）


class RunResponse(BaseModel):
    result: str
    steps: list[dict] = []
    todo_list: Optional[dict] = None


# ---------- 全局 Agent 实例 ----------

agent: Optional[DesktopAgent] = None


def init_agent(caller: str = "route.agent", force: bool = False):
    """初始化 Agent（委托 main 模块，同步本地引用）"""
    global agent
    # 防御：本地引用已就绪时不重复初始化。否则每次调用都会触发 main.init_agent()
    # 里「停止旧微信 Bot → 重启」的逻辑，打断正在轮询的 bot（2026-08-03 事故：运行时
    # 重复 init_agent 停掉了 bot，补发任务被取消，最终回复丢失）。
    # force=True 为显式重启路径（保存设置/删 Provider），总是重建。
    if agent is not None and not force:
        return
    from app_state import get_init_agent, get_agent
    _main_init = get_init_agent()
    if _main_init is not None:
        _main_init(caller=caller, force=force)
    agent = get_agent()



# ---------- SSE 后台驱动（刷新可恢复） ----------
# agent 流不挂在 HTTP 连接上：连接断开/刷新只退订，driver 继续后台跑完并实时落盘
# stream_log（事件 jsonl + running 登记），终态统一「落历史 + clear_running + 反思」。
# 客户端通过 hub 订阅实时事件；刷新后通过 /sessions/{id}/stream/active 拿落盘事件回放。

_END_SENTINEL = object()   # driver 结束标志，广播给所有订阅者
_live_hubs: dict = {}      # message_id -> _StreamHub（活跃 run 的事件广播器）

class _StreamHub:
    """单个 run 的事件广播器：driver 产出 SSE 文本 -> 写 buffer + 推给所有订阅队列。

    订阅者（每个 SSE 连接一个队列）先回放 buffer 再实时续接，保证晚到的新连接不丢事件。
    buffer 保留全量事件（单 run 事件数有限），run 结束且无订阅者后由 _cleanup 回收。
    """
    def __init__(self):
        self.buffer: list = []
        self.subs: set = set()
        self.finished = False
        self.created_at = time.time()

    def publish(self, sse_text: str) -> None:
        self.buffer.append(sse_text)
        for q in list(self.subs):
            try:
                q.put_nowait(sse_text)
            except Exception:
                pass

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        for q in list(self.subs):
            try:
                q.put_nowait(_END_SENTINEL)
            except Exception:
                pass

    def subscribe(self):
        q = asyncio.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self.subs.discard(q)


def _cleanup_finished_hubs() -> None:
    """回收已结束且无订阅者的 hub（宽限 300s 让刚结束的 run 仍可被 active 接口短暂回放）。"""
    now = time.time()
    for mid in list(_live_hubs.keys()):
        hub = _live_hubs[mid]
        if hub.finished and not hub.subs and (now - hub.created_at) > 300:
            _live_hubs.pop(mid, None)


# ---------- 路由 ----------


@router.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest, request: Request):
    """发送消息给 Agent 并获取回复"""
    if not agent:
        init_agent(caller="route.agent.run")
    if not agent:
        logger.error("Agent 初始化失败，请检查 API Key 设置")
        raise HTTPException(503, "Agent 初始化失败，请检查 API Key 设置")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    message_id = str(uuid.uuid4())
    set_log_context(session_id=session_id, message_id=message_id)
    _apply_session_workspace(uid, session_id, req.project_id)
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    display_text = _display_user_message(uid, req.message, attachments)
    session_store.add_message(uid, session_id, "user", display_text)
    model_override = _image_model_override(attachments)
    # 解析可选的 provider 覆盖：仅当该 provider 在配置中存在时才生效（否则回退到全局 active_provider）
    provider_override = req.provider if (req.provider and agent and req.provider in (getattr(agent.config, "providers", {}) or {})) else ""
    # ── 解析文本文件内容，直接嵌入 agent 消息 ──
    agent_message = req.message
    if attachments:
        try:
            parsed = json.loads(display_text)
            if isinstance(parsed, dict) and parsed.get("text_files"):
                text_content = parsed.get("text", "")
                if text_content and text_content != req.message:
                    agent_message = text_content
        except (json.JSONDecodeError, TypeError):
            pass
    # ── 解析 ZIP 清单，追加到 LLM 消息中 ──
    if attachments and any(a.get("mime_type") == "application/zip" for a in attachments):
        try:
            parsed = json.loads(display_text)
            manifest = parsed.get("zip_manifest", "")
            if manifest:
                agent_message = req.message + "\n\n" + manifest
        except (json.JSONDecodeError, TypeError):
            pass
    # ── 解析图片下载地址，追加到 LLM 消息中供图生图模型使用 ──
    if attachments and any(a.get("mime_type", "").startswith("image/") for a in attachments):
        try:
            parsed = json.loads(display_text)
            img_paths = parsed.get("images", [])
            if img_paths:
                img_urls = _user_image_urls(uid, img_paths, request)
                url_lines = "\n".join(f"- {url}" for url in img_urls)
                agent_message += f"\n\n[上传的图片已在服务器保存，以下为图片下载地址可供图生图模型使用：]\n{url_lines}"
        except (json.JSONDecodeError, TypeError):
            pass
    if _is_skill_inventory_query(req.message):
        result = _format_loaded_skills()
        _save_assistant_result(uid, session_id, req.message, result)
        return RunResponse(result=result, steps=[])

    result, steps = await agent.run(
        agent_message,
        history=history_messages,
        attachments=attachments,
        model_override=model_override,
        thread_id=session_id,
        provider_override=provider_override,
    )
    # 从 todo store 取出清单（非流式模式）。
    # 用 peek（只查内存不读盘）+ 完整 key（"uid:session_id"）：工具按 LangGraph config
    # 的完整 thread_id 写入，本轮 ainvoke 刚执行完 manage_todo 必在缓存中。无参/读盘会
    # 取到其它会话或上一轮残留的清单。取出后立即清缓存防跨请求残留；磁盘文件保留供"继续"恢复。
    todo_list_r = None
    try:
        from tools.todo_tools import peek_todo_list, pop_todo_list
        _todo_key = f"{uid}:{session_id}"
        todo_list_r = peek_todo_list(_todo_key)
        pop_todo_list(_todo_key)
    except Exception:
        pass
    artifact_paths = [
        str(step.get("args", {}).get("path", ""))
        for step in steps
        if step.get("type") == "tool_call"
        and step.get("tool") in {"write_file", "append_to_file", "edit_file"}
        and isinstance(step.get("args"), dict)
        and step.get("args", {}).get("path")
    ]
    result = _append_artifact_links(result, uid, artifact_paths)
    _save_assistant_result(uid, session_id, req.message, result, todo_list=todo_list_r)
    
    # 后台反思
    asyncio.create_task(_async_reflect(uid, req.message, steps, result))
    
    return RunResponse(result=result, steps=steps, todo_list=todo_list_r)


@router.post("/run/stream")
async def run_agent_stream(req: RunRequest, request: Request):
    """流式处理消息（SSE）"""
    if not agent:
        init_agent(caller="route.agent.stream")
    if not agent:
        raise HTTPException(503, "Agent 初始化失败")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    message_id = str(uuid.uuid4())
    set_log_context(session_id=session_id, message_id=message_id)
    _apply_session_workspace(uid, session_id, req.project_id)
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    display_text = _display_user_message(uid, req.message, attachments)
    session_store.add_message(uid, session_id, "user", display_text)
    model_override = _image_model_override(attachments)
    provider_override = req.provider if (req.provider and agent and req.provider in (getattr(agent.config, "providers", {}) or {})) else ""
    agent_message = req.message
    if attachments:
        try:
            parsed = json.loads(display_text)
            if isinstance(parsed, dict) and parsed.get("text_files"):
                text_content = parsed.get("text", "")
                if text_content and text_content != req.message:
                    agent_message = text_content
        except (json.JSONDecodeError, TypeError):
            pass
    if attachments and any(a.get("mime_type") == "application/zip" for a in attachments):
        try:
            parsed = json.loads(display_text)
            manifest = parsed.get("zip_manifest", "")
            if manifest:
                agent_message = req.message + "\n\n" + manifest
        except (json.JSONDecodeError, TypeError):
            pass
    if attachments and any(a.get("mime_type", "").startswith("image/") for a in attachments):
        try:
            parsed = json.loads(display_text)
            img_paths = parsed.get("images", [])
            if img_paths:
                img_urls = _user_image_urls(uid, img_paths, request)
                url_lines = "\n".join(f"- {url}" for url in img_urls)
                agent_message += f"\n\n[上传的图片已在服务器保存，以下为图片下载地址可供图生图模型使用：]\n{url_lines}"
        except (json.JSONDecodeError, TypeError):
            pass
    if _is_skill_inventory_query(req.message):
        result = _format_loaded_skills()
        _save_assistant_result(uid, session_id, req.message, result)

        async def skill_inventory_stream():
            yield f"data: {json.dumps({'type': 'done', 'content': result}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            skill_inventory_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    artifact_paths: list[str] = []
    collected_steps: list[dict] = []
    collected_todo_list = None

    # ── 刷新可恢复：agent 流由后台 driver 持有，HTTP 连接只是订阅者 ──
    hub = _StreamHub()
    _live_hubs[message_id] = hub
    asyncio.create_task(_drive_agent_stream(
        uid=uid, session_id=session_id, message_id=message_id,
        agent_message=agent_message, history=history_messages,
        attachments=attachments, model_override=model_override,
        provider_override=provider_override,
        req=req, artifact_paths=artifact_paths,
        collected_steps=collected_steps,
        collected_todo_list=collected_todo_list,
        hub=hub,
    ))

    async def event_stream():
        q = hub.subscribe()
        try:
            start = len(hub.buffer)
            for s in hub.buffer[:start]:
                yield s
            while True:
                item = await q.get()
                if item == _END_SENTINEL:
                    break
                yield item
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            pass
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/stream/active")
async def session_stream_active(session_id: str, request: Request):
    """刷新浏览器后恢复：返回该会话最近一个 run 的落盘事件 + 状态。"""
    uid = _resolve_user(request)
    run = stream_log.last_active_run(uid, session_id)
    if run is None:
        return {"active": False}
    return {
        "active": True,
        "message_id": run["message_id"],
        "running": run["running"],
        "finished": run["finished"],
        "events": _trim_events_for_replay(run["events"]),
    }


def _trim_events_for_replay(events: list) -> list:
    """回放端只保留前端渲染所需的事件，裁掉高频 token 碎片。"""
    out = []
    pending_tokens = []
    def _flush_tokens():
        if pending_tokens:
            out.append({"type": "token_delta", "content": "".join(pending_tokens)})
            pending_tokens.clear()
    for ev in events:
        t = ev.get("type")
        if t == "token":
            pending_tokens.append(ev.get("content", ""))
        else:
            _flush_tokens()
            out.append(ev)
    _flush_tokens()
    return out

class FeedbackRequest(BaseModel):
    rating: int = 0  # -1 | 0 | 1
    correction: Optional[str] = None


@router.post("/sessions/{session_id}/feedback")
async def submit_session_feedback(session_id: str, req: FeedbackRequest, request: Request):
    """收集用户对某轮结果的反馈（👍/👎 + 纠错），存入长期记忆并触发反思。"""
    uid = _resolve_user(request)
    session = session_store.get_session(uid, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在或无权访问")
    rating = max(-1, min(1, int(req.rating)))
    correction = (req.correction or "").strip()
    mem = get_memory(uid)
    mem.set(f"_feedback_{session_id}_{int(time.time())}", {"rating": rating, "correction": correction})
    # 仅在自进化开关开启时，根据纠错触发偏好/踩坑反思（_reflect_from_feedback 内部再判一次开关）
    if correction:
        asyncio.create_task(_reflect_from_feedback(uid, session_id, rating, correction))
    return {"ok": True}


async def _drive_agent_stream(
    uid: str,
    session_id: str,
    message_id: str,
    agent_message: str,
    history: list,
    attachments: list,
    model_override: str,
    provider_override: str,
    req: RunRequest,
    artifact_paths: list,
    collected_steps: list,
    collected_todo_list: Optional[dict],
    hub: "_StreamHub",
) -> None:
    """后台驱动：消费 agent 流并实时落盘 + 广播给所有 SSE 订阅者。

    与 HTTP 连接解耦：客户端断开/刷新只影响订阅侧，driver 继续后台跑完。
    终态由本函数统一处理（落历史 + clear_running + 反思），保证只发生一次。
    """
    stream_log.mark_running(uid, session_id, message_id)
    final_content = ""
    error_content = ""
    forwarded_terminal_event = False

    def _persist(parsed: Optional[dict]) -> None:
        if parsed:
            stream_log.append_event(uid, session_id, message_id, parsed)

    try:
        stream = agent._stream_done_wrapper(
            agent_message,
            history=history,
            attachments=attachments,
            model_override=model_override,
            thread_id=session_id,
            provider_override=provider_override,
        )
        if model_override:
            hub.publish(f"data: {json.dumps({'type': 'model_switch', 'model': model_override, 'reason': '图片输入'}, ensure_ascii=False)}\n\n")
        async for sse_event in stream:
            if sse_event.strip() == "data: [DONE]":
                continue
            # 解析并持久化
            data = None
            try:
                m = re.search(r"data: ({.*})", sse_event)
                if m:
                    data = json.loads(m.group(1))
            except Exception:
                pass
            _persist(data)
            # 收集 steps/todo（复用原 event_stream 逻辑）
            if data:
                try:
                    if data.get("type") == "tool_start":
                        args = data.get("args") or {}
                        if data.get("tool") in {"write_file", "append_to_file", "edit_file"} and args.get("path"):
                            artifact_paths.append(str(args["path"]))
                        collected_steps.append(data)
                    elif data.get("type") == "tool_result":
                        collected_steps.append(data)
                    elif data.get("type") == "thought":
                        collected_steps.append(data)
                    elif data.get("type") in ("subagent_start", "subagent_end"):
                        collected_steps.append(data)
                    elif data.get("type") == "context_compacted":
                        collected_steps.append(data)
                    elif data.get("type") == "todo":
                        todo_data = data.get("todo_list")
                        if todo_data:
                            collected_todo_list = todo_data
                    elif data.get("type") == "done":
                        # 拦截原始 done（仅记录 content），终态段统一构造并下发最终 done，避免双发
                        final_content = data.get("content", "")
                        forwarded_terminal_event = True
                        continue
                    # ponytail: 其余事件（tool/result/thought/...）统一 publish 到下方
                    elif data.get("type") == "error":
                        error_content = data.get("content", "")
                        forwarded_terminal_event = True
                except Exception:
                    pass
            hub.publish(sse_event)
    except asyncio.CancelledError:
        logger.info("[driver] stream cancelled: uid=%s session=%s message=%s", uid, session_id, message_id)
    except Exception as e:
        logger.exception("[driver] stream error")
        err_sse = f"data: {json.dumps({'type': 'error', 'content': f'service internal error: {e}'}, ensure_ascii=False)}\n\n"
        hub.publish(err_sse)
        stream_log.append_event(uid, session_id, message_id, {"type": "error", "content": str(e)})
    finally:
        # 终态处理：落历史 + 清 running + 反思（只执行一次）
        try:
            final_content = final_content or ""
            if final_content:
                final_content = _strip_screenshot_urls(final_content)
                final_content = _append_artifact_links(final_content, uid, artifact_paths)
                _save_assistant_result(uid, session_id, req.message, final_content, collected_steps, collected_todo_list)
                hub.publish(f"data: {json.dumps({'type': 'done', 'content': final_content}, ensure_ascii=False)}\n\n")
            elif error_content:
                _save_assistant_result(uid, session_id, req.message, "❌ " + error_content, collected_steps, collected_todo_list)
            elif artifact_paths:
                summary = _append_artifact_links("任务已完成，文件已保存。", uid, artifact_paths)
                _save_assistant_result(uid, session_id, req.message, summary, collected_steps, collected_todo_list)
                hub.publish(f"data: {json.dumps({'type': 'done', 'content': summary}, ensure_ascii=False)}\n\n")
            elif not forwarded_terminal_event:
                fallback = (
                    "任务已结束，但模型没有生成最终回答"
                    f"（本轮只输出了推理内容、未给出正文，或已接近最大推理步数）。"
                    f"当前最大推理步数为 {agent.config.recursion_limit}。可直接重试；"
                    "若任务较复杂，可提高该值或把任务拆小后再试。"
                )
                _save_assistant_result(uid, session_id, req.message, fallback, collected_steps, collected_todo_list)
                hub.publish(f"data: {json.dumps({'type': 'done', 'content': fallback}, ensure_ascii=False)}\n\n")
            else:
                note = "（本轮已结束，但未生成正文；已记录以下工作步骤。）"
                _save_assistant_result(uid, session_id, req.message, note, collected_steps, collected_todo_list)
                hub.publish(f"data: {json.dumps({'type': 'done', 'content': note}, ensure_ascii=False)}\n\n")
        except Exception as e:
            logger.exception("[driver] finalize error")
            hub.publish(f"data: {json.dumps({'type': 'done', 'content': '服务内部错误: ' + str(e)}, ensure_ascii=False)}\n\n")
        hub.publish("data: [DONE]\n\n")
        hub.finish()
        stream_log.clear_running(uid, session_id, message_id, finished=True)
        asyncio.create_task(_async_reflect(uid, req.message, collected_steps, final_content or "", outcome="error" if error_content else "success"))
