"""look_image 工具：给主脑一个主动看图的出口。

为什么要有它
------------
账本里只存 `[图片]` 占位符，话题概括那一步只覆盖「最近 25 条窗口」里的图。
主脑 read_channel 翻历史时，可能看到更早的、或者窗口外的图片引用 —— 这时候它需要
一个能直接说「帮我看这张图」的工具，而不是干瞪眼。

这个工具直接把 `astrbot_plugin_irmia_vision` 插件的核心能力收进 IRMIA，
不再依赖外部插件：

- 传本地路径或账本消息 id（二选一），直接看
- 命中 vision_cache 直接返回缓存描述，不重复烧 token
- 未命中才调 VL 模型，结果写回缓存
- 只支持图片格式（png/jpg/jpeg/webp/gif/bmp），不认路径直接给话

与话题概括共用同一份缓存的同一套 dHash 判重，两处不会各写各的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .vision import (
    VisionCache,
    describe_and_cache,
    is_image_path,
    sha256_of_file,
    compute_dhash,
)

try:
    from astrbot.api import FunctionTool
except Exception:  # pragma: no cover - 单测无 AstrBot 环境时的轻量基类兜底

    class FunctionTool:  # type: ignore[no-redef]
        pass


LOOK_IMAGE_DESCRIPTION = (
    "看一张图片，返回里面的内容描述（包含能看清的文字）。"
    "图片路径从账本消息的 media 列拿，或者在 read_channel 的结果里找。"
    "只认本地图片文件；同一张图有缓存就直接给，不重复调用模型。"
)


@pydantic_dataclass
class LookImageTool(FunctionTool):
    """AstrBot 标准 dataclass 形态的 look_image 工具。"""

    name: str = "look_image"
    description: str = LOOK_IMAGE_DESCRIPTION
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "图片的本地路径，形如 /AstrBot/data/temp/media_image_xxx.jpg。"
                        "一次只传一张。"
                    ),
                },
                "question": {
                    "type": "string",
                    "description": "可选。想问这张图的具体问题，比如「图里的报错是什么」。",
                },
            },
            "required": ["path"],
        }
    )
    handler_fn: Callable[..., Any] | None = Field(default=None, repr=False)

    async def call(self, context: Any, **kwargs: Any) -> str:
        """执行 look_image 工具调用。"""
        if self.handler_fn is None:
            return "error: look_image 尚未绑定运行时宿主。"
        return await self.handler_fn(context, **kwargs)


async def describe_single_image(
    cache: VisionCache,
    provider: Any,
    path: str,
    *,
    question: str = "",
    model_id: str = "",
    ttl_days: int = 7,
    timeout_sec: int = 60,
    max_chars: int = 300,
    logger: Any = None,
) -> str:
    """看单张图并返回描述文本（缓存优先，未命中才调模型并落库）。

    Args:
        cache: IRMIA 内置的识图缓存。
        provider: 具备 image 模态的 Provider。
        path: 图片本地路径。
        question: 想针对图片问的问题，留空用通用描述提示。
        model_id: 写缓存时记的模型名。
        ttl_days: 缓存过期天数，<=0 表示不过期。
        timeout_sec: 单次 VL 调用超时。
        max_chars: 描述字数上限。
        logger: 插件 logger，可选。

    Returns:
        人类可读的结果文本；出错时返回带原因的说明，不抛异常。
    """
    target = str(path or "").strip()
    if not target:
        return "没给图片路径哦，传 path 参数，形如 /AstrBot/data/temp/media_image_xxx.jpg。"

    if not await _to_thread(is_image_path, target):
        return (
            f"读不了这个路径：{target}\n"
            "要么文件不存在，要么后缀不是支持的图片格式"
            "（png/jpg/jpeg/webp/gif/bmp）。"
        )

    if provider is None:
        return "现在没有可用的识图模型，检查一下轻量模型（light_provider_id）的配置。"

    try:
        await _to_thread(cache.open)
    except Exception as exc:  # noqa: BLE001 - 缓存打不开也要能看图
        if logger is not None:
            logger.debug(f"[IRMIA] look_image 打开缓存失败: {exc!r}")

    sha = await _to_thread(sha256_of_file, target)
    dhash = await _to_thread(compute_dhash, target)
    cached = await _to_thread(
        cache.find,
        path=target,
        sha256=sha,
        filename=target.rsplit("/", 1)[-1],
        dhash=dhash,
        ttl_days=ttl_days,
    )
    if cached is not None:
        text = (cached.summary or cached.text).strip()
        if text:
            return f"{text}\n\n（来自缓存 result_id={cached.result_id}）"

    try:
        captions = await describe_and_cache(
            cache,
            provider,
            [target],
            model_id=model_id or "unknown",
            ttl_days=ttl_days,
            max_chars=max_chars,
            timeout_sec=timeout_sec,
            logger=logger,
            question=question,
        )
    except Exception as exc:  # noqa: BLE001 - 工具不给主脑抛异常
        return f"看图失败：{exc!r}"

    text = str(captions[0] if captions else "").strip()
    if not text:
        return "模型没返回内容，这张图可能读不出来，或者模型不支持看图。"
    return text


async def _to_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """把同步文件/数据库操作丢到线程池，别卡住事件循环。"""
    import asyncio

    return await asyncio.to_thread(fn, *args)
