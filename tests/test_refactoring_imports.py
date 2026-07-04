"""验证重构后模块导入正确性的测试。"""
import sys
from pathlib import Path

# 确保能找到 agent_core
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_services_imports():
    """验证 services/ 模块可导入且关键函数存在。"""
    from agent_core.services.workspace import (
        _workspace_for_user,
        _resolve_artifact_path,
        _append_artifact_links,
        _extract_zip,
        _safe_attachments,
        _display_user_message,
        _user_image_urls,
    )
    from agent_core.services.agent_service import (
        _ensure_session,
        _is_skill_inventory_query,
        _image_model_override,
        _format_loaded_skills,
        _save_assistant_result,
        _strip_screenshot_urls,
        _resolve_user,
        _apply_session_workspace,
        _async_reflect,
    )


def test_deps_imports():
    """验证 api/deps.py 可导入。"""
    from agent_core.api.deps import (
        AUTH_COOKIE_NAME,
        _load_auth_config,
        _auth_config,
        _sign_session,
        _verify_session,
        _get_current_user,
        _require_admin,
    )


def test_auth_imports():
    """验证 api/auth.py 可导入（路由模块）。"""
    from agent_core.api.auth import router


def test_route_modules_import():
    """验证所有 api/routes/ 模块可导入。"""
    from agent_core.api.routes.agent import router as r1
    from agent_core.api.routes.sessions import router as r2
    from agent_core.api.routes.skills import router as r3
    from agent_core.api.routes.artifacts import router as r4
    from agent_core.api.routes.db import router as r5
    from agent_core.api.routes.system import router as r6
    from agent_core.api.routes.wechat import router as r7
    from agent_core.api.routes.monitoring import router as r8
    assert all(r is not None for r in [r1, r2, r3, r4, r5, r6, r7, r8])


def test_main_slimmed():
    """验证新 main.py 行数大幅减少（<500 行）。"""
    main_path = Path(__file__).parent.parent / "agent_core" / "main.py"
    lines = len(main_path.read_text().splitlines())
    assert lines < 500, f"main.py 仍有 {lines} 行，未充分精简"
    print(f"  main.py: {lines} 行 ✅")


def test_total_modules():
    """验证模块总数 >= 12 个。"""
    services = list((Path(__file__).parent.parent / "agent_core" / "services").glob("*.py"))
    api = list((Path(__file__).parent.parent / "agent_core" / "api").glob("*.py"))
    routes = list((Path(__file__).parent.parent / "agent_core" / "api" / "routes").glob("*.py"))
    # 过滤 __init__.py
    modules = [f for f in services + api + routes if f.name != "__init__.py"]
    assert len(modules) >= 12, f"只有 {len(modules)} 个模块，期望至少 12 个"
    print(f"  拆分模块: {len(modules)} 个 ✅")
    for m in sorted(modules):
        rel = m.relative_to(Path(__file__).parent.parent)
        print(f"    {rel}")
