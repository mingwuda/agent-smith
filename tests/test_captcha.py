"""
验证码识别单元测试 —— 直接调用视觉 LLM API 测试 prompt 效果。

测试流程：
  1. 加载历史 captcha 截图（selector 模式和 page 模式）
  2. 使用当前最新的 prompt 调用视觉 LLM
  3. 验证返回结果的有效性（type、坐标数量、置信度等）
"""
import base64
import json
import os
import struct
import sys
import time
from pathlib import Path

# 确保能导入 agent_core 模块
sys.path.insert(0, str(Path(__file__).parent.parent / "agent_core"))

# ── 配置 ─────────────────────────────────────────────────
# API Key 通过环境变量注入，禁止在代码/版本库中硬编码（避免密钥泄露）
API_KEY = os.environ.get("AGENT_API_KEY", "")
API_URL = "https://api.stepfun.com/v1/chat/completions"
MODEL = "step-3.7-flash"

SCREENSHOT_DIR = Path.home() / "agent_workspace" / "admin" / ".browser_screenshots"

# 测试用截图：选 2 个 selector 模式和 1 个 page 模式的
TEST_IMAGES = {
    "selector_306x167": "captcha_1782875935.png",    # 306x167 有 hint
    "selector_330x254": "captcha_1782884379.png",    # 330x254 最新
    "page_1280x720": "captcha_1782873957.png",       # 1280x720 整页
}

PASS = 0
FAIL = 0


# ── 工具函数 ─────────────────────────────────────────────
def png_dimensions(path: str) -> tuple:
    with open(path, "rb") as f:
        f.read(16)
        w = struct.unpack(">I", f.read(4))[0]
        h = struct.unpack(">I", f.read(4))[0]
    return w, h


def build_prompt(is_page_level: bool = False) -> str:
    """与 browser_tools.py 中的 _build_captcha_prompt 保持同步"""
    if is_page_level:
        return (
            "判断图片中是否包含验证码（CAPTCHA）元素并返回 JSON。\n\n"
            "类型：\n"
            "1. text：图片中有扭曲字母或数字验证码，输出 chars。\n"
            "2. click：点选验证码（汉字或图标点选），输出 clicks 数组。\n"
            "   注意：这是整页截图，图标很小。请尽可能给出每个目标的大致像素坐标。\n"
            "   如果不确定精确位置，可以设置 confidence<0.7。\n"
            "3. slider：滑块验证码。\n"
            "4. unknown：没有任何验证码。\n\n"
            "⚠️ 特别注意：\n"
            "- 很多网站的图标点选验证码看起来像装饰性图标面板（建筑剪影、动物、水果、"
            "交通标志等），但只要带有\"刷新/换一批\"按钮，就极可能是验证码。\n"
            "- 不要因为图标看起来像 UI 装饰元素就判断为 unknown。\n"
            "- 如果图片右侧或中间有一个带刷新按钮的图标区域，优先判断为 click。\n"
            "- ⭐ 只输出要求点击的图标，不要列出验证码区域中的所有图标。"
            "通常页面只要求点击 2~3 个。如果看到 6~9 个图标，只有其中几个是需要点的。\n\n"
            "返回 JSON 格式：\n"
            '{"type":"click|text|slider|unknown","chars":"","clicks":[{"char":"汉字或图标名","x":0,"y":0}],"w":宽度,"h":高度,"confidence":0.0,"explain":"说明"}\n'
            "只输出 JSON，不要 Markdown 包裹。"
        )
    return (
        "识别图片中的验证码元素并返回精确坐标 JSON。\n\n"
        "图片是验证码区域的特写截图（不是整页），请精确识别每个点击目标。\n\n"
        "类型：\n"
        "1. click：点选验证码，图片中散落着图标或汉字字符，"
        "需要按页面提示的顺序依次点击。输出 clicks 数组：\n"
        "   - 汉字点选：char 写实际汉字（如 发、送、验）\n"
        "   - 图标点选：char 写图标名称（如 皇冠、眼睛、建筑、铃铛）\n"
        "   - 目标通常分散在验证码区域的不同位置\n"
        "2. text：扭曲字母数字，输出 chars。\n"
        "3. slider：滑块缺口。\n"
        "4. unknown：不是验证码。\n\n"
        "⚠️ 特别注意：\n"
        "- 图标点选验证码的图标可能是建筑剪影、动物、水果、日常用品等，"
        "看起来像装饰元素，但它们是可点击的验证码目标。\n"
        "- 如果图片中有\"刷新/换一批\"按钮、\"请按顺序点击\"等提示文字，"
        "即使图标看起来像 UI 装饰，也一定是 click 类型验证码。\n"
        "- 不要因为图标风格简洁现代就判断为 unknown。\n"
        "- ⭐ 只输出要求点击的图标，不要列出验证码区域中的所有图标。"
        "验证码区域可能展示 6~9 个图标，但通常只要求点击其中 2~3 个。\n"
        "优先参考页面提示文字（instruction_hint）来确认点击目标。\n\n"
        "重要：坐标 (x,y) 相对于图片左上角，看不清就降低 confidence。\n\n"
        "返回 JSON 格式：\n"
        '{"type":"click|text|slider|unknown","chars":"","clicks":[{"char":"汉字或图标名","x":0,"y":0}],"w":宽度,"h":高度,"confidence":0.0,"explain":"说明"}\n'
        "只输出 JSON，不要 Markdown 包裹。"
    )


def extract_json(text: str) -> dict | None:
    """与 browser_tools.py 中的 _extract_json_from_text 同步"""
    import re
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def format_result(parsed: dict, img_w: int, img_h: int, source: str = "page") -> str:
    """与 browser_tools.py 中的 _format_result 同步"""
    import json as _json
    ctype = (parsed.get("type") or "unknown").lower()
    conf = parsed.get("confidence", 0)

    lines = [f"🔐 验证码类型: {ctype}    置信度: {conf}"]
    lines.append(f"📐 验证码尺寸: {img_w} x {img_h}")

    if ctype == "click":
        clicks = parsed.get("clicks") or []
        if not clicks:
            lines.append("⚠️ 未识别到点击目标")
        else:
            lines.append(f"🎯 共 {len(clicks)} 个点击目标:")
            for i, c in enumerate(clicks, 1):
                lines.append(f"  {i}. `{c.get('char','?')}` @ ({c.get('x',0)}, {c.get('y',0)})")
            clicks_json = _json.dumps(clicks, ensure_ascii=False)
            if source == "page":
                lines.append(
                    "→ 调用 browser_click_captcha 执行点击：\n"
                    f"  browser_click_captcha(clicks='{clicks_json}')"
                )
            elif source.startswith("selector:"):
                sel = source[len("selector:"):].strip()
                lines.append(
                    "→ 调用 browser_captcha_click_sequence 执行点击：\n"
                    f"  browser_captcha_click_sequence(selector=\"{sel}\", "
                    f"clicks='{clicks_json}', image_w={img_w}, image_h={img_h})"
                )
            if conf < 0.5:
                lines.append(
                    "⚠️ 置信度较低，建议先点击刷新按钮获取新验证码"
                )
    elif ctype == "unknown":
        lines.append("❓ 无法识别验证码类型，请人工介入或刷新验证码重试")
    elif ctype == "text":
        lines.append(f"🔤 字符: {parsed.get('chars','')}")
        lines.append("→ 调用 browser_fill 填入")
    elif ctype == "slider":
        lines.append("🧩 滑块验证码：先识别缺口位置，再用 browser_slide 拖动滑块")

    explain = parsed.get("explain", "")
    if explain:
        lines.append(f"💡 {explain[:120]}...")
    return "\n".join(lines)


# ── 测试函数 ─────────────────────────────────────────────
def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


async def call_vision_llm(png_path: str, is_page_level: bool, instruction_hint: str = "") -> dict | None:
    """调用视觉 LLM 识别验证码。返回解析后的 JSON dict。"""
    import httpx

    if not API_KEY:
        print("  ⚠️  未设置环境变量 AGENT_API_KEY，跳过视觉 LLM 真实调用")
        return None

    w, h = png_dimensions(png_path)
    with open(png_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    prompt = build_prompt(is_page_level)
    prompt += f"\n\n图片实际尺寸: {w}x{h}"
    if instruction_hint:
        prompt += f"\n\n页面提示文字：{instruction_hint}\n注意：图中按此顺序点击。"

    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 8192,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print(f"  ⚠️  API 调用失败: {e}")
        return None

    content = data["choices"][0]["message"]["content"]
    finish_reason = data["choices"][0].get("finish_reason", "")
    if finish_reason == "length":
        print(f"  ⚠️  finish_reason=length, 响应被截断 ({len(content)} chars)")
    elif not content or len(content.strip()) < 10:
        print(f"  ⚠️  响应为空或过短")

    parsed = extract_json(content)
    if not parsed:
        print(f"  ⚠️  无法解析 JSON。原始响应前 200 字: {content[:200]}")
    return parsed


# ── 单元测试组 ────────────────────────────────────────────
def test_prompt_building():
    """测试 1: prompt 构建"""
    print("\n📦 测试组 1: Prompt 构建")
    
    page_prompt = build_prompt(is_page_level=True)
    sel_prompt = build_prompt(is_page_level=False)
    
    check("page prompt 包含图标面积小提示", "图标很小" in page_prompt)
    check("page prompt 包含刷新按钮提示", "刷新" in page_prompt)
    check("page prompt 包含只输出 2~3 个提示", "2~3" in page_prompt)
    check("selector prompt 包含特写截图提示", "特写截图" in sel_prompt)
    check("selector prompt 包含刷新按钮提示", "刷新" in sel_prompt)
    check("selector prompt 包含只输出 2~3 个提示", "2~3" in sel_prompt)
    check("selector prompt 包含优先参考 instruction_hint", "instruction_hint" in sel_prompt)
    check("page prompt 较短 (< 800 chars)", len(page_prompt) < 800)
    check("selector prompt 适中 (< 950 chars)", len(sel_prompt) < 950)


def test_json_extraction():
    """测试 2: JSON 提取"""
    print("\n📦 测试组 2: JSON 提取")
    
    # 标准无包裹
    r1 = extract_json('{"type":"click","clicks":[{"char":"A","x":10,"y":20}]}')
    check("标准 JSON 解析", r1 is not None and r1["type"] == "click")
    
    # 带 markdown 包裹
    r2 = extract_json('```json\n{"type":"text","chars":"abc"}\n```')
    check("Markdown 包裹 JSON 解析", r2 is not None and r2["type"] == "text")
    
    # 截断 JSON（缺少末尾闭合）
    r3 = extract_json('{"type":"click","clicks":[{"char":"A","x":10,"y":20}],"confidence":0.9')
    check("截断 JSON 容错", r3 is not None and r3.get("type") == "click",
          f"返回 {r3}")
    
    # 空字符串
    r4 = extract_json("")
    check("空字符串返回 None", r4 is None)
    
    # 非 JSON
    r5 = extract_json("这不是 JSON")
    check("非 JSON 返回 None", r5 is None)


def test_format_result():
    """测试 3: 结果格式化"""
    print("\n📦 测试组 3: 结果格式化")
    
    # click 类型
    r1 = format_result({"type":"click","clicks":[{"char":"星","x":100,"y":50}]}, 306, 167, "selector:#captcha")
    check("click 类型包含 browser_captcha_click_sequence", "browser_captcha_click_sequence" in r1)
    check("click 类型包含选择器", "captcha" in r1)
    check("click 类型包含坐标", "100" in r1)
    
    # page 模式
    r2 = format_result({"type":"click","clicks":[{"char":"星","x":100,"y":50}]}, 1280, 720, "page")
    check("page 模式包含 browser_click_captcha", "browser_click_captcha" in r2)
    
    # 低置信度提示
    r3 = format_result({"type":"click","clicks":[{"char":"星","x":100,"y":50}],"confidence":0.3}, 306, 167, "page")
    check("低置信度包含刷新提示", "刷新" in r3)
    
    # 未知类型
    r4 = format_result({"type":"unknown"}, 306, 167, "page")
    check("unknown 类型提示人工介入", "人工介入" in r4)


async def test_vision_llm_selector():
    """测试 4: 视觉 LLM - selector 模式（特写截图，最关键）"""
    print("\n📦 测试组 4: 视觉 LLM - selector 模式 (306x167 / 330x254)")
    
    for name, fname in TEST_IMAGES.items():
        if "selector" not in name:
            continue
        path = SCREENSHOT_DIR / fname
        if not path.exists():
            print(f"  ⚠️  跳过: {fname} 不存在")
            continue
        
        w, h = png_dimensions(str(path))
        print(f"\n  🔍 测试: {fname} ({w}x{h})")
        
        hint = "请按顺序点击"
        parsed = await call_vision_llm(str(path), is_page_level=False, instruction_hint=hint)
        
        if parsed is None:
            check(f"{name}: API 返回有效结果", False, "API 调用失败")
            continue
        
        ctype = parsed.get("type", "")
        conf = parsed.get("confidence", 0)
        clicks = parsed.get("clicks") or []
        
        print(f"    类型: {ctype}, 置信度: {conf}, 点击数: {len(clicks)}")
        for c in clicks:
            print(f"      `{c.get('char','?')}` @ ({c.get('x',0)}, {c.get('y',0)})")
        if parsed.get("explain"):
            print(f"    说明: {parsed['explain'][:100]}")
        
        # 验证标准
        check(f"{name}: 正确识别为 click", ctype == "click",
              f"返回了 {ctype}")
        check(f"{name}: 置信度 >= 0.5", conf >= 0.5,
              f"置信度 {conf} 偏低")
        check(f"{name}: 点击数合理 (2~4)", 2 <= len(clicks) <= 4,
              f"点击数 {len(clicks)}，预期 2~4")
        check(f"{name}: 图标名称非空", all(c.get("char") for c in clicks),
              "存在空的 char 字段")
        check(f"{name}: 坐标在图片范围内", 
              all(0 <= c.get("x", -1) <= w and 0 <= c.get("y", -1) <= h for c in clicks),
              "坐标超出图片范围")


async def test_vision_llm_page():
    """测试 5: 视觉 LLM - page 模式（全页截图）"""
    print("\n📦 测试组 5: 视觉 LLM - page 模式 (1280x720)")
    
    for name, fname in TEST_IMAGES.items():
        if "page" not in name:
            continue
        path = SCREENSHOT_DIR / fname
        if not path.exists():
            continue
        
        w, h = png_dimensions(str(path))
        print(f"\n  🔍 测试: {fname} ({w}x{h})")
        
        parsed = await call_vision_llm(str(path), is_page_level=True)
        
        if parsed is None:
            check(f"{name}: API 返回有效结果", False)
            continue
        
        ctype = parsed.get("type", "")
        conf = parsed.get("confidence", 0)
        
        print(f"    类型: {ctype}, 置信度: {conf}")
        if parsed.get("explain"):
            print(f"    说明: {parsed['explain'][:100]}")
        
        # page 模式主要检测是否能识别出验证码存在
        check(f"{name}: 至少不是 unknown", ctype != "unknown",
              f"返回 unknown（可能是空白页或广告）")
        check(f"{name}: 置信度 > 0", conf > 0,
              f"置信度为 0")


async def main():
    print("=" * 60)
    print("  验证码识别单元测试")
    print(f"  模型: {MODEL}")
    print(f"  API: {API_URL}")
    print("=" * 60)
    
    # 单元测试（不需 API 调用）
    test_prompt_building()
    test_json_extraction()
    test_format_result()
    
    # API 集成测试（需要调用视觉 LLM）
    await test_vision_llm_selector()
    await test_vision_llm_page()
    
    # 汇总
    total = PASS + FAIL
    print("\n" + "=" * 60)
    print(f"  测试完成: {total} 项")
    print(f"  ✅ 通过: {PASS}")
    print(f"  ❌ 失败: {FAIL}")
    if FAIL == 0:
        print("  🎉 全部通过！")
    else:
        print(f"  通过率: {PASS/total*100:.0f}%")
    print("=" * 60)
    
    return FAIL == 0


if __name__ == "__main__":
    import asyncio
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
