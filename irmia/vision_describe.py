"""读群与话题概括共享按需识图，超时后留给后台完成，不改账本文本。"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, Awaitable, Callable, Sequence

from .config import IrmiaConfig
from .store import ChannelMessageRow
from .vision import KeptImage, VisionStore

BRIEF_PROMPT = "用一句话说这张图是什么（{n} 字以内）。图里有文字就把关键文字照抄出来。只说看得见的内容，不要猜。只输出这句话本身。"
DETAIL_PROMPT = "请用自然流畅的中文客观描述这张图：整体场景、主要主体的外观/动作/表情、空间布局、色调氛围；逐字读出图中所有可见文字；严格基于可见内容，不要猜测；最后用一两句话概括图片的主题。总长度控制在 {n} 字以内。"
QA_PROMPT = "针对这张图回答问题：{question}\n只根据图里看得见的内容回答，看不出来就直说看不出来。{n} 字以内。"
ATTEMPTS_PER_PROVIDER = 2
CAPTION_CONCURRENCY = 3
STICKER_RE = re.compile(r"^\[图片\]\s*\[[^\[\]]{1,8}\]$")
BARE_IMAGE_RE = re.compile(r"\[图片\](?!\s*[:：])")
PENDING_TEXT = "还没看清，可用 read_image 细看"


async def describe_once(
    providers: Sequence[Any],
    image_path: str,
    prompt: str,
    timeout_sec: int,
    logger: Any,
) -> tuple[str, str]:
    """单图依次尝试模型链，每个模型最多两次，全部失败返回空结果。"""
    for provider in providers:
        for attempt in range(ATTEMPTS_PER_PROVIDER):
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        image_urls=[image_path],
                        session_id=f"irmia_vision_{uuid.uuid4().hex}",
                        persist=False,
                    ),
                    timeout=timeout_sec,
                )
                text = (resp.completion_text or "").strip()
                if text:
                    return text, provider.meta().id
            except Exception as exc:
                logger.debug(f"[IRMIA] 识图调用失败（第 {attempt + 1} 次）: {exc!r}")
    return "", ""


class Captioner:
    """共用缓存、并发额度和进行中的任务，避免读群与话题概括重复识同一张图。"""

    def __init__(
        self,
        vstore: VisionStore,
        providers_getter: Callable[[str], Awaitable[Sequence[Any]]],
        cfg_getter: Callable[[], IrmiaConfig],
        spawn_bg: Callable[[Awaitable[str]], asyncio.Task[str]],
        logger: Any,
    ) -> None:
        self.vstore = vstore
        self.providers_getter = providers_getter
        self.cfg_getter = cfg_getter
        self.spawn_bg = spawn_bg
        self.logger = logger
        self._semaphore = asyncio.Semaphore(CAPTION_CONCURRENCY)
        self._inflight: dict[tuple[str, str, str], asyncio.Task[str]] = {}

    async def _describe(
        self, umo: str, kept: KeptImage, kind: str, question: str
    ) -> str:
        """在统一并发额度内单图识别，成功后写入对应种类的缓存。"""
        cfg = self.cfg_getter()
        max_chars = (
            cfg.image_brief_max_chars if kind == "brief" else cfg.read_image_max_chars
        )
        template = {"brief": BRIEF_PROMPT, "detail": DETAIL_PROMPT, "qa": QA_PROMPT}[
            kind
        ]
        prompt = template.format(n=max_chars, question=question)
        async with self._semaphore:
            providers = await self.providers_getter(umo)
            text, model_id = await describe_once(
                providers, kept.path, prompt, cfg.vision_timeout_sec, self.logger
            )
            if kind == "brief":
                text = re.sub(r"\s+", " ", text)
            text = text.strip()[:max_chars]
            if text:
                await asyncio.to_thread(
                    self.vstore.put_desc,
                    kept.sha256,
                    kind,
                    question,
                    kept.dhash,
                    model_id,
                    text,
                )
            return text

    def _task_for(
        self, umo: str, kept: KeptImage, kind: str, question: str = ""
    ) -> asyncio.Task[str]:
        """复用同图同种类任务，并从创建起交由 runtime 统一管理生命周期。"""
        key = (kept.sha256, kind, question)
        task = self._inflight.get(key)
        if task is None:
            task = self.spawn_bg(self._describe(umo, kept, kind, question))
            self._inflight[key] = task
            task.add_done_callback(lambda _task: self._inflight.pop(key, None))
        return task

    async def caption_rows(
        self, rows: Sequence[ChannelMessageRow], *, wait_sec: float, max_new: int
    ) -> dict[int, list[str | None]]:
        """只为展示行补描述：空串表示未留底，None 表示有留底但尚无描述。"""
        eligible = [
            row
            for row in rows
            if not row.is_bot
            and row.platform_message_id
            and BARE_IMAGE_RE.search(row.text)
            and not STICKER_RE.fullmatch(row.text.strip())
        ]
        if not eligible:
            return {}
        kept_by_message = await asyncio.to_thread(
            self.vstore.kept_for_many,
            eligible[0].umo,
            [row.platform_message_id for row in eligible],
        )
        captions: dict[int, list[str | None]] = {}
        pending: list[tuple[ChannelMessageRow, KeptImage]] = []
        cfg = self.cfg_getter()
        for row in eligible:
            kept_images = kept_by_message.get(row.platform_message_id, [])
            if not kept_images:
                continue
            slots: list[str | None] = [""] * len(BARE_IMAGE_RE.findall(row.text))
            captions[row.id] = slots
            for kept in kept_images:
                if kept.idx >= len(slots):
                    continue
                hit = await asyncio.to_thread(
                    self.vstore.find_desc, kept.sha256, "brief", "", kept.dhash
                )
                slots[kept.idx] = hit[: cfg.image_brief_max_chars] if hit else None
                if not hit:
                    pending.append((row, kept))
        tasks: dict[asyncio.Task[str], list[tuple[int, int]]] = {}
        pending.sort(key=lambda item: (item[0].id, item[1].idx), reverse=True)
        for row, kept in pending[:max_new]:
            task = self._task_for(row.umo, kept, "brief")
            tasks.setdefault(task, []).append((row.id, kept.idx))
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=wait_sec)
            for task in done:
                text = task.result()
                for row_id, idx in tasks[task]:
                    captions[row_id][idx] = text or None
        return captions

    async def describe_for_tool(self, umo: str, kept: KeptImage, question: str) -> str:
        """详细描述或针对问题回答，仅查原图精确缓存。"""
        kind = "qa" if question else "detail"
        hit = await asyncio.to_thread(
            self.vstore.find_desc, kept.sha256, kind, question
        )
        if hit:
            return hit[: self.cfg_getter().read_image_max_chars]
        return await asyncio.shield(self._task_for(umo, kept, kind, question))


def render_with_captions(text: str, captions: list[str | None] | None) -> str:
    """依次替换裸图片占位符，未留底的槽位保持原样，描述换行压为空格。"""
    if captions is None:
        return text
    slots = iter(captions)

    def replace(match: re.Match[str]) -> str:
        caption = next(slots, "")
        if caption == "":
            return match.group(0)
        clean = (
            re.sub(r"\s+", " ", caption).strip()
            if caption is not None
            else PENDING_TEXT
        )
        return f"[图片: {clean}]"

    return BARE_IMAGE_RE.sub(replace, text)
