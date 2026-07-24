"""验证 _strip_think_tags：从最终答案剥离内联 <think>...</think> 思考块，且不误伤正常正文。

背景：部分网关（DeepSeek-R1 系、某些兼容代理）不把推理放进 reasoning_content 字段，
而是直接混进 chunk.content 里用 think 标签包裹。若不剥离，思考过程会漏进最终答案。
"""
from agent_core.main import app  # noqa: F401  触发 agent_core 的 sys.path 注入
from agent_core.agent_helpers import _strip_think_tags


def test_paired_block_removed():
    assert _strip_think_tags("<think>reason\nhere</think>最终答案") == "最终答案"


def test_block_in_middle():
    assert _strip_think_tags("前言<think>思考</think>正文") == "前言正文"


def test_thinking_variant_tag():
    assert _strip_think_tags("<thinking>abc</thinking>Hello") == "Hello"


def test_open_only_truncated():
    # 只有起始标签、无闭合（流被截断）：吃到结尾
    assert _strip_think_tags("<think>被截断的推理一直到结尾") == ""


def test_open_only_with_prefix():
    assert _strip_think_tags("答案先出来了<think>又开始碎碎念") == "答案先出来了"


def test_no_tags_untouched():
    # 关键：正常正文（含裸 < 号 / 比较运算）绝不能被误伤
    s = "普通答案，含 < 号和 3<5 比较"
    assert _strip_think_tags(s) == s


def test_multiline_block():
    assert _strip_think_tags("<think>\nl1\nl2\n</think>\n\n# 标题\n正文") == "# 标题\n正文"


def test_case_insensitive():
    assert _strip_think_tags("<THINK>x</THINK>Y") == "Y"


def test_tag_with_attributes():
    assert _strip_think_tags('<think type="reasoning">hmm</think>done') == "done"


def test_empty_and_none_safe():
    assert _strip_think_tags("") == ""
    assert _strip_think_tags(None) is None


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print("ALL_PASS" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
