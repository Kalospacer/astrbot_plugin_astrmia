"""入站图片留底必须当场 await，不能等事件结束后再找临时文件。"""

from __future__ import annotations

import asyncio
from typing import Any

from .vision import VisionStore


async def keep_event_images(
    event: Any, umo: str, platform_message_id: str, vstore: VisionStore, logger: Any
) -> int:
    """缩小保存顶层图片；单张失败不影响其余图片和消息处理。"""
    if not platform_message_id:
        return 0
    images = [c for c in event.message_obj.message if type(c).__name__ == "Image"]
    kept = 0
    for idx, comp in enumerate(images):
        try:
            src = await comp.convert_to_file_path()
            await asyncio.to_thread(
                vstore.keep_image, umo, platform_message_id, idx, src
            )
            kept += 1
        except Exception as exc:
            logger.debug(
                f"[IRMIA] 图片留底失败 ({umo} #{platform_message_id}/{idx}): {exc!r}"
            )
    return kept
