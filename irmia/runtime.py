"""IRMIA 插件运行时协调器：串联全局主脑、信箱分流、话题概括、read_channel、呼吸心跳与钩子。"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import IrmiaConfig, is_in_quiet_window, load_irmia_config
from .heartbeat import HeartbeatEngine, HeartbeatFiring
from .inbox import GroupBatcher, evaluate_channel_message
from .mention import build_mention_notice
from .read_channel_tool import ReadChannelTool, execute_read_channel
from .read_image_tool import READ_IMAGE_DESCRIPTION, ReadImageTool, execute_read_image
from .render import (
    SELF_BRIEF_HEADER,
    build_self_brief,
    format_clock_line,
    format_short_time,
    render_heartbeat_prompt,
    render_session_list,
    resolve_tz,
)
from .store import IrmiaStore, parse_umo
from .topic import TopicSummarizer
from .vision import VisionStore
from .vision_describe import Captioner
from .vision_keep import keep_event_images
from .wake_summary import build_wake_material, summarize_wake

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 单测无 pydantic_core/AstrBot 环境时的轻量兜底

    @dataclass
    class TextPart:  # type: ignore[no-redef]
        text: str
        type: str = "text"
        _no_save: bool = False

        def mark_as_temp(self) -> "TextPart":
            self._no_save = True
            return self


try:
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.star.star_tools import StarTools
except Exception:  # pragma: no cover - 单元测试环境兼容
    CronMessageEvent = object  # type: ignore[assignment,misc]
    MessageSession = None  # type: ignore[assignment]
    StarTools = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_astrmia"
SEND_OK_PREFIX = "Message sent to session"

# AstrBot 本地运行时在工具提示后追加的工作目录说明；路径随源会话变化，挪到用户消息尾部以稳定 system 前缀
WORKSPACE_NOTE_RE = re.compile(
    r"\nCurrent workspace: `[^`]*`\. .*?do not assume this behavior for other tools\.\n",
    re.S,
)


def _parse_conv_history(conv: Any) -> list[Any]:
    """统一反序列化 Conversation.history 字段（兼容 JSON 字符串与列表）。"""
    raw_hist = getattr(conv, "history", "[]")
    if isinstance(raw_hist, list):
        return list(raw_hist)
    if isinstance(raw_hist, str) and raw_hist.strip():
        try:
            parsed = json.loads(raw_hist)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


class HeartbeatWakeEvent(CronMessageEvent):  # type: ignore[misc]
    """呼吸心跳与手动静默唤醒专用事件：重写 send/send_streaming 为静默，只有调用工具才对外发消息。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.role = "admin"

    async def send(self, message: Any) -> None:
        """静默丢弃普通直出发送，避免心跳自言自语直接发到聊天窗口。"""
        return

    async def send_streaming(self, generator: Any, use_fallback: bool = False) -> None:
        """静默消费流式生成器。"""
        try:
            async for _ in generator:
                pass
        except Exception:
            pass


def is_internal_silent_event(event: Any) -> bool:
    """判断是否为 AstrHe 夜间复盘静默事件或其它内部事件（本插件对此类事件完全让路）。"""
    if event is None:
        return True
    if event.__class__.__name__ == "SilentNightEvent":
        return True
    try:
        return str(event.get_platform_name() or "") == "astrhe"
    except Exception:
        return False


def extract_event_text_and_at(
    event: Any, quote_preview_chars: int = 40
) -> tuple[str, bool, str]:
    """从 AstrMessageEvent 中提取带 [@qq] 标记的文本、是否平台 @/引用了 Bot、以及 Bot 自身 ID。"""
    self_id = ""
    try:
        self_id = str(event.get_self_id() or "").strip()
    except Exception:
        pass

    is_at_bot = bool(getattr(event, "is_at_or_wake_command", False))
    parts: list[str] = []
    msg_obj = getattr(event, "message_obj", None)
    chain = getattr(msg_obj, "message", None) or []

    for comp in chain:
        comp_cls = comp.__class__.__name__
        if comp_cls == "At":
            qq = str(getattr(comp, "qq", "") or "").strip()
            if qq:
                parts.append(f"[@{qq}]")
                if self_id and qq == self_id:
                    is_at_bot = True
        elif comp_cls == "AtAll":
            parts.append("[@all]")
        elif comp_cls == "Reply":
            reply_sender = str(getattr(comp, "sender_id", "") or "").strip()
            if self_id and reply_sender == self_id:
                is_at_bot = True
            reply_text = str(getattr(comp, "message_str", "") or "").strip()
            if reply_text:
                parts.append(f"[引用: {reply_text[:quote_preview_chars]}]")
        elif comp_cls == "Plain":
            txt = str(getattr(comp, "text", "") or "")
            if txt:
                parts.append(txt)
        elif comp_cls == "Image":
            parts.append("[图片]")
        elif comp_cls == "Record":
            parts.append("[语音]")
        elif comp_cls == "Video":
            parts.append("[视频]")
        elif comp_cls == "File":
            parts.append("[文件]")

    joined = " ".join(p.strip() for p in parts if p.strip()).strip()
    if not joined:
        joined = str(getattr(event, "message_str", "") or "").strip()
    return joined, is_at_bot, self_id


class IrmiaRuntime:
    """协调全局主脑、信箱分流、话题概括、read_channel 工具与呼吸心跳。"""

    def __init__(
        self,
        context: Any,
        config: dict[str, Any] | None,
        logger: Any,
        data_dir: Path | None = None,
        astrhe_db_path: Path | None = None,
    ) -> None:
        self.context = context
        self._raw_config = config if config is not None else {}
        self.logger = logger

        resolved_dir = (
            Path(data_dir)
            if data_dir is not None
            else (
                Path(StarTools.get_data_dir(PLUGIN_NAME))
                if StarTools is not None
                else Path("data") / "plugin_data" / PLUGIN_NAME
            )
        )
        self.store = IrmiaStore(resolved_dir, astrhe_db_path=astrhe_db_path)
        self.batcher = GroupBatcher()
        self.topic_summarizer = TopicSummarizer(self.store, context, logger)
        self.vision_store = VisionStore(
            self.store.data_dir / "vision.db", self.store.data_dir / "kept_images"
        )
        self.captioner = Captioner(
            self.vision_store, self._vision_providers, self.cfg, self._spawn_bg, logger
        )
        self.topic_summarizer.captioner = self.captioner

        cfg = load_irmia_config(self._raw_config)
        self.heartbeat = HeartbeatEngine(
            floor_ms=cfg.heartbeat_floor_min * 60_000,
            ceil_ms=cfg.heartbeat_ceil_min * 60_000,
            tick_ms=cfg.heartbeat_tick_sec * 1000,
            target_mean_ms=cfg.heartbeat_target_mean_min * 60_000,
        )

        self._bot_self_ids: set[str] = set()
        self._auto_master_umo: str | None = None
        # 主脑锁：聊天从读历史持有到任务结束；唤醒思考不持锁，仅小结重读与写回时排队
        self._brain_lock = asyncio.Lock()
        self._brain_owner: asyncio.Task[Any] | None = None
        self._wake_running = False
        self._last_active_event: Any = None
        self._current_chat_event: contextvars.ContextVar[Any] = (
            contextvars.ContextVar("irmia_current_chat_event", default=None)
        )
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._tools_registered = False
        # 夜间静默窗口状态：记住上一拍是否在窗口内，用来在出窗口时把心率计时归零
        self._was_in_quiet = False
        # 留底和描述缓存每小时清理一次，不依赖心跳唤醒开关。
        self._vision_cleanup_at = 0.0
        self._install_send_message_patch()

    def cfg(self) -> IrmiaConfig:
        """动态读取当前最新配置并按需同步心跳参数。"""
        c = load_irmia_config(self._raw_config)
        self.heartbeat.reconfigure(
            floor_min=c.heartbeat_floor_min,
            ceil_min=c.heartbeat_ceil_min,
            target_mean_min=c.heartbeat_target_mean_min,
            tick_sec=c.heartbeat_tick_sec,
        )
        return c

    def tz_name(self) -> str:
        """读取 AstrBot 配置的时区，缺省为 Asia/Shanghai。"""
        try:
            tz = str(self.context.get_config().get("timezone") or "").strip()
            return tz or "Asia/Shanghai"
        except Exception:
            return "Asia/Shanghai"

    def admin_ids(self) -> set[str]:
        """读取 AstrBot 配置的管理员 ID 集合。"""
        try:
            raw = self.context.get_config().get("admins_id", []) or []
            return {str(x).strip() for x in raw if str(x).strip()}
        except Exception:
            return set()

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        """启动存储、注册工具、清理过期识图缓存并开启呼吸心跳循环。"""
        self._install_send_message_patch()
        await self.store.open()
        self._auto_master_umo = (
            await self.store.get_kv("auto_master_umo", "")
        ).strip()
        await asyncio.to_thread(self.vision_store.open)
        await self._maybe_cleanup_vision(self.cfg())
        self._register_tools()
        self._start_heartbeat_loop()
        dist = self.heartbeat.distribution()
        self.logger.info(
            f"[IRMIA] 已启动：心跳区间 {dist.floor_ms / 60000:.0f}~{dist.ceil_ms / 60000:.0f}m, "
            f"目标均值 {dist.mean_ms / 60000:.2f}m (alpha={dist.alpha:.4f})"
        )

    async def _vision_providers(self, umo: str) -> list[Any]:
        """按识图降级链解析模型，未配置时复用轻量模型及会话默认模型。"""
        cfg = self.cfg()
        providers = []
        for provider_id in cfg.vision_provider_ids or [cfg.light_provider_id]:
            provider = await self.topic_summarizer.resolve_provider(umo, provider_id)
            if provider is not None:
                providers.append(provider)
        return providers

    async def terminate(self) -> None:
        """停止心跳循环、等待后台任务收尾并关闭存储。"""
        self._uninstall_send_message_patch()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        await asyncio.to_thread(self.vision_store.close)
        await self.store.close()

    def _spawn_bg(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _register_tools(self) -> None:
        if self._tools_registered:
            return
        add_llm_tools = getattr(self.context, "add_llm_tools", None)
        if not callable(add_llm_tools):
            return
        try:
            add_llm_tools(
                ReadChannelTool(handler_fn=self._handle_read_channel_tool),
                # 群聊消息里的图片用 read_image 看。
                ReadImageTool(
                    handler_fn=self._handle_read_image_tool,
                    description=READ_IMAGE_DESCRIPTION.format(
                        keep_days=self.cfg().image_keep_days
                    ),
                ),
            )
            self._tools_registered = True
        except Exception as exc:
            self.logger.warning(f"[IRMIA] 注册工具失败: {exc}")

    async def _handle_read_image_tool(
        self, run_context: Any, **kwargs: Any
    ) -> str:
        """按会话与聊天行编号看图，不接收临时图片路径。"""
        cfg = self.cfg()
        return await execute_read_image(
            store=self.store,
            vstore=self.vision_store,
            captioner=self.captioner,
            sid=str(kwargs.get("sid") or "").strip(),
            msg_id=int(kwargs["msg_id"]),
            index=int(kwargs.get("index", 1)),
            question=str(kwargs.get("question") or "").strip(),
            keep_days=cfg.image_keep_days,
        )

    async def _handle_read_channel_tool(
        self, run_context: Any, **kwargs: Any
    ) -> str:
        """read_channel 工具回调入口：直接从 run_context 关联的事件读取单轮状态，防止跨会话串扰。"""
        cfg = self.cfg()
        sid = str(kwargs.get("sid") or "").strip()
        before_raw = kwargs.get("before")
        limit_raw = kwargs.get("limit")
        before = int(before_raw) if before_raw is not None else None
        limit = int(limit_raw) if limit_raw is not None else None

        ev = getattr(getattr(run_context, "context", None), "event", None) or self._last_active_event
        called_sid = None
        repeat_reads = None
        if ev is not None and hasattr(ev, "get_extra"):
            # 内嵌通知已读过本轮叫醒群，工具不再兜底重复展示最近消息。
            if not ev.get_extra("irmia_mention_inlined"):
                called_sid = ev.get_extra("irmia_called_sid")
            repeat_reads = ev.get_extra("irmia_repeat_reads")

        return await execute_read_channel(
            store=self.store,
            sid=sid,
            before=before,
            limit=limit,
            contacts_map=cfg.contacts_map,
            mention_keywords=cfg.mention_keywords,
            bot_self_ids=self._bot_self_ids,
            called_sid_this_turn=called_sid,
            tz_name=self.tz_name(),
            repeat_counter=repeat_reads,
            captioner=self.captioner if cfg.read_channel_image_enabled else None,
            caption_wait_sec=cfg.read_channel_image_wait_sec,
            caption_max_new=cfg.read_channel_image_max,
        )

    async def _record_bot_speech(self, umo: str, text: str) -> None:
        """统一记录 Bot 自身发出的消息到兜底账本。"""
        if not umo or not text:
            return
        pid, mtype, sid = parse_umo(umo)
        if not pid or not mtype or not sid:
            return
        self_id = next(iter(self._bot_self_ids), "bot")
        await self.store.ensure_message_recorded(
            umo=umo,
            platform_id=pid,
            message_type=mtype,
            session_id=sid,
            sender_id=self_id,
            text=text,
            ts=time.time(),
            is_bot=True,
        )

    # ---------------- 主脑 UMO 绑定 ----------------

    async def resolve_master_umo(self) -> str:
        """获取全局主脑 UMO（优先配置项 master_umo，其次内存缓存的管理员私聊 UMO）。"""
        cfg = self.cfg()
        if cfg.master_umo:
            return cfg.master_umo
        if self._auto_master_umo is None:
            self._auto_master_umo = (
                await self.store.get_kv("auto_master_umo", "")
            ).strip()
        return self._auto_master_umo

    async def maybe_bind_master_umo(self, event: Any) -> None:
        """当管理员首次私聊时，若未配置 master_umo 则自动记录该私聊 UMO 为主脑屋子。"""
        if await self.resolve_master_umo():
            return
        try:
            if not event.is_private_chat():
                return
            sender_id = str(event.get_sender_id() or "").strip()
            is_admin = (
                getattr(event, "role", "") == "admin"
                or sender_id in self.admin_ids()
            )
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if is_admin and umo:
                self._auto_master_umo = umo
                await self.store.set_kv("auto_master_umo", umo)
                self.logger.info(f"[IRMIA] 已自动绑定全局主脑会话: {umo}")
        except Exception:
            pass

    async def _acquire_brain(self) -> bool:
        """让当前任务取得主脑锁；当前任务已持有时直接复用，返回是否为本次新取得。"""
        task = asyncio.current_task()
        if self._brain_owner is task:
            return False
        await self._brain_lock.acquire()
        self._brain_owner = task
        return True

    def _release_brain(self) -> None:
        self._brain_owner = None
        self._brain_lock.release()

    # ---------------- 钩子 1：入站消息分流与攒批 ----------------

    async def on_incoming_event(self, event: Any) -> None:
        """处理所有平台入站事件：记账兜底、指令放行、私聊直通、群聊未点名进信箱、群聊点名攒批+半遮面通知。"""
        if is_internal_silent_event(event):
            return
        cfg = self.cfg()
        if not cfg.enabled:
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo or umo.startswith(("astrhe:", "cron:", "irmia:")):
            return

        pid, mtype, session_id = parse_umo(umo)
        if not pid or not mtype:
            return

        text, is_platform_at, self_id = extract_event_text_and_at(
            event, cfg.quote_preview_chars
        )
        if self_id:
            self._bot_self_ids.add(self_id)
            msg_obj_at = getattr(event, "message_obj", None)
            for comp in getattr(msg_obj_at, "message", None) or []:
                if comp.__class__.__name__ == "At":
                    comp_qq = str(getattr(comp, "qq", "") or "").strip()
                    comp_name = str(getattr(comp, "name", "") or "").strip().lstrip("@")
                    if comp_qq == self_id and comp_name:
                        self._bot_self_ids.add(comp_name)

        sender_id = ""
        sender_name = ""
        sender_card = ""
        group_name = ""
        try:
            sender_id = str(event.get_sender_id() or "").strip()
            sender_name = str(event.get_sender_name() or "").strip()
            msg_obj = getattr(event, "message_obj", None)
            sender_obj = getattr(msg_obj, "sender", None)
            sender_card = str(
                getattr(sender_obj, "card", "")
                or getattr(sender_obj, "title", "")
                or ""
            ).strip()
            group_obj = getattr(msg_obj, "group", None)
            group_name = str(getattr(group_obj, "group_name", "") or "").strip()
            msg_id = str(getattr(msg_obj, "message_id", "") or "").strip()
        except Exception:
            msg_id = ""

        if self_id and sender_id == self_id:
            return

        is_private = False
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = "friend" in mtype.lower() or "c2c" in mtype.lower()

        now_ts = time.time()
        await self.store.ensure_message_recorded(
            umo=umo,
            platform_id=pid,
            message_type=mtype,
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_card=sender_card,
            text=text,
            ts=now_ts,
            platform_message_id=msg_id or None,
            is_bot=False,
            group_name=group_name,
        )

        if cfg.image_keep_days > 0 and msg_id:
            await keep_event_images(event, umo, msg_id, self.vision_store, self.logger)

        # 若命中了已注册的插件指令（handlers_parsed_params 非空），直接放行给命令处理器
        raw_str = str(getattr(event, "message_str", "") or "").strip()
        if event.get_extra("handlers_parsed_params"):
            return

        decision = evaluate_channel_message(
            is_private=is_private,
            is_platform_at=is_platform_at,
            text=raw_str or text,
            mention_keywords=cfg.mention_keywords,
        )

        if decision.is_private:
            self.heartbeat.note_activity()
            await self.maybe_bind_master_umo(event)
            await self.store.mark_session_all_read(umo)
            if cfg.intercept_direct_reply:
                event.set_extra("enable_streaming", False)
            return

        # 群聊未点名 -> 拦截进信箱，不唤醒主脑，按条件后台概括话题
        if not decision.should_wake:
            event.stop_event()
            self._spawn_bg(
                self.topic_summarizer.maybe_summarize(
                    umo, cfg, force_for_mention=False
                )
            )
            return

        # 群聊已点名 -> 归零心跳计时，执行防大喘气攒批等待
        self.heartbeat.note_activity()
        is_leader = await self.batcher.wait_my_turn(
            umo, float(cfg.group_batch_window_sec)
        )
        if not is_leader:
            event.stop_event()
            return

        event.is_wake = True
        event.is_at_or_wake_command = True
        event.set_extra("enable_streaming", False)

        st = await self.store.get_session_state(umo)

        # 不在这里等话题概括：先保存上一次缓存的话题，概括扔后台跑，跑完给下次用。
        # 完整通知卡等聊天回合取得主脑锁后再生成，避免排队期间的已读水位变化。
        cached_topic = str(st.get("topic", "") or "")
        self._spawn_bg(
            self.topic_summarizer.maybe_summarize(umo, cfg, force_for_mention=True)
        )

        where_name = self.store.resolve_session_name(
            umo,
            group_name or str(st.get("group_name", "") or ""),
            sender_id,
            sender_name,
            cfg.contacts_map,
        )
        caller_name = self.store.resolve_person_name(
            sender_id, sender_name, sender_card, cfg.contacts_map
        )
        at_label = format_short_time(
            now_ts, self.tz_name(), always_hm_only=True
        )

        event.set_extra(
            "irmia_mention_meta",
            {
                "where_name": where_name,
                "caller_name": caller_name,
                "is_at": decision.is_platform_at,
                "at_label": at_label,
                "topic": cached_topic,
            },
        )
        event.set_extra("irmia_called_sid", umo)

    # ---------------- 钩子 2：等待 LLM 请求（主脑排队） ----------------

    async def on_waiting_llm_request(self, event: Any) -> None:
        """聊天在核心读历史前取得主脑锁，持有到保存历史后的任务结束；唤醒只在小结写回时排队。"""
        if is_internal_silent_event(event) or not self.cfg().enabled:
            return
        self._current_chat_event.set(event)
        task = asyncio.current_task()
        if task is not None:
            task.add_done_callback(
                lambda _t, ev=event: self._flush_unconsumed_buffered_chain(ev)
            )
        if not await self.resolve_master_umo():
            return
        if await self._acquire_brain():
            asyncio.current_task().add_done_callback(
                lambda _t: self._release_brain()
            )

    # ---------------- 钩子 3：LLM 请求前（主脑历史切换 + 半遮面替换 + 此刻层注入） ----------------

    async def on_llm_request(self, event: Any, req: Any) -> None:
        """在 LLM 请求发起前绑定全局主脑历史、替换群点名半遮面通知、并注入此刻层信箱清单。"""
        if is_internal_silent_event(event):
            return
        cfg = self.cfg()
        if not cfg.enabled:
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if hasattr(event, "set_extra"):
            event.set_extra("irmia_repeat_reads", {})
            event.set_extra("irmia_final_llm_resp", False)
            event.set_extra("irmia_in_decorating", False)
        is_wake = bool(event.get_extra("irmia_wake")) if hasattr(event, "get_extra") else False
        if not is_wake:
            self._last_active_event = event
        self._current_chat_event.set(event)

        # 1. 全局唯一主脑历史绑定（聊天排队后重读最新存储；内部唤醒自行绑定旧历史供思考，不在此覆盖）
        master_umo = await self.resolve_master_umo()
        if master_umo and not is_wake:
            await self._bind_master_conversation(master_umo, req)

        # 2. 持聊天回合的主脑锁后生成点名卡片，同时清理原消息的引用/图片/音频块
        mention_meta = (
            event.get_extra("irmia_mention_meta")
            if hasattr(event, "get_extra")
            else None
        )
        if isinstance(mention_meta, dict):
            mention_notice = await build_mention_notice(
                store=self.store,
                sid=event.get_extra("irmia_called_sid"),
                meta=mention_meta,
                cfg=cfg,
                bot_self_ids=self._bot_self_ids,
                tz_name=self.tz_name(),
                captioner=self.captioner,
            )
            event.set_extra("irmia_mention_notice", mention_notice)
            event.set_extra("irmia_mention_inlined", cfg.mention_inline_enabled)
            req.prompt = mention_notice
            if isinstance(getattr(req, "image_urls", None), list):
                req.image_urls.clear()
            if isinstance(getattr(req, "audio_urls", None), list):
                req.audio_urls.clear()
            if isinstance(getattr(req, "extra_user_content_parts", None), list):
                req.extra_user_content_parts[:] = [
                    p
                    for p in req.extra_user_content_parts
                    if not str(getattr(p, "text", "")).startswith(
                        ("<Quoted Message>", "<image_caption>", "[Audio Attachment]", "[File Attachment]", "[Video Attachment]")
                    )
                ]
        elif umo and not is_wake and not umo.startswith(("irmia:", "cron:")):
            _, mtype, _ = parse_umo(umo)
            if "group" not in mtype.lower():
                sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
                sender_name = str(getattr(event, "get_sender_name", lambda: "")() or "")
                who = self.store.resolve_person_name(
                    sender_id, sender_name, "", cfg.contacts_map
                )
                req.prompt = f"[私聊 · {who} · sid {umo}]\n{req.prompt or ''}"

        # 3. 静态 SELF_BRIEF 幂等放入 system_prompt（保护前缀缓存），动态此刻层与工作目录说明放入 extra_user_content_parts
        self_brief = build_self_brief(
            cfg.intercept_direct_reply,
            cfg.topic_max_chars,
            mention_inline=cfg.mention_inline_enabled,
        )
        workspace_note = ""
        if hasattr(req, "system_prompt"):
            sys_p = str(getattr(req, "system_prompt", "") or "")
            m = WORKSPACE_NOTE_RE.search(sys_p)
            if m:
                workspace_note = m.group(0).strip()
                sys_p = sys_p[: m.start()] + sys_p[m.end() :]
            if SELF_BRIEF_HEADER not in sys_p:
                sys_p = f"{sys_p}\n\n{self_brief}" if sys_p else self_brief
            req.system_prompt = sys_p
            include_brief_in_extra = False
        else:
            include_brief_in_extra = True

        if hasattr(req, "extra_user_content_parts"):
            sessions = await self.store.list_sessions()
            tz = self.tz_name()
            parts = []
            if include_brief_in_extra:
                parts.append(self_brief)
            parts.extend(
                [
                    "=== [IRMIA] 当前状态 ===",
                    format_clock_line(time.time(), tz),
                    "这次是你自己醒来的，没有会话在等你回复。"
                    if is_wake
                    else f"这次叫醒你的会话：{umo}",
                    render_session_list(
                        sessions,
                        lambda s: self.store.resolve_session_name(
                            s.umo,
                            s.group_name,
                            s.last_sender_id,
                            s.last_sender_name,
                            cfg.contacts_map,
                            with_last_sender=True,
                        ),
                        tz,
                        cfg.session_list_max,
                    ),
                ]
            )
            if workspace_note:
                parts.append(workspace_note)
            part = TextPart(text="\n\n".join(parts))
            part.mark_as_temp()
            req.extra_user_content_parts.append(part)

    async def _bind_master_conversation(self, master_umo: str, req: Any) -> None:
        """将当前请求的 conversation 与 contexts 切换到 master_umo 对应的全局主脑对话。"""
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                return
            cid = await conv_mgr.get_curr_conversation_id(master_umo)
            if not cid:
                pid, _, _ = parse_umo(master_umo)
                cid = await conv_mgr.new_conversation(
                    master_umo, platform_id=pid or None
                )
            master_conv = await conv_mgr.get_conversation(
                master_umo, cid, create_if_not_exists=True
            )
            if master_conv is None:
                return
            req.conversation = master_conv
            req.contexts = _parse_conv_history(master_conv)
        except Exception as exc:
            self.logger.debug(f"[IRMIA] 绑定全局主脑历史跳过: {exc}")

    # ---------------- 钩子 4：工具回执记账 ----------------

    async def on_llm_tool_respond(
        self, event: Any, tool: Any, tool_args: Any, tool_result: Any
    ) -> None:
        """监听 send_message_to_user 成功回执：标记是否已发到当前窗口，并把文字记入兜底账本（is_bot=1）。"""
        if is_internal_silent_event(event) or not self.cfg().enabled:
            return
        if getattr(tool, "name", "") != "send_message_to_user":
            return

        try:
            content = getattr(tool_result, "content", None)
            if content is not None and isinstance(content, list):
                res_text = " ".join(
                    str(getattr(c, "text", "")) for c in content
                ).strip()
            else:
                res_text = str(tool_result or "").strip()

            if not res_text.startswith(SEND_OK_PREFIX):
                return
            target_umo = res_text[len(SEND_OK_PREFIX) :].strip()
            # 发送成功即标记（图片/表情包也算），与能否提取出文字、记账是否成功无关
            current_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if target_umo == current_umo and hasattr(event, "set_extra"):
                event.set_extra("irmia_sent_current_session", True)

            args = tool_args if isinstance(tool_args, dict) else {}
            msgs = args.get("messages", [])
            text_parts: list[str] = []
            if isinstance(msgs, list):
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    k = str(m.get("type", "")).lower()
                    if k == "plain":
                        text_parts.append(str(m.get("text", "") or ""))
                    elif k == "mention_user":
                        text_parts.append(f"[@{m.get('mention_user_id', '')}]")
                    else:
                        text_parts.append(f"[{k}]")
            spoken_text = "".join(text_parts).strip()
            if spoken_text:
                await self._record_bot_speech(target_umo, spoken_text)
        except Exception as exc:
            self.logger.debug(f"[IRMIA] 处理 send_message_to_user 回执异常: {exc}")

    # ---------------- 钩子 5：发送前拦截（沉默权/工具回流/当前窗口已发声）与发送后记账 ----------------

    def _install_send_message_patch(self) -> None:
        if self.context is None or not hasattr(self.context, "send_message"):
            return
        if getattr(self.context, "_irmia_send_patched", False):
            return
        original = self.context.send_message
        self._original_send_message = original
        setattr(self.context, "_irmia_send_patched", True)

        async def patched_send_message(session, message_chain):
            if self.cfg().enabled and self.cfg().intercept_direct_reply:
                ev = self._current_chat_event.get()
                if ev is not None and not is_internal_silent_event(ev):
                    ev_umo = str(getattr(ev, "unified_msg_origin", "") or "").strip()
                    if ev_umo and str(session) == ev_umo and not ev.get_extra("irmia_in_decorating"):
                        chain = getattr(message_chain, "chain", None)
                        if chain:
                            buffered = ev.get_extra("irmia_buffered_tool_chain", [])
                            if not isinstance(buffered, list):
                                buffered = []
                            buffered.extend(chain)
                            ev.set_extra("irmia_buffered_tool_chain", buffered)
                            ev.set_extra("irmia_sent_current_session", True)
                            return True
            return await original(session, message_chain)

        self.context.send_message = patched_send_message

    def _uninstall_send_message_patch(self) -> None:
        if self.context is not None and getattr(self.context, "_irmia_send_patched", False):
            if hasattr(self, "_original_send_message"):
                self.context.send_message = self._original_send_message
            setattr(self.context, "_irmia_send_patched", False)

    def _flush_unconsumed_buffered_chain(self, event: Any) -> None:
        """若本轮未进入 on_decorating_result（如模型末步无文本输出），把暂存链补发出去。"""
        if not hasattr(event, "get_extra"):
            return
        buffered = event.get_extra("irmia_buffered_tool_chain")
        if not isinstance(buffered, list) or not buffered:
            return
        event.set_extra("irmia_buffered_tool_chain", [])
        event.set_extra("irmia_in_decorating", True)
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo or self.context is None or not hasattr(self.context, "send_message"):
            return
        mc = MessageChain() if MessageChain is not None else SimpleNamespace(chain=[])
        mc.chain = list(buffered)
        self._spawn_bg(self.context.send_message(umo, mc))

    async def on_llm_response(self, event: Any, resp: Any = None) -> None:
        """核心仅在无 tool_calls 的最终回复步触发 OnLLMResponseEvent，标记已进入最终回复阶段。"""
        if is_internal_silent_event(event) or not self.cfg().enabled:
            return
        if hasattr(event, "set_extra"):
            event.set_extra("irmia_final_llm_resp", True)

    async def on_decorating_result(self, event: Any) -> None:
        """处理工具调用途中的过渡文字拦截、[SILENT] 静默标记，以及工具回复当前窗口时的回流替换。

        1. 在 OnLLMResponseEvent 触发前（即模型仍在调用 read_channel / send_message_to_user 的中间步骤），
           模型顺嘴输出的过渡文字（preamble）在此直接收起，不发往聊天窗口，也不开启 irmia_in_decorating。
        2. 最终步若模型曾通过 send_message_to_user 发往当前唤醒窗口，暂存的真实回复链在此处替换掉
           模型末尾的自言自语总结，继续交给后续装饰钩子（表情包替换、分段发送）。
        """
        if is_internal_silent_event(event):
            return
        cfg = self.cfg()
        if not cfg.enabled:
            return
        if hasattr(event, "get_extra") and event.get_extra("handlers_parsed_params"):
            return

        result = getattr(event, "get_result", lambda: None)()
        if result is None or not getattr(result, "chain", None):
            return
        if not result.is_llm_result():
            return

        try:
            plain_text = result.get_plain_text().strip()
        except Exception:
            plain_text = ""

        # 若尚未经过 on_llm_response（显式设为了 False），说明是工具循环中间步骤的过渡话，直接收起
        is_final = (
            event.get_extra("irmia_final_llm_resp")
            if hasattr(event, "get_extra")
            else None
        )
        if is_final is False:
            if plain_text:
                self.logger.info(f"[IRMIA 收起工具调用过渡文字] {plain_text}")
            result.chain = []
            return

        if hasattr(event, "set_extra"):
            event.set_extra("irmia_in_decorating", True)

        if cfg.intercept_direct_reply and event.get_extra("irmia_sent_current_session"):
            if hasattr(event, "set_extra"):
                event.set_extra("_send_message_to_user_current_session_plain_texts", [])
            buffered = event.get_extra("irmia_buffered_tool_chain")
            if isinstance(buffered, list) and buffered:
                if plain_text:
                    self.logger.info(
                        f"[IRMIA 工具回复回流流水线，收起末尾文字] {plain_text}"
                    )
                result.chain = list(buffered)
                event.set_extra("irmia_buffered_tool_chain", [])
                return
            if plain_text:
                self.logger.info(f"[IRMIA 已用工具回复当前窗口，收起末尾文字] {plain_text}")
            result.chain = []
            return

        if plain_text == "[SILENT]":
            self.logger.info(
                f"[IRMIA 沉默] 主脑选择保持沉默 ({getattr(event, 'unified_msg_origin', '')})"
            )
            result.chain = []
            return

    async def on_after_message_sent(self, event: Any) -> None:
        """当直出回复实际发送到平台后，确保兜底账本中也记录了这句（我）的发言。"""
        if is_internal_silent_event(event) or not self.cfg().enabled:
            return
        result = getattr(event, "get_result", lambda: None)()
        if result is None or not getattr(result, "chain", None):
            return
        try:
            if not result.is_model_result():
                return
            plain_text = result.get_plain_text().strip()
            if not plain_text:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            await self._record_bot_speech(umo, plain_text)
        except Exception:
            pass

    # ---------------- 呼吸式概率心跳后台循环 ----------------

    def _start_heartbeat_loop(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """按抽签间隔循环抽签；单拍异常只记日志，下一拍照常继续。"""
        while True:
            await asyncio.sleep(self.cfg().heartbeat_tick_sec)
            try:
                await self._heartbeat_tick()
            except Exception as exc:
                self.logger.error(f"[IRMIA] 心跳本拍异常: {exc!r}")

    def local_hour(self) -> int:
        """按 AstrBot 配置的时区取当前小时（0~23）。"""
        tz, _ = resolve_tz(self.tz_name())
        return datetime.now(tz=tz).hour

    def in_quiet_window(self, cfg: IrmiaConfig) -> bool:
        """判断当前是否处于心跳静默窗口（窗口关闭时恒为 False）。"""
        if not cfg.heartbeat_quiet_enabled:
            return False
        return is_in_quiet_window(
            self.local_hour(),
            cfg.heartbeat_quiet_start_hour,
            cfg.heartbeat_quiet_end_hour,
        )

    async def _vision_cache_note(self, cfg: IrmiaConfig) -> str:
        """在线程池查询留底和描述缓存数量，生成状态说明。"""
        kept, desc = await asyncio.to_thread(self.vision_store.stats)
        ttl = (
            f"未使用 {cfg.vision_cache_ttl_days} 天后清理"
            if cfg.vision_cache_ttl_days > 0
            else "不过期"
        )
        return f"留底 {kept} 张（保留 {cfg.image_keep_days} 天），描述缓存 {desc} 条（{ttl}）"

    async def _maybe_cleanup_vision(self, cfg: IrmiaConfig) -> None:
        """启动及每小时清理 Mia 自己的留底图和描述缓存。"""
        now = time.time()
        if now - self._vision_cleanup_at < 3600:
            return
        removed = await asyncio.to_thread(
            self.vision_store.cleanup, cfg.image_keep_days, cfg.vision_cache_ttl_days
        )
        self._vision_cleanup_at = now
        if any(removed):
            self.logger.info(
                f"[IRMIA] 清理留底行 {removed[0]} 条、图片 {removed[1]} 张、描述 {removed[2]} 条"
            )

    def quiet_window_note(self, cfg: IrmiaConfig) -> str:
        """生成状态摘要里那行静默窗口说明。"""
        if not cfg.heartbeat_quiet_enabled:
            return "夜间静默：关闭（全天按概率自然呼吸）"
        window = (
            f"{cfg.heartbeat_quiet_start_hour:02d}:00~"
            f"{cfg.heartbeat_quiet_end_hour:02d}:00"
        )
        state = "当前在静默中（心跳已暂停）" if self.in_quiet_window(cfg) else "当前不在静默窗口"
        return f"夜间静默：{window}（{state}）"

    async def _heartbeat_tick(self) -> None:
        cfg = self.cfg()
        if not cfg.enabled:
            return
        await self._maybe_cleanup_vision(cfg)
        if not cfg.heartbeat_enabled:
            return
        # 正在和人聊、已有唤醒进行中或尚未绑定主脑时跳过，不推进抽签周期
        if self._brain_lock.locked() or self._wake_running or not await self.resolve_master_umo():
            return

        # 夜间静默窗口：窗口内先确保心率归零一次，然后整拍跳过，不抽签
        if self.in_quiet_window(cfg):
            if not self._was_in_quiet:
                # 进窗口：把安静计时归零，避免窗口里累积出一发必中的概率
                self.heartbeat.note_activity()
                self._was_in_quiet = True
                self.logger.info(
                    f"[IRMIA] 进入夜间静默窗口 "
                    f"{cfg.heartbeat_quiet_start_hour:02d}:00~"
                    f"{cfg.heartbeat_quiet_end_hour:02d}:00，本拍起暂停心跳"
                )
            return
        if self._was_in_quiet:
            # 出窗口：再归零一次，让早晨第一拍的命中概率从 floor 重新开始爬
            self.heartbeat.note_activity()
            self._was_in_quiet = False
            self.logger.info("[IRMIA] 已离开夜间静默窗口，心跳恢复正常呼吸")

        firing = self.heartbeat.sample_tick()
        if firing is None:
            return
        self.logger.info(
            f"[IRMIA 心跳触发] 安静 {firing.quiet_seconds:.0f}s, "
            f"p={firing.probability:.4f}, roll={firing.roll:.4f}"
        )
        await self.trigger_wake_turn(firing=firing)

    async def trigger_wake_turn(
        self,
        *,
        firing: HeartbeatFiring | None = None,
        manual_note: str | None = None,
    ) -> str:
        """发起静默唤醒：思考与聊天并行，仅末尾小结写回取锁、排在聊天之后。"""
        master_umo = await self.resolve_master_umo()
        if not master_umo:
            return "还没有绑定全局主脑会话（管理员先私聊一次，或在配置里填写 master_umo），本次不唤醒。"
        if self._wake_running:
            return "上一次唤醒还没结束，这次先不叫醒。"
        self._wake_running = True
        try:
            return await self._run_wake_turn(master_umo, firing, manual_note)
        finally:
            self._wake_running = False

    async def _run_wake_turn(
        self,
        master_umo: str,
        firing: HeartbeatFiring | None,
        manual_note: str | None,
    ) -> str:
        """不持主脑锁跑原生 Agent 唤醒，做过事则在末尾排队写入轻量小结。"""
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            build_main_agent,
        )
        from astrbot.core.pipeline.context import call_event_hook
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.star.star_handler import EventType
        from astrbot.core.tools.message_tools import SendMessageToUserTool

        cfg = self.cfg()
        session = MessageSession.from_str(master_umo)

        prompt_text = (
            render_heartbeat_prompt(firing)
            if firing is not None
            else f"[手动唤醒] {manual_note or '管理员让你醒来看看会话清单和待办。'}"
        )

        wake_event = HeartbeatWakeEvent(
            context=self.context,
            session=session,
            message=prompt_text,
            extras={"irmia_wake": True},
            message_type=session.message_type,
        )

        cfg_dict = self.context.get_config(umo=wake_event.unified_msg_origin)
        provider_settings = cfg_dict.get("provider_settings", {}) or {}
        runner_cfg = (
            cfg_dict.get("agent_runner", {}).get("config", {}) or {}
        )
        persona_cfg = runner_cfg.get("persona", {}) or {}
        misc_cfg = runner_cfg.get("misc", {}) or {}
        compression_cfg = runner_cfg.get("compression", {}) or {}

        # 与核心 InternalAgentSubStage 相同的上下文压缩/裁剪参数映射
        max_turns = compression_cfg.get("max_turns", -1)
        trim_turns = compression_cfg.get("trim_turns", 1)
        dequeue_turns = min(
            max(1, trim_turns), max_turns - 1 if max_turns > 0 else trim_turns
        )

        build_cfg = MainAgentBuildConfig(
            tool_call_timeout=int(misc_cfg.get("tool_call_timeout", 120) or 120),
            fallback_provider_ids=runner_cfg.get("model", {}).get(
                "fallback_provider_ids", []
            ),
            context_limit_reached_strategy=compression_cfg.get(
                "overflow_strategy", "truncate_by_turns"
            ),
            llm_compress_instruction=compression_cfg.get("instruction", ""),
            llm_compress_keep_recent_ratio=compression_cfg.get(
                "keep_recent_ratio", 0.15
            ),
            llm_compress_provider_id=compression_cfg.get("provider_id", ""),
            max_context_length=max_turns,
            dequeue_context_length=dequeue_turns if dequeue_turns > 0 else 1,
            fallback_max_context_tokens=compression_cfg.get(
                "fallback_max_tokens", 128000
            ),
            llm_safety_mode=bool(persona_cfg.get("safety_mode", True)),
            safety_mode_strategy=str(
                persona_cfg.get("safety_mode_strategy", "system_prompt")
            ),
            streaming_response=False,
            computer_use_runtime=str(
                provider_settings.get("computer_use_runtime", "none")
            ),
            sandbox_cfg=provider_settings.get("sandbox", {}) or {},
            provider_settings=provider_settings,
        )

        req = ProviderRequest()
        await self._bind_master_conversation(master_umo, req)
        req.prompt = prompt_text

        tool_mgr = self.context.get_llm_tool_manager()
        req.func_tool = ToolSet()
        req.func_tool.add_tool(tool_mgr.get_builtin_tool(SendMessageToUserTool))

        build_res = await build_main_agent(
            event=wake_event,
            plugin_context=self.context,
            config=build_cfg,
            req=req,
            apply_reset=False,
        )
        if not build_res:
            return "构建主 Agent 失败（可能未配置可用模型提供商）。"

        # 触发全局 OnLLMRequestEvent 钩子（使本插件及 AstrHe / LivingMemory 的注入全部生效）
        await call_event_hook(wake_event, EventType.OnLLMRequestEvent, req)
        if build_res.reset_coro is not None:
            await build_res.reset_coro

        runner = build_res.agent_runner
        async for _ in runner.step_until_done(cfg.wake_max_steps):
            pass
        llm_resp = runner.get_final_llm_resp()
        reply_text = (
            llm_resp.completion_text.strip()
            if llm_resp and llm_resp.completion_text
            else ""
        )
        if reply_text == "[SILENT]":
            reply_text = ""

        # 空拍（没调过工具、手动唤醒也没有有效结果）不调小结模型、不往主脑历史写任何东西
        if req.tool_calls_result or (manual_note is not None and reply_text):
            await self._append_wake_summary(
                master_umo, wake_event, req, prompt_text, reply_text, cfg
            )
        return reply_text or "（安静结束本拍，未外发消息）"

    async def _append_wake_summary(
        self,
        master_umo: str,
        wake_event: Any,
        req: Any,
        prompt_text: str,
        reply_text: str,
        cfg: IrmiaConfig,
    ) -> None:
        """锁外生成轻量小结，锁内重读最新主脑历史并追加；生成失败只记日志。"""
        from astrbot.core.utils.history_saver import persist_agent_history

        if req.conversation is None:
            return
        provider = await self.topic_summarizer.resolve_provider(
            master_umo, cfg.light_provider_id
        )
        if provider is None:
            self.logger.warning("[IRMIA] 唤醒小结跳过：没有可用的轻量模型")
            return
        material = build_wake_material(prompt_text, req.tool_calls_result, reply_text)
        try:
            summary = await summarize_wake(
                provider,
                material,
                max_chars=cfg.wake_summary_max_chars,
                timeout_sec=cfg.light_timeout_sec,
            )
        except Exception as exc:
            self.logger.warning(f"[IRMIA] 唤醒小结失败，本拍不写入上下文: {exc!r}")
            return
        if not summary:
            return
        conv_mgr = self.context.conversation_manager
        async with self._brain_lock:
            req.conversation = await conv_mgr.get_conversation(
                master_umo, req.conversation.cid
            )
            await persist_agent_history(
                conv_mgr,
                event=wake_event,
                req=req,
                summary_note=f"[IRMIA Wake] {summary}",
            )
        self.logger.info(f"[IRMIA 唤醒小结] {summary}")

    # ---------------- 状态诊断 ----------------

    async def status_report(self) -> str:
        """生成 /astrmia status 状态摘要文本。"""
        cfg = self.cfg()
        master_umo = await self.resolve_master_umo()
        dist = self.heartbeat.distribution()
        quiet_min = self.heartbeat.quiet_ms() / 60_000.0
        cur_p = self.heartbeat.current_probability()
        sessions = await self.store.list_sessions()
        astrhe_linked = (
            "已连接"
            if self.store.astrhe_db_path and self.store.astrhe_db_path.is_file()
            else "未检测到（使用内置兜底账本）"
        )

        lines = [
            "=== AstrMia 运行状态 ===",
            f"总开关：{'开启' if cfg.enabled else '关闭'}",
            f"全局主脑会话：{master_umo or '（待管理员首次私聊自动绑定）'}",
            f"AstrHe 账本/关系本联动：{astrhe_linked}",
            f"工具已发到当前窗口时收起末尾回复：{'开启' if cfg.intercept_direct_reply else '关闭（默认直出，输出 [SILENT] 静默）'}",
            f"呼吸心跳：{'开启' if cfg.heartbeat_enabled else '关闭'} "
            f"（区间 {dist.floor_ms / 60000:.0f}~{dist.ceil_ms / 60000:.0f}m · "
            f"目标均值 {dist.mean_ms / 60000:.1f}m · α={dist.alpha:.4f}）",
            self.quiet_window_note(cfg),
            f"识图缓存：{await self._vision_cache_note(cfg)}",
            f"当前安静时长：{quiet_min:.1f} 分钟（下一拍抽中概率 p={cur_p:.3f}，连续空拍={self.heartbeat.idle_ticks}）",
            f"已知外部会话数：{len(sessions)}",
        ]
        for s in sessions[: cfg.session_list_max]:
            name = self.store.resolve_session_name(
                s.umo,
                s.group_name,
                s.last_sender_id,
                s.last_sender_name,
                cfg.contacts_map,
            )
            topic_str = f" · 话题：{s.topic}" if s.topic else ""
            lines.append(
                f"  - {name} ({s.umo})：未读 {s.unread} / 总计 {s.total_messages}{topic_str}"
            )
        return "\n".join(lines)
