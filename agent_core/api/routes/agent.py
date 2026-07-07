"""Agent 运行路由"""
import asyncio
import hashlib
import json
import re
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
)
from logger import set_log_context, get_logger
from config import AgentConfig
from agent import DesktopAgent
import session_store

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


class RunResponse(BaseModel):
    result: str
    steps: list[dict] = []
    todo_list: Optional[dict] = None


# ---------- 全局 Agent 实例 ----------

agent: Optional[DesktopAgent] = None


def init_agent():
    """初始化 Agent（委托 main 模块，同步本地引用）"""
    global agent
    from main import init_agent as _main_init
    _main_init()
    from main import agent as _main_agent
    agent = _main_agent


# ---------- 路由 ----------


@router.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest, request: Request):
    """发送消息给 Agent 并获取回复"""
    if not agent:
        init_agent()
    if not agent:
        logger.error("Agent 初始化失败，请检查 API Key 设置")
        raise HTTPException(503, "Agent 初始化失败，请检查 API Key 设置")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    message_id = str(uuid.uuid4())
    set_log_context(session_id=session_id, message_id=message_id)
    _apply_session_workspace(uid, session_id)
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    display_text = _display_user_message(uid, req.message, attachments)
    session_store.add_message(uid, session_id, "user", display_text)
    model_override = _image_model_override(attachments)
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
    )
    # 从 todo store 取出清单（非流式模式）
    todo_list_r = None
    try:
        from tools.todo_tools import get_todo_list
        todo_list_r = get_todo_list()
    except Exception:
        pass
    artifact_paths = [
        str(step.get("args", {}).get("path", ""))
        for step in steps
        if step.get("type") == "tool_call"
        and step.get("tool") in {"write_file", "append_to_file"}
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
        init_agent()
    if not agent:
        raise HTTPException(503, "Agent 初始化失败")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    message_id = str(uuid.uuid4())
    set_log_context(session_id=session_id, message_id=message_id)
    _apply_session_workspace(uid, session_id)
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    display_text = _display_user_message(uid, req.message, attachments)
    session_store.add_message(uid, session_id, "user", display_text)
    model_override = _image_model_override(attachments)

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

        async def skill_inventory_stream():
            yield f"data: {json.dumps({'type': 'done', 'content': result}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            skill_inventory_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    artifact_paths: list[str] = []
    collected_steps: list[dict] = []  # 收集步骤卡片数据，将存入历史
    collected_todo_list = None  # 收集 todo 清单数据
    
    async def event_stream():
        final_content = ""
        error_content = ""
        forwarded_terminal_event = False
        yielded_count = 0
        if model_override:
            yield f"data: {json.dumps({'type': 'model_switch', 'model': model_override, 'reason': '图片输入'}, ensure_ascii=False)}\n\n"
        stream = agent._stream_done_wrapper(
            agent_message,
            history=history_messages,
            attachments=attachments,
            model_override=model_override,
            thread_id=session_id,
        )
        try:
            async for sse_event in stream:
                if await request.is_disconnected():
                    logger.info("[run/stream] 客户端断开，停止事件流")
                    await stream.aclose()
                    return
                if sse_event.strip() == "data: [DONE]":
                    continue
                yielded_count += 1
                logger.debug("[run/stream] yield event: %s", sse_event[:80])
                if '"type": "tool_start"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            data = json.loads(m.group(1))
                            args = data.get("args") or {}
                            if data.get("tool") in {"write_file", "append_to_file"} and args.get("path"):
                                artifact_paths.append(str(args["path"]))
                            collected_steps.append(data)  # 收集步骤
                    except Exception:
                        pass
                elif '"type": "tool_result"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            collected_steps.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                elif '"type": "thought"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            collected_steps.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                elif '"type": "subagent_start"' in sse_event or '"type": "subagent_end"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            collected_steps.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                elif '"type": "todo"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            todo_data = json.loads(m.group(1)).get("todo_list")
                            if todo_data:
                                nonlocal collected_todo_list
                                collected_todo_list = todo_data
                    except Exception:
                        pass
                if '"type": "done"' in sse_event:
                    m = re.search(r'data: ({.*})', sse_event)
                    if m:
                        final_content = json.loads(m.group(1)).get("content", "")
                    forwarded_terminal_event = True
                    continue
                if '"type": "error"' in sse_event:
                    m = re.search(r'data: ({.*})', sse_event)
                    if m:
                        error_content = json.loads(m.group(1)).get("content", "")
                    forwarded_terminal_event = True
                yield sse_event
        except asyncio.CancelledError:
            await stream.aclose()
            return
        except Exception as e:
            logger.exception("SSE 流异常: async for 循环内未捕获的异常")
            await stream.aclose()
            yield f"data: {json.dumps({'type': 'error', 'content': f'服务内部错误: {e}'}, ensure_ascii=False)}\n\n"
            return

        logger.info(
            "[run/stream] stream_run 返回: yielded_count=%d, final_content_len=%d, error_content_len=%d, forwarded_terminal=%s",
            yielded_count,
            len(final_content),
            len(error_content),
            forwarded_terminal_event,
        )
        try:
            final_content = final_content or ""
            if final_content:
                final_content = _append_artifact_links(final_content, uid, artifact_paths)
                if "/api/screenshot" in final_content:
                    logger.warning("[run/stream] ⚠️ final_content 仍包含截图引用！来源待查")
                logger.info("[run/stream] 发送 done: content_len=%d", len(final_content))
                yield f"data: {json.dumps({'type': 'done', 'content': final_content}, ensure_ascii=False)}\n\n"
                _save_assistant_result(uid, session_id, req.message, final_content, collected_steps, collected_todo_list)
            elif error_content:
                logger.info("[run/stream] 保存 error 结果: error_len=%d", len(error_content))
                _save_assistant_result(uid, session_id, req.message, "❌ " + error_content, collected_steps, collected_todo_list)
            elif artifact_paths:
                summary = _append_artifact_links("任务已完成，文件已保存。", uid, artifact_paths)
                logger.info("[run/stream] 发送 artifact 总结: %s", summary)
                yield f"data: {json.dumps({'type': 'done', 'content': summary}, ensure_ascii=False)}\n\n"
                _save_assistant_result(uid, session_id, req.message, summary, collected_steps, collected_todo_list)
            elif not forwarded_terminal_event:
                fallback = (
                    "任务已结束，但模型没有生成最终回答。"
                    "这通常发生在接近最大推理步数时，模型仍在继续调用工具。"
                    f"当前最大推理步数为 {agent.config.recursion_limit}，可以提高该值，或把任务拆小后重试。"
                )
                logger.info("[run/stream] 发送 fallback: %s", fallback)
                yield f"data: {json.dumps({'type': 'done', 'content': fallback}, ensure_ascii=False)}\n\n"
                _save_assistant_result(uid, session_id, req.message, fallback, collected_steps, collected_todo_list)
            else:
                logger.info("[run/stream] 已转发 terminal 事件，不再发送兜底")
        except Exception as e:
            logger.exception("SSE 流处理异常")
            err_msg = f"服务内部错误: {e}"
            yield f"data: {json.dumps({'type': 'done', 'content': err_msg}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        # 后台反思
        asyncio.create_task(_async_reflect(uid, req.message, collected_steps, final_content or ""))
    
    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
