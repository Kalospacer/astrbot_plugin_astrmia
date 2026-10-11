"""IRMIA 插件运行时协调器：串联全局主脑、信箱分流、话题概括、read_channel、呼吸心跳与钩子。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import IrmiaConfig, load_irmia_config
from .heartbeat import HeartbeatEngine, HeartbeatFiring
from .inbox import GroupBatcher, evaluate_channel_message
from .read_channel_tool import (
    SENT_CREDENTIAL_NOTE,
    ReadChannelTool,
    execute_read_channel,
)
from .render import (
    SELF_BRIEF_HEADER,
    build_self_brief,
    format_clock_line,
    format_short_time,
    render_heartbeat_prompt,
    render_mention_notice,
    render_session_list,
)
from .store import IrmiaStore, parse_umo
from .topic import TopicSummarizer

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


def extract_event_text_and_at(event: Any) -> tuple[str, bool, str]:
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
                parts.append(f"[引用: {reply_text[:40]}]")
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

        cfg = load_irmia_config(self._raw_config)
        self.heartbeat = HeartbeatEngine(
            floor_ms=cfg.heartbeat_floor_min * 60_000,
            ceil_ms=cfg.heartbeat_ceil_min * 60_000,
            tick_ms=60_000,
            target_mean_ms=cfg.heartbeat_target_mean_min * 60_000,
        )

        self._bot_self_ids: set[str] = set()
        self._auto_master_umo: str | None = None
        self._active_turns: int = 0
        self._last_active_event: Any = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._tools_registered = False

    def cfg(self) -> IrmiaConfig:
        """动态读取当前最新配置并按需同步心跳参数。"""
        c = load_irmia_config(self._raw_config)
        self.heartbeat.reconfigure(
            floor_min=c.heartbeat_floor_min,
            ceil_min=c.heartbeat_ceil_min,
            target_mean_min=c.heartbeat_target_mean_min,
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
        """启动存储、注册 read_channel 工具并开启呼吸心跳循环。"""
        await self.store.open()
        self._auto_master_umo = (
            await self.store.get_kv("auto_master_umo", "")
        ).strip()
        self._register_tools()
        self._start_heartbeat_loop()
        dist = self.heartbeat.distribution()
        self.logger.info(
            f"[IRMIA] 已启动：心跳区间 {dist.floor_ms / 60000:.0f}~{dist.ceil_ms / 60000:.0f}m, "
            f"目标均值 {dist.mean_ms / 60000:.2f}m (alpha={dist.alpha:.4f})"
        )

    async def terminate(self) -> None:
        """停止心跳循环、等待后台任务收尾并关闭存储。"""
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
        await self.store.close()

    def _spawn_bg(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _register_tools(self) -> None:
        if self._tools_registered:
            return
        add_llm_tools = getattr(self.context, "add_llm_tools", None)
        if not callable(add_llm_tools):
            return
        tool = ReadChannelTool(handler_fn=self._handle_read_channel_tool)
        try:
            add_llm_tools(tool)
            self._tools_registered = True
        except Exception as exc:
            self.logger.warning(f"[IRMIA] 注册 read_channel 工具失败: {exc}")

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

        text, is_platform_at, self_id = extract_event_text_and_at(event)
        if self_id:
            self._bot_self_ids.add(self_id)

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
                    umo,
                    light_provider_id=cfg.light_provider_id,
                    min_unread=cfg.topic_min_unread,
                    cooldown_sec=cfg.topic_cooldown_sec,
                    force_for_mention=False,
                    contacts_map=cfg.contacts_map,
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

        topic = (
            await self.topic_summarizer.maybe_summarize(
                umo,
                light_provider_id=cfg.light_provider_id,
                min_unread=cfg.topic_min_unread,
                cooldown_sec=cfg.topic_cooldown_sec,
                force_for_mention=True,
                contacts_map=cfg.contacts_map,
            )
            or ""
        )

        st = await self.store.get_session_state(umo)
        read_up_to_id = int(st.get("read_up_to_id", 0) or 0)
        unread_cnt = self.store.count_unread(umo, read_up_to_id)

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

        notice = render_mention_notice(
            umo=umo,
            where_name=where_name,
            caller_name=caller_name,
            is_at=decision.is_platform_at,
            at_label=at_label,
            unread=unread_cnt,
            topic=topic,
        )
        event.set_extra("irmia_mention_notice", notice)
        event.set_extra("irmia_called_sid", umo)

    # ---------------- 钩子 2：LLM 请求前（主脑历史切换 + 半遮面替换 + 此刻层注入） ----------------

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
        self._last_active_event = event

        # 赋予主脑跨会话调用 send_message_to_user 的权限
        event.role = "admin"

        # 1. 全局唯一主脑历史绑定
        master_umo = await self.resolve_master_umo()
        if master_umo and (
            getattr(req, "conversation", None) is None or umo != master_umo
        ):
            await self._bind_master_conversation(master_umo, req)

        # 2. 群聊点名 -> 替换为半遮面通知卡（同时清理原群聊消息附带的引用/图片/音频块）
        mention_notice = (
            event.get_extra("irmia_mention_notice")
            if hasattr(event, "get_extra")
            else None
        )
        if isinstance(mention_notice, str) and mention_notice.strip():
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
        elif umo and not umo.startswith(("irmia:", "cron:")):
            _, mtype, _ = parse_umo(umo)
            if "group" not in mtype.lower():
                sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
                sender_name = str(getattr(event, "get_sender_name", lambda: "")() or "")
                who = self.store.resolve_person_name(
                    sender_id, sender_name, "", cfg.contacts_map
                )
                req.prompt = f"[私聊消息 · {who} (sid: {umo})]\n{req.prompt or ''}"

        # 3. 静态 SELF_BRIEF 幂等放入 system_prompt（保护前缀缓存），动态此刻层放入 extra_user_content_parts
        self_brief = build_self_brief(cfg.intercept_direct_reply)
        if hasattr(req, "system_prompt"):
            sys_p = str(getattr(req, "system_prompt", "") or "")
            if SELF_BRIEF_HEADER not in sys_p:
                req.system_prompt = (
                    f"{sys_p}\n\n{self_brief}" if sys_p else self_brief
                )
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
                    "=== [此刻层 · 状态与信箱清单] ===",
                    format_clock_line(time.time(), tz),
                    f"当前唤醒来源：{umo or '内部唤醒'}",
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
                    ),
                ]
            )
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

    # ---------------- 钩子 3：Agent 生命周期与工具回执增强 ----------------

    async def on_agent_begin(self, event: Any, run_context: Any) -> None:
        if is_internal_silent_event(event):
            return
        self._active_turns += 1
        self._last_active_event = event

    async def on_agent_done(
        self, event: Any, run_context: Any, response: Any
    ) -> None:
        if is_internal_silent_event(event):
            return
        self._active_turns = max(0, self._active_turns - 1)
        if response is not None and getattr(response, "completion_text", "").strip() == "[SILENT]":
            response.completion_text = ""

    async def on_llm_tool_respond(
        self, event: Any, tool: Any, tool_args: Any, tool_result: Any
    ) -> None:
        """监听 send_message_to_user：记入兜底账本（is_bot=1）并在回执中附带已送达凭据。"""
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
                if hasattr(event, "set_extra"):
                    event.set_extra("irmia_spoke_via_tool", True)

            if content is not None and isinstance(content, list) and content:
                last_item = content[-1]
                if hasattr(last_item, "text") and SENT_CREDENTIAL_NOTE not in str(
                    last_item.text
                ):
                    last_item.text = f"{last_item.text}{SENT_CREDENTIAL_NOTE}"
        except Exception as exc:
            self.logger.debug(f"[IRMIA] 处理 send_message_to_user 回执异常: {exc}")

    # ---------------- 钩子 4：发送前拦截（自言自语/沉默权）与发送后记账 ----------------

    async def on_decorating_result(self, event: Any) -> None:
        """处理 [SILENT] 静默标记与 intercept_direct_reply（拦截直出文字作屋内自言自语）开关。"""
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

        try:
            plain_text = result.get_plain_text().strip()
        except Exception:
            plain_text = ""

        if plain_text == "[SILENT]":
            self.logger.info(
                f"[IRMIA 沉默] 主脑选择保持沉默 ({getattr(event, 'unified_msg_origin', '')})"
            )
            result.chain.clear()
            return

        if cfg.intercept_direct_reply:
            if plain_text:
                self.logger.info(f"[IRMIA 屋里自言自语] {plain_text}")
            result.chain.clear()

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
        """每分钟抽签一次，命中时触发静默心跳唤醒。"""
        try:
            while True:
                await asyncio.sleep(60.0)
                cfg = self.cfg()
                if not cfg.enabled or not cfg.heartbeat_enabled:
                    continue
                if self._active_turns > 0:
                    continue
                firing = self.heartbeat.sample_tick()
                if firing is not None:
                    self.logger.info(
                        f"[IRMIA 心跳触发] 安静 {firing.quiet_seconds:.0f}s, "
                        f"p={firing.probability:.4f}, roll={firing.roll:.4f}"
                    )
                    await self.trigger_wake_turn(firing=firing)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(f"[IRMIA] 心跳循环异常: {exc}")

    async def trigger_wake_turn(
        self,
        *,
        firing: HeartbeatFiring | None = None,
        manual_note: str | None = None,
    ) -> str:
        """复用 AstrBot 原生 build_main_agent 链路发起一次静默唤醒 turn（心跳或手动 /irmia wake）。"""
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            build_main_agent,
        )
        from astrbot.core.config.agent_runner import (
            resolve_context_compression_config,
        )
        from astrbot.core.pipeline.context import call_event_hook
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.star.star_handler import EventType
        from astrbot.core.tools.message_tools import SendMessageToUserTool
        from astrbot.core.utils.history_saver import persist_agent_history

        master_umo = await self.resolve_master_umo()
        session_str = master_umo or "irmia:FriendMessage:master"
        session = MessageSession.from_str(session_str)

        prompt_text = (
            render_heartbeat_prompt(firing)
            if firing is not None
            else f"[手动唤醒] {manual_note or '来看看外面的信箱和待办吧。'}"
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

        build_cfg = MainAgentBuildConfig(
            tool_call_timeout=int(misc_cfg.get("tool_call_timeout", 120) or 120),
            fallback_provider_ids=runner_cfg.get("model", {}).get(
                "fallback_provider_ids", []
            ),
            **resolve_context_compression_config(
                runner_cfg.get("compression", {}) or {}
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
        await self._bind_master_conversation(session_str, req)
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
        self._active_turns += 1
        try:
            async for _ in runner.step_until_done(32):
                pass
            llm_resp = runner.get_final_llm_resp()
            reply_text = (
                llm_resp.completion_text.strip()
                if llm_resp and llm_resp.completion_text
                else ""
            )
            if reply_text == "[SILENT]":
                reply_text = ""
            spoke = bool(wake_event.get_extra("irmia_spoke_via_tool"))
            # 空拍心跳（既未通过工具发言、也无有效内心总结）不往主脑对话历史硬塞垃圾记录
            if spoke or (manual_note is not None and reply_text):
                await persist_agent_history(
                    self.context.conversation_manager,
                    event=wake_event,
                    req=req,
                    summary_note=f"[IRMIA Wake] {reply_text or '已通过工具主动发送消息'}",
                )
            return reply_text or "（安静结束本拍，未外发消息）"
        finally:
            self._active_turns = max(0, self._active_turns - 1)

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
            f"直出文本拦截（仅工具发声）：{'开启' if cfg.intercept_direct_reply else '关闭（默认直出，输出 [SILENT] 静默）'}",
            f"呼吸心跳：{'开启' if cfg.heartbeat_enabled else '关闭'} "
            f"（区间 {dist.floor_ms / 60000:.0f}~{dist.ceil_ms / 60000:.0f}m · "
            f"目标均值 {dist.mean_ms / 60000:.1f}m · α={dist.alpha:.4f}）",
            f"当前安静时长：{quiet_min:.1f} 分钟（下一拍抽中概率 p={cur_p:.3f}，连续空拍={self.heartbeat.idle_ticks}）",
            f"已知外部会话数：{len(sessions)}",
        ]
        for s in sessions[:8]:
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
