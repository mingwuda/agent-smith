"""工具结果状态归类（唯一出口）。

问题（P1-5）：工具「成功 / 空结果 / 报错 / 超时」此前只靠一条隐含约定
——返回值以 `❌` 开头算错，其余一律算成功。

设计原则（评审后修正）：**只信工具自己写的判定前缀，不扫正文关键字。**

为什么不能扫正文：这个 agent 日常就在读日志、grep 报错、cat 错误堆栈。
一旦把「错误 / failed / timeout」当判据，`echo '日志：未发现错误'`（退出码 0）
就会被判成失败——这是实测复现过的误报，对读日志的场景是高频噪声。

因此状态来源只有两个：
1. 返回值开头的显式标记（`❌` 硬失败、`⏱` 转后台、控制流标记）；
2. 空字符串。

超时（timeout）现在**必须显式标注**：工具自己要报告超时，得返回带
`__TIMEOUT__` 或 ⏱ 前缀的文本。不再从正文里猜。
"""
from __future__ import annotations

from typing import Literal

ToolStatus = Literal["ok", "empty", "error", "timeout"]

# 只有「工具自己明确说这是超时」才算 timeout。注意：
# `⏱ 命令前台执行 30s 仍未完成，已转后台` 是**成功路径**（命令还在后台正常跑），
# 不是超时失败——评审 P0-1 指出的误判。转后台用独立标记表示。
_PREFIX_ERROR = ("❌",)                    # 工具自身硬失败
_PREFIX_TIMEOUT = ("⌛", "__TIMEOUT__")      # 明确的超时终止
_PREFIX_ASYNC = ("⏱",)                      # 已转后台继续跑（不是失败）
_PREFIX_CONTROL = ("__CONFIRM_NEEDED__", "__PERMISSION_NEEDED__")
_EMPTY_MARKER = "[状态: empty]"             # 工具自报「成功但无输出」


def classify(output: str) -> ToolStatus:
    """把工具返回文本归类为 ok / empty / error / timeout。

    判定顺序（全部基于**开头若干字符**，绝不扫正文）：
      控制流标记 → ok（等用户操作，不是失败）
      `⏱`        → ok（已转后台，命令仍在正常运行）
      `⌛`/`__TIMEOUT__` → timeout（真的超时被终止）
      `❌`        → error（工具自身硬失败）
      空白       → empty
      其余       → ok
    """
    if output is None:
        return "empty"
    # 防御：工具出口可能收到非 str（bytes 等），强转再判定，
    # 否则 startswith 遇 bytes 会抛 TypeError 打断整轮对话。
    text = output if isinstance(output, str) else str(output)
    text = text.strip()
    if not text:
        return "empty"

    head = text[:24]  # 只看开头，正文内容一律不作为判据
    if head.startswith(_PREFIX_CONTROL):
        return "ok"
    if head.startswith(_PREFIX_TIMEOUT):
        return "timeout"
    if head.startswith(_PREFIX_ASYNC):
        return "ok"
    if head.startswith(_PREFIX_ERROR):
        return "error"
    # 工具自报的空结果（带了提示文本，但语义仍是 empty）
    if _EMPTY_MARKER in text[:200]:
        return "empty"
    return "ok"


def is_error(status: ToolStatus) -> bool:
    """前端卡片是否该标红。

    timeout 与 error 都是「模型需要知道出了问题」，都标红；
    但二者语义不同（前者可调大超时重试，后者通常该换策略）。
    """
    return status in ("error", "timeout")


def annotate_for_model(output: str, status: ToolStatus) -> str:
    """给模型的工具结果加状态提示，仅对「空」和「超时」加。

    最容易被模型误判的只有两种：
    - empty：命令成功但没输出。模型常误判为失败而原样重试，白烧 token。
    - timeout：没做完，应调大时限或缩小任务范围，而不是原样重试。

    ok / error 不加前缀：error 已有 `❌`（模型能看懂），ok 是绝大多数情况，
    加前缀纯属浪费 token 并干扰阅读。
    """
    if status == "empty":
        return "[状态: empty] 工具执行成功，但没有任何输出。这不代表失败——不要原样重试同一调用。"
    if status == "timeout":
        return "[状态: timeout] 工具未在时限内完成，结果可能不完整。考虑调大 timeout 或换更小的任务范围。"
    return output or ""
