"""微信 Bot 指令处理（command handling）mixin。

从 wechat_bot.py 拆分（2026-08-03 大文件治理）：把 _handle_message 里
13 个斜杠命令分支（/new /list /switch /delete /projects /project /unproject
/sessions /modals /modal /push /stop /help）与菜单构建逻辑抽到独立模块。

新增一个指令时只改这里，无需通读轮询/发送/生命周期代码。
"""
import hashlib
import uuid
from typing import Optional

import session_store
from logger import get_logger

logger = get_logger(__name__)


def _resolve_session_ref(wechat_uid: str, token: str, menu: Optional[dict], project_id: str = "") -> Optional[str]:
    """将 /switch /delete 的参数解析为真实 sessionId。

    - token 为数字 → 优先用 /list 时缓存的序号映射，否则回退到当前列表顺序
    - 非数字 → 当作原始 sessionId（需真实存在）
    - project_id 非空时：无论序号映射还是原始 id，都要求会话属于该项目，
      否则返回 None（用户切到项目后只操作该项目下的会话）
    - 无法解析返回 None
    """
    if token.isdigit():
        n = int(token)
        if menu and str(n) in menu:
            sid = menu[str(n)]
            if not project_id:
                return sid
            sess = session_store.get_session(wechat_uid, sid)
            if sess and (sess.get("project_id") or "") == project_id:
                return sid
            # menu 序号指向其他项目的会话，回退到项目内列表重新解析
        sessions = (session_store.list_sessions_by_project(wechat_uid, project_id)
                    if project_id else session_store.list_sessions(wechat_uid))
        if 1 <= n <= len(sessions):
            return sessions[n - 1]["id"]
        return None
    sess = session_store.get_session(wechat_uid, token)
    if sess and (not project_id or (sess.get("project_id") or "") == project_id):
        return token
    return None


def _resolve_project_ref(wechat_uid: str, token: str, menu: Optional[dict]) -> Optional[str]:
    """将 /project 的参数解析为真实 project_id。

    - token 为数字 → 优先用 /projects 时缓存的序号映射，否则回退到当前列表顺序
    - 非数字 → 当作原始 project_id（需真实存在）
    - 无法解析返回 None
    """
    if token.isdigit():
        n = int(token)
        if menu and str(n) in menu:
            return menu[str(n)]
        projects = session_store.list_projects(wechat_uid)
        if 1 <= n <= len(projects):
            return projects[n - 1]["id"]
        return None
    if session_store.get_project(wechat_uid, token):
        return token
    return None


class WeChatCommandMixin:
    # ── 指令分发 ──────────────────────────────────

    async def _handle_command(self, text: str, from_user: str, context_token: str, wechat_uid: str) -> bool:
        """处理斜杠命令；返回 True 表示已处理（调用方直接 return）。

        命令分支都不跑 agent（各自直接 send_message 后 return），
        与正在执行的 agent 并发安全；/stop 需要立即响应才可达中断目的。
        """
        # ── /new 命令：创建新会话 ──
        if text.strip() == "/new":
            logger.debug("[微信Bot:%s] 触发 /new 命令", self.user_id)
            new_sid = uuid.uuid4().hex[:8]
            current_pid = self._wechat_current_project.get(from_user, "")
            session_store.create_session(
                wechat_uid, title="新会话", session_id=new_sid,
                project_id=current_pid or None,
            )
            self._wechat_sessions[from_user] = new_sid
            # 会话列表已变化，序号映射失效
            self._wechat_session_menu.pop(from_user, None)
            scope = f"（项目 {current_pid[:8]}）" if current_pid else ""
            await self.send_message(from_user, context_token, f"✅ 已创建新会话{scope}，可以开始新的对话了")
            logger.info("[微信Bot:%s] 用户 %s 创建新会话 %s project=%s", self.user_id, from_user[:16], new_sid, current_pid)
            return True

        # ── /list 命令：列出会话（带序号，供 /switch /delete 按序号操作）──
        # 有当前项目时只列出该项目下的会话；无项目上下文时列出全部（原行为）
        if text.strip() == "/list":
            list_text = self._build_session_list_text(
                wechat_uid, from_user,
                project_id=self._wechat_current_project.get(from_user, ""),
            )
            await self.send_message(from_user, context_token, list_text)
            return True

        # ── /switch 命令：切换会话（支持序号或原始 sessionId，限当前项目内）──
        if text.strip().startswith("/switch "):
            arg = text.strip()[len("/switch "):].strip()
            if not arg:
                await self.send_message(from_user, context_token, "❌ 请指定会话序号或 ID，格式：/switch &lt;序号|sessionId&gt;")
                return True
            target_sid = _resolve_session_ref(
                wechat_uid, arg, self._wechat_session_menu.get(from_user),
                self._wechat_current_project.get(from_user, ""),
            )
            if target_sid is None:
                await self.send_message(from_user, context_token, f"❌ 序号 {arg} 无效。发送 /list 查看可用会话。")
                return True
            sess = session_store.get_session(wechat_uid, target_sid)
            if not sess:
                await self.send_message(from_user, context_token, f"❌ 会话 {target_sid} 不存在。发送 /list 查看可用会话。")
                return True
            self._wechat_sessions[from_user] = target_sid
            await self.send_message(from_user, context_token, f"✅ 已切换到会话 {target_sid}，可以继续对话了")
            logger.info("[微信Bot:%s] 用户 %s 切换到会话 %s", self.user_id, from_user[:16], target_sid)
            return True

        # ── /delete 命令：删除一个或多个历史会话（序号或 sessionId，空格分隔，限当前项目内）──
        if text.strip().startswith("/delete "):
            raw = text.strip()[len("/delete "):].strip()
            if not raw:
                await self.send_message(from_user, context_token, "❌ 请指定会话序号或 ID，格式：/delete &lt;序号|sessionId&gt; [&lt;...&gt;]")
                return True
            tokens = raw.split()
            current_pid = self._wechat_current_project.get(from_user, "")
            deleted_sids: list[str] = []
            invalid: list[str] = []   # 序号无效，无法解析
            skipped: list[str] = []   # 解析到但不存在，跳过
            failed: list[str] = []    # 删除失败
            current_deleted = False
            for tok in tokens:
                sid = _resolve_session_ref(wechat_uid, tok, self._wechat_session_menu.get(from_user), current_pid)
                if sid is None:
                    invalid.append(tok)
                    continue
                sess = session_store.get_session(wechat_uid, sid)
                if not sess:
                    skipped.append(sid)
                    continue
                if not session_store.delete_session(wechat_uid, sid):
                    failed.append(sid)
                    continue
                deleted_sids.append(sid)
                # 若删除的是当前会话，清空映射，下一轮消息会重新创建默认会话
                if self._wechat_sessions.get(from_user) == sid:
                    self._wechat_sessions.pop(from_user, None)
                    current_deleted = True
            # 删除后列表已变，重新生成最新清单并刷新序号映射缓存
            list_text = self._build_session_list_text(wechat_uid, from_user)
            # 构造汇总回复
            parts = [f"🗑️ 已删除 {len(deleted_sids)} 个会话"]
            if deleted_sids:
                parts.append("：" + "、".join(deleted_sids))
            if invalid:
                parts.append(f"\n⚠️ 无效序号已忽略：{', '.join(invalid)}")
            if skipped:
                parts.append(f"\n⚠️ 不存在已跳过：{', '.join(skipped)}")
            if failed:
                parts.append(f"\n❌ 删除失败：{', '.join(failed)}")
            if current_deleted:
                parts.append("\n（当前会话已删除，下一轮消息将自动重建默认会话）")
            # 回显删除后的最新会话清单，便于用户确认与继续操作（序号映射已刷新）
            parts.append("\n\n" + list_text)
            await self.send_message(from_user, context_token, "".join(parts))
            logger.info("[微信Bot:%s] 用户 %s 批量删除会话: 成功=%s 无效=%s 跳过=%s 失败=%s",
                        self.user_id, from_user[:16], deleted_sids, invalid, skipped, failed)
            return True

        # ── /projects 命令：列出所有项目 ──
        if text.strip() == "/projects":
            proj_text = self._build_project_list_text(wechat_uid, from_user)
            await self.send_message(from_user, context_token, proj_text)
            return True

        # ── /project 命令：切换到某项目（后续 /new 将归到该项目）──
        if text.strip().startswith("/project "):
            arg = text.strip()[len("/project "):].strip()
            if not arg:
                await self.send_message(from_user, context_token, "❌ 请指定项目序号或 ID，格式：/project &lt;序号|projectId&gt;")
                return True
            target_pid = _resolve_project_ref(wechat_uid, arg, self._wechat_project_menu.get(from_user))
            if target_pid is None:
                await self.send_message(from_user, context_token, f"❌ 项目 {arg} 无效。发送 /projects 查看可用项目。")
                return True
            proj = session_store.get_project(wechat_uid, target_pid)
            if not proj:
                await self.send_message(from_user, context_token, f"❌ 项目 {target_pid} 不存在。发送 /projects 查看可用项目。")
                return True
            self._wechat_current_project[from_user] = target_pid
            self._save_current_project()
            # 项目上下文已变，会话序号映射失效
            self._wechat_session_menu.pop(from_user, None)
            await self.send_message(from_user, context_token, f"✅ 已切换到项目 {proj['name']} ({target_pid[:8]})，后续 /new 将归到该项目")
            logger.info("[微信Bot:%s] 用户 %s 切换到项目 %s", self.user_id, from_user[:16], target_pid)
            return True

        # ── /unproject 命令：取消当前项目绑定，回到未归属状态 ──
        if text.strip() == "/unproject":
            current = self._wechat_current_project.pop(from_user, None)
            self._save_current_project()
            self._wechat_session_menu.pop(from_user, None)
            if current:
                await self.send_message(from_user, context_token, "✅ 已取消项目绑定，后续 /new 将创建未归属会话")
            else:
                await self.send_message(from_user, context_token, "ℹ️ 当前未绑定任何项目")
            return True

        # ── /sessions 命令：列出当前项目的会话（无项目时列出未归属会话）──
        if text.strip() == "/sessions":
            current_pid = self._wechat_current_project.get(from_user, "")
            list_text = self._build_session_list_text(
                wechat_uid, from_user,
                project_id=current_pid if current_pid else "__unassigned__",
            )
            await self.send_message(from_user, context_token, list_text)
            return True

        # ── /modals 命令：列出可用模型（仅已配置 API Key 的 Provider）──
        if text.strip() == "/modals":
            model_text = self._build_model_list_text(from_user)
            await self.send_message(from_user, context_token, model_text)
            return True

        # ── /modal 命令：切换模型（按 /modals 序号，或直接按模型名）──
        if text.strip() == "/modal" or text.strip().startswith("/modal "):
            arg = text.strip()[len("/modal "):].strip()
            if not arg:
                await self.send_message(
                    from_user, context_token,
                    "❌ 请指定模型序号或名称，格式：/modal <序号|模型名>（先发 /modals 查看可用模型）",
                )
                return True
            entry = None
            if arg.isdigit():
                entry = self._wechat_model_menu.get(from_user, {}).get(arg)
                if entry is None:
                    # 序号缓存可能过期（配置在 Web 端改过），重新构建列表再试一次
                    self._build_model_list_text(from_user)
                    entry = self._wechat_model_menu.get(from_user, {}).get(arg)
                if entry is None:
                    await self.send_message(
                        from_user, context_token,
                        f"❌ 序号 {arg} 无效。发送 /modals 查看最新可用模型。",
                    )
                    return True
            else:
                # 按模型名匹配：精确优先，其次模糊；多个匹配时列出候选让用户用序号精确定位
                cfg = self._ensure_agent().config
                all_models = [
                    (pid, m)
                    for pid, prov in (cfg.providers or {}).items()
                    for m in (prov or {}).get("models") or []
                ]
                exact = [e for e in all_models if e[1] == arg]
                fuzzy = [e for e in all_models if arg in e[1]]
                candidates = exact or fuzzy
                if len(candidates) == 1:
                    entry = candidates[0]
                elif len(candidates) > 1:
                    shown = ", ".join(f"{m}@{p}" for p, m in candidates[:5])
                    await self.send_message(
                        from_user, context_token,
                        f"❌ 模型名 '{arg}' 匹配到多个：{shown}。请用 /modals 的序号切换。",
                    )
                    return True
                else:
                    await self.send_message(
                        from_user, context_token,
                        f"❌ 未找到模型 '{arg}'。发送 /modals 查看可用模型。",
                    )
                    return True
            provider_id, model = entry
            result = self._switch_model(from_user, provider_id, model)
            await self.send_message(from_user, context_token, result)
            logger.info("[微信Bot:%s] 用户 %s 切换模型 -> %s @ %s",
                        self.user_id, from_user[:16], model, provider_id)
            return True

        # ── /push 命令：消息入队，当前任务结束后自动发送（不打断当前任务）──
        stripped = text.strip()
        if stripped == "/push" or stripped.startswith("/push "):
            queued = stripped[len("/push"):].strip()
            if not queued:
                await self.send_message(from_user, context_token,
                    "📥 用法：/push <内容> — 将内容加入队列，当前任务结束后自动发送；空闲时立即执行")
                return True
            q = self._push_queues.setdefault(from_user, [])
            q.append(queued)
            task = self._active_run_task
            if task is not None and not task.done():
                # 任务执行中：入队，等任务结束由 _handle_message 尾部 flush
                await self.send_message(from_user, context_token,
                    f"📥 已入队（当前任务结束后自动发送，队列 {len(q)} 条）")
                logger.info("[微信Bot:%s] 用户 %s /push 入队: %s (队列 %d 条)",
                            self.user_id, from_user[:16], queued[:80], len(q))
            else:
                # 空闲：直接按普通消息完整处理（持锁串行，避免与并发消息乱序）
                logger.info("[微信Bot:%s] 用户 %s /push 空闲直发: %s",
                            self.user_id, from_user[:16], queued[:80])
                async with self._msg_lock:
                    await self._flush_push_queue(from_user, context_token)
            return True

        # ── /steer 命令：实时干预——把内容注入正在执行的任务的下一步（不排队）──
        if stripped == "/steer" or stripped.startswith("/steer "):
            steer = stripped[len("/steer"):].strip()
            if not steer:
                await self.send_message(from_user, context_token,
                    "⚡ 用法：/steer <内容> — 把指令立即注入正在执行的任务的下一步；空闲时等同普通发送")
                return True
            task = self._active_run_task
            uid = f"wechat_{self.user_id}"
            session_id = self._wechat_sessions.get(from_user, "")
            if task is not None and not task.done() and session_id:
                from inbox import get_inbox_manager
                get_inbox_manager().get(uid, session_id).append("step", steer)
                get_inbox_manager().mark_run_active(uid, session_id, True)
                logger.info("[微信Bot:%s] 用户 %s /steer 注入: %s (session=%s)",
                            self.user_id, from_user[:16], steer[:80], session_id)
                await self.send_message(from_user, context_token,
                    "⚡ 打断指令已注入，正在执行的任务将在下一步生效")
            else:
                # 空闲：走与 /push 空闲一致的路——进 push 队列后统一 flush（复用既测试路径）
                logger.info("[微信Bot:%s] 用户 %s /steer 空闲转普通消息: %s",
                            self.user_id, from_user[:16], steer[:80])
                self._push_queues.setdefault(from_user, []).append(steer)
                async with self._msg_lock:
                    await self._flush_push_queue(from_user, context_token)
            return True

        # ── /stop 命令：中断当前正在执行的 agent 请求 ──
        if text.strip() == "/stop":
            if self._cancel_active_run():
                await self.send_message(from_user, context_token, "⏹️ 正在中断当前任务…")
                logger.info("[微信Bot:%s] 用户 %s 发送 /stop，已请求中断当前任务",
                            self.user_id, from_user[:16])
            else:
                await self.send_message(from_user, context_token, "ℹ️ 当前没有正在执行的任务")
            return True

        # ── /help 命令：指令使用说明 ──
        if text.strip() == "/help":
            help_text = (
                "📖 指令帮助\n"
                "/projects — 列出所有项目\n"
                "/project <序号|ID> — 切换项目\n"
                "/unproject — 取消项目绑定\n"
                "/list — 列出当前项目下的会话\n"
                "/sessions — 列出会话（无项目时显示未归属）\n"
                "/new — 创建新会话\n"
                "/switch <序号|ID> — 切换会话\n"
                "/delete <序号|ID> … — 删除会话\n"
                "/modals — 列出可用模型\n"
                "/modal <序号|模型名> — 切换模型\n"
                "/push <内容> — 消息入队，当前任务结束后自动发送\n"
                "/steer <内容> — 打断注入，立即作用于正在执行的任务的下一步\n"
                "/stop — 中断当前正在执行的任务\n"
                "/help — 显示本帮助"
            )
            await self.send_message(from_user, context_token, help_text)
            return True

        return False

    # ── 菜单构建（带序号，供切换/删除命令引用）──────────

    def _build_session_list_text(self, wechat_uid: str, from_user: str, project_id: str = "") -> str:
        """构建带序号的会话清单文本，并刷新 from_user 的序号映射缓存。

        供 /list、/sessions 与 /delete 复用。无会话时返回提示并清空缓存。
        project_id 为空时列出全部会话；否则只列出该项目下的会话。
        """
        if project_id == "__unassigned__":
            all_sessions = session_store.list_sessions_unassigned(wechat_uid)
        elif project_id:
            all_sessions = session_store.list_sessions_by_project(wechat_uid, project_id)
        else:
            all_sessions = session_store.list_sessions(wechat_uid)
        if not all_sessions:
            self._wechat_session_menu.pop(from_user, None)
            scope = "该项目" if project_id else "当前"
            return f"📭 {scope}暂无会话。发送 /new 创建新会话。"
        menu: dict[str, str] = {}
        # 预加载项目映射，用于显示项目名
        project_names: dict[str, str] = {}
        if not project_id:
            try:
                for p in session_store.list_projects(wechat_uid):
                    project_names[p["id"]] = p["name"]
            except Exception:
                pass
        lines = []
        if project_id:
            lines.append(f"📋 项目会话（用序号切换/删除）：")
        else:
            lines.append(f"📋 共有 {len(all_sessions)} 个会话（用序号切换/删除）：")
        for i, s in enumerate(all_sessions, 1):
            sid = s["id"]
            menu[str(i)] = sid
            # 取最后一条用户消息作为摘要
            sess_detail = session_store.get_session(wechat_uid, sid)
            last_user_msg = ""
            if sess_detail and sess_detail.get("messages"):
                for m in reversed(sess_detail["messages"]):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")[:50]
                        break
            marker = "→ " if sid == self._wechat_sessions.get(from_user) else "  "
            tag = ""
            pid = (s.get("project_id") or "").strip()
            if pid and pid != project_id:
                tag = f" [{project_names.get(pid, pid[:8])}]"
            lines.append(f"{marker}{i}. {sid}: {last_user_msg or '(空)'}{tag}")
        self._wechat_session_menu[from_user] = menu
        return "\n".join(lines)

    def _build_project_list_text(self, wechat_uid: str, from_user: str) -> str:
        """构建带序号的项目清单文本，并刷新 from_user 的项目序号映射缓存。"""
        projects = session_store.list_projects(wechat_uid)
        if not projects:
            self._wechat_project_menu.pop(from_user, None)
            return "📭 暂无项目。发送 /projects 查看，或通过 Web 端创建项目。"
        menu: dict[str, str] = {}
        lines = [f"📁 共有 {len(projects)} 个项目（用序号切换）："]
        for i, p in enumerate(projects, 1):
            pid = p["id"]
            menu[str(i)] = pid
            marker = "→ " if pid == self._wechat_current_project.get(from_user) else "  "
            scope = p.get("directory_path") or "默认工作区"
            lines.append(f"{marker}{i}. {p['name']} ({pid[:8]}) — {scope}")
        self._wechat_project_menu[from_user] = menu
        return "\n".join(lines)

    def _build_model_list_text(self, from_user: str) -> str:
        """构建带序号的可用模型清单，并刷新 from_user 的模型序号映射缓存。

        只列出已配置 API Key 的 provider 的模型（未配置 Key 的切换了也调用失败）。
        序号为跨 provider 扁平编号，供 /modal 按序号切换（模型名可能跨 provider 重复，
        用扁平序号可精确定位）。超过微信单条消息长度上限时截断，其余在 Web 端查看。
        """
        cfg = self._ensure_agent().config
        menu: dict[str, tuple[str, str]] = {}
        cur_pid = cfg.active_provider
        cur_model = cfg.model
        cur_name = (cfg.providers or {}).get(cur_pid, {}).get("name", cur_pid)
        all_lines: list[str] = []
        for pid, prov in (cfg.providers or {}).items():
            if not (prov or {}).get("api_key"):
                continue  # 未配置 Key 的 provider 不可用，不列出
            name = (prov or {}).get("name", pid)
            for m in (prov or {}).get("models") or []:
                idx = len(all_lines) + 1
                key = str(idx)
                menu[key] = (pid, m)
                marker = " ← 当前" if (pid == cur_pid and m == cur_model) else ""
                all_lines.append(f"{idx}. {m} @ {name}{marker}")
        self._wechat_model_menu[from_user] = menu
        if not all_lines:
            return "🤖 暂无可用模型（所有 Provider 都未配置 API Key）。请先在 Web 端「设置-模型」中配置。"
        # ponytail: 微信单条消息约 2KB 上限，模型超 30 个时截断提示，剩余在 Web 端查看；
        # 个人部署一般 providers×models < 30，不会触发。
        MAX_LINES = 30
        if len(all_lines) > MAX_LINES:
            shown = all_lines[:MAX_LINES]
            shown.append(f"…共 {len(all_lines)} 个模型，仅显示前 {MAX_LINES} 个，其余请在 Web 端查看")
            all_lines = shown
        return (
            f"🤖 可用模型（当前：{cur_model} @ {cur_name}）\n"
            + "\n".join(all_lines)
            + "\n💡 发送 /modal <序号> 切换模型"
        )

    def _switch_model(self, from_user: str, provider_id: str, model: str) -> str:
        """切换当前微信用户的 agent 模型。

        修改 agent 的配置（active_provider + model）→ 持久化到全局 config.json
        （重启后保持）→ 重建 graph 使新模型立即生效。checkpointer 不变，
        历史会话上下文保留。注意：config.json 是全局配置，Web 端重启后同样生效。
        """
        agent = self._ensure_agent()
        # 保留原有 base_url / api_key，避免切换时把 provider 关键配置清空
        prov = agent.config.providers.get(provider_id, {})
        agent.config.update_provider(
            provider_id,
            model=model,
            base_url=prov.get("base_url", ""),
            api_key=prov.get("api_key", ""),
        )
        agent.config.save()
        agent._rebuild_graph()
        name = agent.config.providers[provider_id].get("name", provider_id)
        return f"✅ 已切换到模型 {model}（{name}），后续对话生效"
