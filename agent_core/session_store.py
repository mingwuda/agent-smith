"""会话存储 —— 按用户隔离的 SQLite 数据库"""
import base64
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional


def _decode_message_content(content: str) -> dict:
    """解析存储的消息内容。可能为纯文本或 JSON（含图片/步骤）。"""
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            result: dict = {}
            # 文本内容：优先 text 字段，其次 content 字段
            if "text" in payload:
                result["content"] = payload["text"]
            elif "content" in payload:
                result["content"] = payload["content"]
            # 图片（用户消息）
            images = payload.get("images", [])
            if images:
                data_urls = []
                for img in images:
                    # 已经是 data URL 格式（如微信 Bot 直接传入的 base64）
                    if str(img).startswith("data:image/"):
                        data_urls.append(str(img))
                    # 本地文件路径（如 Web 端上传后保存的路径）
                    else:
                        try:
                            raw = Path(img).read_bytes()
                            ext = Path(img).suffix.lstrip(".") or "png"
                            b64 = base64.b64encode(raw).decode()
                            data_urls.append(f"data:image/{ext};base64,{b64}")
                        except Exception:
                            pass
                if data_urls:
                    result["images"] = data_urls
            # 步骤卡片（助手消息）
            if "steps" in payload:
                result["steps"] = payload["steps"]
            if "todo_list" in payload:
                result["todo_list"] = payload["todo_list"]
            if result:
                return result
    except (json.JSONDecodeError, ValueError):
        pass
    return {"content": content}

import user_manager

DATA_DIR = Path.home() / ".desktop_agent"
GLOBAL_DB = DATA_DIR / "sessions.sqlite3"
MIGRATION_KEY = "json_sessions_migrated"


def _timestamp() -> str:
    return datetime.now().isoformat()


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _db_path(user_id: str) -> Path:
    """每个用户独立的 SQLite 数据库"""
    udir = user_manager.session_dir(user_id)
    return udir / "sessions.sqlite3"


@contextmanager
def _connect(user_id: str = "default"):
    _ensure_dir()
    db_path = _db_path(user_id)
    # timeout: 写锁等待上限(秒)，避免瞬时并发直接抛 "database is locked"
    # journal_mode=WAL + busy_timeout: 提升读写并发能力，写冲突时自动重试等待
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        _init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_db(conn: sqlite3.Connection):
    # ── 会话表 ──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # 兼容性迁移：添加 workspace 列（旧数据库没有该列）
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN workspace TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 兼容性迁移：添加 project_id 外键（关联项目）
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # ── 项目表 ──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            directory_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # ── 消息表 ──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id)"
    )


def _row_to_session(row: sqlite3.Row, include_messages: bool = False, messages: Optional[list[dict]] = None) -> dict:
    session = {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"],
    }
    try:
        session["workspace"] = row["workspace"] or ""
    except (IndexError, KeyError):
        session["workspace"] = ""
    try:
        session["project_id"] = row["project_id"] or ""
    except (IndexError, KeyError):
        session["project_id"] = ""
    if include_messages:
        session["messages"] = messages or []
    return session


def create_session(user_id: str = "default", title: Optional[str] = None, session_id: Optional[str] = None, project_id: Optional[str] = None) -> dict:
    """创建一个新会话"""
    session_id = session_id or str(uuid.uuid4())[:8]
    now = _timestamp()
    with _connect(user_id) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (id, title, created_at, updated_at, project_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, title or f"会话 {now[:16].replace('T', ' ')}", now, now, project_id or ''),
        )
    return get_session(user_id, session_id) or {
        "id": session_id,
        "title": title or f"会话 {now[:16].replace('T', ' ')}",
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "messages": [],
    }


def add_message(user_id: str, session_id: str, role: str, content: str) -> Optional[dict]:
    """追加一条消息到会话"""
    now = _timestamp()
    with _connect(user_id) as conn:
        exists = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            return None
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
    return get_session(user_id, session_id)


def list_sessions(user_id: str = "default") -> list[dict]:
    """列出某个用户的所有会话"""
    with _connect(user_id) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count,
                   s.workspace, COALESCE(s.project_id,'') AS project_id
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
        return [_row_to_session(row) for row in rows]


def get_session(user_id: str, session_id: str) -> Optional[dict]:
    """获取单个会话（含消息）"""
    with _connect(user_id) as conn:
        row = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count,
                   s.workspace, COALESCE(s.project_id,'') AS project_id
            FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.id = ? GROUP BY s.id
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        msg_rows = conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        messages = [
            _decode_message_content(r["content"])
            | {"role": r["role"], "timestamp": r["timestamp"]}
            for r in msg_rows
        ]
        return _row_to_session(row, include_messages=True, messages=messages)


def get_session_lite(user_id: str, session_id: str, limit: int = 20, offset: int = -20) -> Optional[dict]:
    """获取单个会话的轻量版本（仅消息元数据，不含 steps/todo/content 详情）

    参数:
      limit: 返回消息数量上限，默认 20
      offset: 偏移量。负数表示从末尾往前取（如 -20 表示最后 20 条），正数表示从开头跳过
    """
    with _connect(user_id) as conn:
        row = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count,
                   s.workspace, COALESCE(s.project_id,'') AS project_id
            FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.id = ? GROUP BY s.id
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None

        # 单独查询消息总数，避免 sessions.message_count 字段未维护导致不准
        total_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]

        # 构建分页查询
        if offset < 0:
            # 从末尾往前取：先算起始位置
            start = max(0, total_count + offset)
            end = total_count
            msg_rows = conn.execute(
                "SELECT id, role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                (session_id, limit, start),
            ).fetchall()
        else:
            # 从开头跳过 offset 条
            msg_rows = conn.execute(
                "SELECT id, role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()

        messages = []
        for i, r in enumerate(msg_rows):
            parsed = _decode_message_content(r["content"])
            # index 必须是全量列表中的真实位置，否则详情接口取值会偏移
            real_index = (start if offset < 0 else offset) + i
            msg_meta = {
                "role": r["role"],
                "timestamp": r["timestamp"],
                "index": real_index,
            }
            # 用户消息：保留 content 和 images，截断大文本
            if r["role"] == "user":
                content_text = parsed.get("content", "") or ""
                msg_meta["content"] = content_text[:200]
                if "images" in parsed:
                    msg_meta["images"] = parsed["images"]
            else:
                # 助手消息：只保留 content 和是否有 steps/todo 的标记
                content_text = parsed.get("content", "") or ""
                msg_meta["content"] = content_text[:200]
                msg_meta["has_steps"] = bool(parsed.get("steps"))
                msg_meta["has_todo"] = bool(parsed.get("todo_list"))
                # 保留 content_preview 用于占位显示
                msg_meta["content_preview"] = content_text[:120]
            messages.append(msg_meta)

        # 判断是否还有更早的消息
        if offset < 0:
            has_more = total_count > len(messages)
        else:
            has_more = offset > 0

        result = _row_to_session(row, include_messages=True, messages=messages)
        result["has_more"] = has_more
        result["total_count"] = total_count
        return result


def get_message_detail(user_id: str, session_id: str, message_index: int) -> Optional[dict]:
    """获取单条消息的完整详情（含 steps/todo/content）"""
    with _connect(user_id) as conn:
        msg_rows = conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        if message_index < 0 or message_index >= len(msg_rows):
            return None
        r = msg_rows[message_index]
        parsed = _decode_message_content(r["content"])
        return parsed | {"role": r["role"], "timestamp": r["timestamp"], "index": message_index}


def delete_session(user_id: str, session_id: str) -> bool:
    """删除会话"""
    with _connect(user_id) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0


def rename_session(user_id: str, session_id: str, new_title: str) -> bool:
    """重命名会话"""
    now = _timestamp()
    with _connect(user_id) as conn:
        cur = conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (new_title, now, session_id),
        )
        return cur.rowcount > 0


def set_session_workspace(user_id: str, session_id: str, workspace: str) -> bool:
    """设置会话的工作目录"""
    now = _timestamp()
    with _connect(user_id) as conn:
        cur = conn.execute(
            "UPDATE sessions SET workspace = ?, updated_at = ? WHERE id = ?",
            (workspace, now, session_id),
        )
        return cur.rowcount > 0


def get_session_workspace(user_id: str, session_id: str) -> str:
    """获取会话的工作目录。

    优先级（方案B：项目目录优先于会话级 workspace）：
      1. 所属项目的 directory_path（项目目录权威，避免旧会话级 workspace
         把 agent 指到错误目录）
      2. 会话自身 workspace（仅当会话未关联项目、或项目未设目录时兜底）
      3. 跨命名空间回退：项目可能建在 default 库

    返回空串表示使用默认用户工作区。
    """
    pid = ""
    sess_ws = ""
    with _connect(user_id) as conn:
        row = conn.execute(
            "SELECT workspace, project_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return ""
        try:
            sess_ws = (row["workspace"] or "").strip()
        except (IndexError, KeyError):
            sess_ws = ""
        try:
            pid = (row["project_id"] or "").strip()
        except (IndexError, KeyError):
            pid = ""
        # 1. 项目目录优先
        if pid:
            dp = _project_dir_of(conn, pid)
            if dp:
                return dp
    # 2. 跨命名空间回退：项目可能建在 default 库
    if pid and user_id and user_id != "default":
        try:
            with _connect("default") as dconn:
                dp = _project_dir_of(dconn, pid)
                if dp:
                    return dp
        except Exception:
            pass
    # 3. 会话级 workspace 兜底（无项目、或项目无目录时）
    if sess_ws:
        return sess_ws
    return ""


def get_project_workspace(user_id: str, project_id: str) -> str:
    """直接按项目 id 取 directory_path（含跨命名空间回退）。

    供前端实时切换项目时使用：用户当前选中的项目目录立即生效，
    不依赖会话表里 project_id 是否已同步。
    """
    if not project_id:
        return ""
    with _connect(user_id) as conn:
        dp = _project_dir_of(conn, project_id)
        if dp:
            return dp
    if user_id and user_id != "default":
        try:
            with _connect("default") as dconn:
                dp = _project_dir_of(dconn, project_id)
                if dp:
                    return dp
        except Exception:
            pass
    return ""


def _project_dir_of(conn, project_id: str) -> str:
    """在当前连接的 projects 表中查项目目录，取到则返回 stripped 路径，否则空串。"""
    try:
        proj = conn.execute(
            "SELECT directory_path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if proj:
            dp = proj["directory_path"]
            if dp and dp.strip():
                return dp.strip()
    except (IndexError, KeyError, sqlite3.OperationalError):
        pass
    return ""


# ═══════════════════════════════════════════════
#  项目管理（Workspace 侧边栏）
# ═══════════════════════════════════════════════

def create_project(user_id: str, name: str, directory_path: str = "") -> dict:
    """创建项目"""
    project_id = "proj_" + str(uuid.uuid4())[:8]
    now = _timestamp()
    with _connect(user_id) as conn:
        conn.execute(
            "INSERT INTO projects (id, name, directory_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, name, directory_path, now, now),
        )
    return get_project(user_id, project_id) or {
        "id": project_id, "name": name, "directory_path": directory_path,
        "created_at": now, "updated_at": now,
    }


def list_projects(user_id: str = "default") -> list[dict]:
    """列出所有项目（含每个项目的会话数）"""
    with _connect(user_id) as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.directory_path, p.created_at, p.updated_at,
                   COUNT(s.id) AS session_count
            FROM projects p
            LEFT JOIN sessions s ON s.project_id = p.id
            GROUP BY p.id
            ORDER BY p.updated_at DESC
            """
        ).fetchall()
        projects = []
        for r in rows:
            projects.append({
                "id": r["id"],
                "name": r["name"],
                "directory_path": r["directory_path"] or "",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "session_count": r["session_count"] or 0,
            })
        return projects


def get_project(user_id: str, project_id: str) -> Optional[dict]:
    """获取单个项目详情"""
    with _connect(user_id) as conn:
        row = conn.execute(
            """
            SELECT p.id, p.name, p.directory_path, p.created_at, p.updated_at,
                   COUNT(s.id) AS session_count
            FROM projects p LEFT JOIN sessions s ON s.project_id = p.id
            WHERE p.id = ? GROUP BY p.id
            """,
            (project_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"], "name": row["name"],
            "directory_path": row["directory_path"] or "",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "session_count": row["session_count"] or 0,
        }


def update_project(user_id: str, project_id: str, name: Optional[str] = None,
                  directory_path: Optional[str] = None) -> bool:
    """更新项目名称或目录路径"""
    now = _timestamp()
    sets = []
    vals = []
    if name is not None:
        sets.append("name = ?")
        vals.append(name)
    if directory_path is not None:
        sets.append("directory_path = ?")
        vals.append(directory_path)
    # updated_at 始终更新，且必须放在所有可选字段之后，保证 SET 子句与参数顺序一致
    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(project_id)
    sql = f"UPDATE projects SET {', '.join(sets)} WHERE id = ?"
    with _connect(user_id) as conn:
        cur = conn.execute(sql, vals)
        return cur.rowcount > 0


def delete_project(user_id: str, project_id: str) -> bool:
    """删除项目（其下属会话的 project_id 被置空，会话本身不删）"""
    with _connect(user_id) as conn:
        # 先将关联会话的 project_id 置空
        conn.execute(
            "UPDATE sessions SET project_id = '' WHERE project_id = ?",
            (project_id,),
        )
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0


def set_session_project(user_id: str, session_id: str, project_id: str) -> bool:
    """将会话归属到某个项目"""
    now = _timestamp()
    with _connect(user_id) as conn:
        cur = conn.execute(
            "UPDATE sessions SET project_id = ?, updated_at = ? WHERE id = ?",
            (project_id, now, session_id),
        )
        return cur.rowcount > 0


def list_sessions_by_project(user_id: str, project_id: str) -> list[dict]:
    """列出某个项目下的所有会话"""
    with _connect(user_id) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count,
                   s.workspace, s.project_id
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.project_id = ?
            GROUP BY s.id ORDER BY s.updated_at DESC
            """,
            (project_id,),
        ).fetchall()
        return [_row_to_session(r) for r in rows]


def list_sessions_unassigned(user_id: str = "default") -> list[dict]:
    """列出未归属任何项目的会话"""
    with _connect(user_id) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count,
                   s.workspace, COALESCE(s.project_id,'')
            FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
            WHERE COALESCE(s.project_id,'') = '' OR s.project_id IS NULL
            GROUP BY s.id ORDER BY s.updated_at DESC
            """
        ).fetchall()
        return [_row_to_session(r) for r in rows]
