"""按会话与消息编号查看留底图。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .config import IrmiaConfig
from .store import IrmiaStore, parse_umo
from .vision import VisionStore
from .vision_describe import ANY_IMAGE_RE, Captioner

try:
    from astrbot.api import FunctionTool
except Exception:  # pragma: no cover - 沿用旧工具的无 AstrBot 单测基类兜底

    class FunctionTool:  # type: ignore[no-redef]
        pass


READ_IMAGE_DESCRIPTION = (
    "看聊天记录里某条消息的图。传 sid 和 read_channel 行首的 #编号。"
    "不带 question 就给你详细描述（包括图里的字），带了 question 就只回答这个问题。"
    "只能看最近 {keep_days} 天的图。硬盘上的图片文件不归这个工具管。"
)


@pydantic_dataclass
class ReadImageTool(FunctionTool):
    """AstrBot 标准 dataclass 形态的 read_image 工具。"""

    name: str = "read_image"
    description: str = READ_IMAGE_DESCRIPTION.format(
        keep_days=IrmiaConfig.image_keep_days
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "sid": {
                    "type": "string",
                    "description": "会话的 sid，跟 read_channel 用的一样。",
                },
                "msg_id": {
                    "type": "integer",
                    "description": "read_channel 行首 # 后面的消息编号。",
                },
                "index": {
                    "type": "integer",
                    "description": "可选。一条消息有好几张图时看第几张，从 1 开始数，默认第 1 张。",
                    "default": 1,
                },
                "question": {
                    "type": "string",
                    "description": "可选。想问图里的什么。不填就给详细描述。",
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
        return "sid 写得不对，从 read_channel 的结果或会话清单里把完整的 sid 抄过来。"
    row = await asyncio.to_thread(store.get_message, sid, msg_id)
    if row is None:
        return f"这个会话里没有 #{msg_id} 这条消息。"
    count = len(ANY_IMAGE_RE.findall(row.text))
    if count == 0:
        return "这条消息没有图。"
    if index < 1 or index > count:
        return f"这条消息只有 {count} 张图，index 从 1 开始数。"
    images = await asyncio.to_thread(vstore.kept_for, sid, row.platform_message_id)
    kept = next((image for image in images if image.idx == index - 1), None)
    if kept is None or not await asyncio.to_thread(Path(kept.path).is_file):
        return (
            f"这张图没存下来：只保留最近 {keep_days} 天的图，"
            "插件装上之前的图、或者当时没存成功的图也看不了。"
        )
    text = await captioner.describe_for_tool(sid, kept, question.strip())
    return text or "识图的模型都没看成，这张图暂时看不了。"
