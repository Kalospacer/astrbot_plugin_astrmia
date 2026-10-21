"""入站图片当场留底。事件结束后临时文件就没了。"""

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
    if not images:
        return 0

    async def _keep(idx: int, comp: Any) -> bool:
        try:
            src = await comp.convert_to_file_path()
            await asyncio.to_thread(
                vstore.keep_image, umo, platform_message_id, idx, src
            )
            return True
        except Exception as exc:
            logger.debug(
                f"[IRMIA] 图片留底失败 ({umo} #{platform_message_id}/{idx}): {exc!r}"
            )
            return False

    return sum(await asyncio.gather(*(_keep(i, c) for i, c in enumerate(images))))
