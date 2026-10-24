"""唤醒结束后的小结：交给心跳模型压成一句，追加进主脑历史。"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

# 单条工具返回交给小结模型时保留的字数，防止 read_channel 长结果撑爆小结模型输入
TOOL_RESULT_PREVIEW_CHARS = 800


def _content_text(content: Any) -> str:
    """把消息 content（字符串或多段 part 列表）拼成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(getattr(p, "text", ""))
            for p in content
        )
    return ""


def build_wake_material(
    prompt_text: str, tool_calls_result: Any, final_text: str
) -> str:
    """整理本轮唤醒的原因、工具调用与返回、最后的想法，作为小结材料。"""
    lines = [f"醒来原因：{prompt_text.strip()}", "做了什么："]
    rounds = tool_calls_result or []
    if not isinstance(rounds, list):
        rounds = [rounds]
    for tcr in rounds:
        for msg in tcr.to_openai_messages():
            role = msg.get("role")
            if role == "assistant":
                thought = _content_text(msg.get("content")).strip()
                if thought:
                    lines.append(f"- 写了：{thought}")
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function", {}) if isinstance(call, dict) else {}
                    lines.append(f"- 调用 {fn.get('name', '')}({fn.get('arguments', '')})")
            elif role == "tool":
                result = _content_text(msg.get("content")).strip()
                lines.append(f"  返回：{result[:TOOL_RESULT_PREVIEW_CHARS]}")
    if final_text.strip():
        lines.append(f"最后写了：{final_text.strip()}")
    return "\n".join(lines)


def build_wake_summary_prompt(material: str, max_chars: int) -> str:
    """构造唤醒小结提示词。"""
    return "\n".join(
        [
            "以下是本次自主唤醒的操作记录：调用的工具及返回结果。",
            f"以「我」为主语写一句小结，{max_chars} 字以内：查看了哪些会话、关键信息、向谁发送了什么、未完成事项。",
            "仅依据工具返回结果撰写：发送失败不得记为已发送，未获取的信息不得臆造，无新消息时如实说明。",
            "",
            "=== 记录开始 ===",
            material,
            "=== 记录结束 ===",
            "",
            "只输出小结。",
        ]
    )


def parse_wake_summary(raw: str, max_chars: int) -> str | None:
    """清理模型输出并裁剪到 max_chars 字以内。"""
    cleaned = re.sub(r"\s+", " ", str(raw or "")).strip().strip('"\'`“”‘’')
    return cleaned[:max_chars] if cleaned else None


async def summarize_wake(
    provider: Any, material: str, *, max_chars: int, timeout_sec: int
) -> str | None:
    """调用指定模型生成唤醒小结；超时或异常向上抛出，由调用方记日志。"""
    resp = await asyncio.wait_for(
        provider.text_chat(
            prompt=build_wake_summary_prompt(material, max_chars),
            session_id=f"irmia_wake_{uuid.uuid4().hex[:8]}",
            persist=False,
        ),
        timeout=timeout_sec,
    )
    return parse_wake_summary(getattr(resp, "completion_text", "") or "", max_chars)
