"""读群与话题概括共用的识图：提示词取自 AstrBot 核心配置，本插件不再自带一套。

设计要点
--------
- 提示词只有一处来源：AstrBot 的 `provider_settings.image_caption_prompt`，
  和主对话管线里 `<image_caption>` 用的是同一份。别人换了自己的模板，这里跟着换，
  所以本模块绝不解析描述的固定结构（没有 `# Full Text Representation` 这类假设）。
- 模型来源优先用 AstrBot 的 `default_image_caption_provider_id`，没配才退回插件
  自己的 `vision_provider_ids` 与会话默认模型。
- 缓存里存的是完整描述，不做截断；要截多少字由展示侧按 `image_brief_max_chars`
  现裁，这样 read_image 拿到的永远是全文。
- 描述里可能出现的方括号被替换成全角，避免和 `[图片: ...]` 占位符互相吞掉。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from .config import IrmiaConfig
from .store import ChannelMessageRow
from .vision import KeptImage, VisionStore

#: AstrBot 自带默认值，读不到核心配置时兜底
FALLBACK_CAPTION_PROMPT = "Please describe the image using Chinese."
#: 描述缓存种类：完整描述（不带问题的 read_image、展示侧截断前的原文）
KIND_FULL = "caption"
#: 描述缓存种类：带问题看图
KIND_QA = "qa"
ATTEMPTS_PER_PROVIDER = 2
CAPTION_CONCURRENCY = 3
ELLIPSIS = "…"
#: QQ 小表情：一张图紧跟方括号表情名（如 `[图片][吓]`）；表情名不能是「图片」，否则连发两图会被误跳过
STICKER_RE = re.compile(r"^\[图片\]\s*\[(?!图片)[^\[\]]{1,8}\]$")
#: 账本里还没写描述的占位符
BARE_IMAGE_RE = re.compile(r"\[图片\](?!\s*[:：])")
#: 账本里已经写好描述的占位符（描述内部不含半角方括号）
DESCRIBED_IMAGE_RE = re.compile(r"\[图片[:：]([^\]]*)\]")
#: 任意图片占位符，用于数一条消息里有几张图
ANY_IMAGE_RE = re.compile(r"\[图片(?:[:：][^\]]*)?\]")
PENDING_TEXT = "还没看清，可以用 read_image 细看"


def _core_provider_settings(context: Any, umo: str | None) -> dict[str, Any]:
    """取 AstrBot 配置里的 provider_settings，取不到给空字典。"""
    try:
        cfg = context.get_config(umo) if umo else context.get_config()
    except Exception:
        return {}
    try:
        settings = cfg.get("provider_settings", None)
    except Exception:
        return {}
    return settings if isinstance(settings, dict) else {}


def read_core_caption_prompt(context: Any, umo: str | None = None) -> str:
    """读 AstrBot 主对话管线的识图提示词，读不到就用官方默认那句。"""
    prompt = str(
        _core_provider_settings(context, umo).get("image_caption_prompt") or ""
    ).strip()
    return prompt or FALLBACK_CAPTION_PROMPT


def read_core_caption_provider_id(context: Any, umo: str | None = None) -> str:
    """读 AstrBot 指定的识图模型 id，没配返回空串。"""
    return str(
        _core_provider_settings(context, umo).get("default_image_caption_provider_id")
        or ""
    ).strip()


def build_qa_prompt(base_prompt: str, question: str) -> str:
    """在核心识图提示词后面追加提问，保持同一套描述口径。"""
    return (
        f"{base_prompt}\n\n"
        f"然后只回答这个问题：{question}\n"
        "只按图里看得见的回答，看不出来就直说看不出来。"
    )


def sanitize_caption_text(text: str) -> str:
    """把方括号换全角、空白压成单空格，避免撑坏 `[图片: ...]` 占位符。"""
    clean = str(text or "").replace("[", "［").replace("]", "］")
    return re.sub(r"\s+", " ", clean).strip()


def truncate_caption(text: str, max_chars: int) -> str:
    """按字数裁描述，超长补省略号；max_chars<=0 表示不裁。"""
    clean = str(text or "")
    if max_chars <= 0 or len(clean) <= max_chars:
        return clean
    if max_chars == 1:
        return ELLIPSIS
    return clean[: max_chars - 1].rstrip() + ELLIPSIS


def extract_captions_from_text(text: str) -> list[str]:
    """从账本文本里取出已写好的图片描述，按出现顺序。"""
    return [m.group(1).strip() for m in DESCRIBED_IMAGE_RE.finditer(str(text or ""))]


def captions_for(
    row: ChannelMessageRow, captions: dict[int, list[str | None]]
) -> list[str | None] | None:
    """取某行的图片描述槽位。

    `caption_rows` 只给真实的外部带图消息建 key；`fetch_messages` 会插入
    「账本还没记上」的自身发言伪行（id 借用相邻消息），这里必须挡住，
    否则她会拿旁边那条消息的图片描述当自己的。
    """
    if row.is_bot or not row.platform_message_id:
        return None
    return captions.get(row.id)


async def describe_once(
    provider: Any,
    image_path: str,
    prompt: str,
    timeout_sec: int,
    logger: Any,
) -> tuple[str, str]:
    """单图调一次识图模型；失败返回空结果。"""
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
    """缓存与在飞任务管理；提示词与模型都跟 AstrBot 核心管线对齐。"""

    def __init__(
        self,
        vstore: VisionStore,
        context: Any,
        cfg_getter: Callable[[], IrmiaConfig],
        spawn_bg: Callable[[Awaitable[str]], asyncio.Task[str]],
        logger: Any,
    ) -> None:
        self.vstore = vstore
        self.context = context
        self.cfg_getter = cfg_getter
        self.spawn_bg = spawn_bg
        self.logger = logger
        self._semaphore = asyncio.Semaphore(CAPTION_CONCURRENCY)
        self._inflight: dict[tuple[str, str, str], asyncio.Task[str]] = {}

    async def _providers(self, umo: str) -> AsyncIterator[Any]:
        """按核心识图模型、插件降级链、会话默认模型的顺序给出去重候选。"""
        cfg = self.cfg_getter()
        core_id = read_core_caption_provider_id(self.context, umo)
        seen: set[int] = set()
        for provider_id in dict.fromkeys([core_id, *(cfg.vision_provider_ids or [])]):
            if not provider_id:
                continue
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                continue
            if provider is not None and id(provider) not in seen:
                seen.add(id(provider))
                yield provider

        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            try:
                provider = await self.context.get_using_provider_async()
            except Exception:
                return
        if provider is not None and id(provider) not in seen:
            yield provider

    async def _describe(self, umo: str, kept: KeptImage, question: str) -> str:
        """在统一并发额度内依次尝试识图模型，成功后写入缓存。"""
        cfg = self.cfg_getter()
        base_prompt = read_core_caption_prompt(self.context, umo)
        prompt = build_qa_prompt(base_prompt, question) if question else base_prompt
        kind = KIND_QA if question else KIND_FULL
        async with self._semaphore:
            async for provider in self._providers(umo):
                text, model_id = await describe_once(
                    provider, kept.path, prompt, cfg.vision_timeout_sec, self.logger
                )
                text = sanitize_caption_text(text)
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
        return ""

    def _task_for(
        self, umo: str, kept: KeptImage, question: str = ""
    ) -> asyncio.Task[str]:
        """复用同图同问题任务，并从创建起交由 runtime 统一管理生命周期。"""
        kind = KIND_QA if question else KIND_FULL
        key = (kept.sha256, kind, question)
        task = self._inflight.get(key)
        if task is None:
            task = self.spawn_bg(self._describe(umo, kept, question))
            self._inflight[key] = task
            task.add_done_callback(lambda _task: self._inflight.pop(key, None))
        return task

    async def caption_rows(
        self, rows: Sequence[ChannelMessageRow], *, wait_sec: float, max_new: int
    ) -> dict[int, list[str | None]]:
        """只为还没写描述的裸占位符补描述：空串表示未留底，None 表示有留底但还没描述。"""
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
                    self.vstore.find_desc, kept.sha256, KIND_FULL, "", kept.dhash
                )
                slots[kept.idx] = hit or None
                if not hit:
                    pending.append((row, kept))
        tasks: dict[asyncio.Task[str], list[tuple[int, int]]] = {}
        pending.sort(key=lambda item: (item[0].id, item[1].idx), reverse=True)
        for row, kept in pending[:max_new]:
            task = self._task_for(row.umo, kept)
            tasks.setdefault(task, []).append((row.id, kept.idx))
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=wait_sec)
            for task in done:
                text = task.result()
                for row_id, idx in tasks[task]:
                    captions[row_id][idx] = text or None
        return captions

    async def describe_for_tool(self, umo: str, kept: KeptImage, question: str) -> str:
        """不带问题给完整描述，带问题针对问题回答；命中缓存直接返回。

        这里只要完整描述，不接受旧的一句话简述凑数：read_image 存在的意义就是把
        细节看全。缓存里都没有才去识一次。
        """
        cfg = self.cfg_getter()
        kind = KIND_QA if question else KIND_FULL
        hit = await asyncio.to_thread(
            self.vstore.find_desc,
            kept.sha256,
            kind,
            question,
            kept.dhash,
            False,
        )
        if hit:
            return truncate_caption(hit, cfg.read_image_max_chars) if question else hit
        text = await asyncio.shield(self._task_for(umo, kept, question))
        return truncate_caption(text, cfg.read_image_max_chars) if question else text


def render_with_captions(
    text: str,
    captions: list[str | None] | None,
    max_chars: int = 0,
) -> str:
    """替换裸图片占位符并把描述裁到 max_chars；为空时保留原占位符。"""
    rendered = str(text or "")
    if captions is not None:
        slots = iter(captions)

        def replace(match: re.Match[str]) -> str:
            caption = next(slots, "")
            if caption == "":
                return match.group(0)
            clean = (
                sanitize_caption_text(caption)
                if caption is not None
                else PENDING_TEXT
            )
            return f"[图片: {truncate_caption(clean, max_chars)}]"

        rendered = BARE_IMAGE_RE.sub(replace, rendered)

    def shrink(match: re.Match[str]) -> str:
        clean = re.sub(r"\s+", " ", match.group(1)).strip()
        return f"[图片: {truncate_caption(clean, max_chars)}]"

    return DESCRIBED_IMAGE_RE.sub(shrink, rendered)
