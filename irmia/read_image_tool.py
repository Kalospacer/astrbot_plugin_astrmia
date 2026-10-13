"""按会话与消息编号查看留底图，不要求模型寻找临时文件路径。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .store import IrmiaStore, parse_umo
from .vision import VisionStore
from .vision_describe import Captioner

try:
    from astrbot.api import FunctionTool
except Exception:  # pragma: no cover - 沿用旧工具的无 AstrBot 单测基类兜底

    class FunctionTool:  # type: ignore[no-redef]
        pass


READ_IMAGE_DESCRIPTION = (
    "看聊天记录里某条消息的图片。传 sid 和 read_channel 行首的 #编号；"
    "不带 question 给详细描述（含图里的文字），带 question 就针对问题回答。"
    "只能看最近 {keep_days} 天的图。硬盘上的图片文件不归这个工具，用 vision_read。"
)


@pydantic_dataclass
class ReadImageTool(FunctionTool):
    """AstrBot 标准 dataclass 形态的 read_image 工具。"""

    name: str = "read_image"
    description: str = READ_IMAGE_DESCRIPTION.format(keep_days=3)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "sid": {
                    "type": "string",
                    "description": "会话 sid，和 read_channel 相同。",
                },
                "msg_id": {
                    "type": "integer",
                    "description": "read_channel 行首 # 后面的消息编号。",
                },
                "index": {
                    "type": "integer",
                    "description": "可选。一条消息里看第几张图，从 1 开始，默认 1。",
                    "default": 1,
                },
                "question": {
                    "type": "string",
                    "description": "可选。想问图里的什么；不填则给详细描述。",
                },
            },
            "required": ["sid", "msg_id"],
        }
    )
    handler_fn: Callable[..., Any] | None = Field(default=None, repr=False)

    async def call(self, context: Any, **kwargs: Any) -> str:
        """执行按消息编号看图的工具调用。"""
        if self.handler_fn is None:
            return "read_image 尚未绑定运行时宿主。"
        return await self.handler_fn(context, **kwargs)


async def execute_read_image(
    *,
    store: IrmiaStore,
    vstore: VisionStore,
    captioner: Captioner,
    sid: str,
    msg_id: int,
    index: int = 1,
    question: str = "",
    keep_days: int,
) -> str:
    """按同一账本来源定位消息，查看指定的留底图或返回可读原因。"""
    if not all(parse_umo(sid)):
        return "sid 格式不对，请从 read_channel 或会话清单里抄完整的 sid。"
    row = await asyncio.to_thread(store.get_message, sid, msg_id)
    if row is None:
        return f"这个会话里没有 #{msg_id} 这条消息。"
    count = len(re.findall(r"\[图片(?:[:：][^\]]*)?\]", row.text))
    if count == 0:
        return "这条消息没有图。"
    if index < 1 or index > count:
        return f"这条消息只有 {count} 张图，index 从 1 开始。"
    images = await asyncio.to_thread(vstore.kept_for, sid, row.platform_message_id)
    kept = next((image for image in images if image.idx == index - 1), None)
    if kept is None or not await asyncio.to_thread(Path(kept.path).is_file):
        return (
            f"这张图没有留底了：只保留最近 {keep_days} 天的图，"
            "上线前的图或留底失败的图也无法查看。"
        )
    text = await captioner.describe_for_tool(sid, kept, question.strip())
    return text or "识图模型都失败了，这张图还没看清。"
