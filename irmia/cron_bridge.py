"""CronJob 主脑接管：定时任务唤醒改跑在全局主脑历史上，并补发请求钩子。

AstrBot 原生 `CronJobManager._woke_main_agent` 加载目标会话自己的旧历史、
不触发 OnLLMRequestEvent、把带时间戳的任务对象拼进 system_prompt，
末尾无锁写回历史。这里包装该方法：启用接管且已绑定主脑时改走
`run_cron_on_master`，否则原样透传。
"""

from __future__ import annotations

import json
import weakref
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .config import IrmiaConfig
    from .runtime import IrmiaRuntime

CRON_PATCH_FLAG = "_irmia_cron_patched"
CRON_ORIGINAL_ATTR = "_irmia_cron_original"


def is_cron_event(event: Any) -> bool:
    """判断事件是否由定时任务触发（心跳事件虽继承 CronMessageEvent，但不算）。"""
    if event is None:
        return False
    if event.__class__.__name__ == "CronMessageEvent":
        return True
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return False
    try:
        return bool(get_extra("cron_job")) or get_extra("cron_payload") is not None
    except Exception:
        return False


def build_cron_user_prompt(
    message: str, extras: dict[str, Any] | None, delivery_session_str: str
) -> str:
    """把任务说明与带时间戳的任务对象放进用户消息，system_prompt 保持静态。"""
    cron_job = (extras or {}).get("cron_job") or {}
    name = str(cron_job.get("name") or cron_job.get("id") or "未命名任务")
    target = delivery_session_str or "无（仅在后台执行）"
    return "\n".join(
        [
            f"[定时任务] {name}",
            f"目标会话：{target}",
            f"任务说明：{message}",
            "这是定时任务触发的自主轮次，不是聊天，没有人在等你回话。"
            "需要联系对方时用 send_message_to_user 发到目标会话，并自然地说明来意；"
            "无需联系时直接完成任务。完成后简要总结做了什么、结果如何。",
            f"任务详情：{json.dumps(cron_job, ensure_ascii=False)}",
        ]
    )


class _CronPatchProxy:
    """挂在 cron_manager 上的轻量代理，弱引用 runtime 避免形成强引用环。"""

    __slots__ = ("_runtime_ref", "_original")

    def __init__(
        self, runtime: IrmiaRuntime, original: Callable[..., Awaitable[None]]
    ) -> None:
        self._runtime_ref = weakref.ref(runtime)
        self._original = original

    async def __call__(
        self,
        *,
        message: str,
        session_str: Any,
        extras: dict,
        delivery_session_str: str = "",
    ) -> None:
        runtime = self._runtime_ref()
        if runtime is None:
            return await self._original(
                message=message,
                session_str=session_str,
                extras=extras,
                delivery_session_str=delivery_session_str,
            )
        cfg = runtime.cfg()
        master_umo = (
            await runtime.resolve_master_umo(cfg)
            if cfg.enabled and cfg.takeover_cron
            else ""
        )
        if not master_umo:
            return await self._original(
                message=message,
                session_str=session_str,
                extras=extras,
                delivery_session_str=delivery_session_str,
            )
        await run_cron_on_master(
            runtime,
            self._original,
            master_umo=master_umo,
            message=message,
            session_str=session_str,
            extras=extras or {},
            delivery_session_str=delivery_session_str,
            cfg=cfg,
        )


def install_cron_patch(runtime: IrmiaRuntime) -> bool:
    """包装 cron_manager._woke_main_agent；已挂载或没有定时任务管理器时跳过。"""
    cron_mgr = getattr(runtime.context, "cron_manager", None)
    original = getattr(cron_mgr, "_woke_main_agent", None)
    if original is None or getattr(cron_mgr, CRON_PATCH_FLAG, False):
        return False

    setattr(cron_mgr, CRON_ORIGINAL_ATTR, original)
    cron_mgr._woke_main_agent = _CronPatchProxy(runtime, original)
    setattr(cron_mgr, CRON_PATCH_FLAG, True)
    return True


def uninstall_cron_patch(runtime: IrmiaRuntime) -> None:
    """还原 _woke_main_agent 并清理保存属性，防止热重载后叠层或引用环残留。"""
    cron_mgr = getattr(runtime.context, "cron_manager", None)
    if cron_mgr is None or not getattr(cron_mgr, CRON_PATCH_FLAG, False):
        return
    original = getattr(cron_mgr, CRON_ORIGINAL_ATTR, None)
    if (
        getattr(original, "__self__", None) is cron_mgr
        and hasattr(type(cron_mgr), "_woke_main_agent")
    ):
        if "_woke_main_agent" in getattr(cron_mgr, "__dict__", {}):
            delattr(cron_mgr, "_woke_main_agent")
    elif original is not None:
        cron_mgr._woke_main_agent = original
    if hasattr(cron_mgr, CRON_ORIGINAL_ATTR):
        delattr(cron_mgr, CRON_ORIGINAL_ATTR)
    setattr(cron_mgr, CRON_PATCH_FLAG, False)


async def run_cron_on_master(
    runtime: IrmiaRuntime,
    original: Callable[..., Awaitable[None]],
    *,
    master_umo: str,
    message: str,
    session_str: Any,
    extras: dict[str, Any],
    delivery_session_str: str,
    cfg: IrmiaConfig | None = None,
) -> None:
    """在主脑历史上跑定时任务，末尾持主脑锁重读最新历史后追加 [CronJob] 小结。"""
    from astrbot.core.agent.runners.base import AgentState
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.utils.config_number import coerce_int_config

    ctx = runtime.context
    logger = runtime.logger
    try:
        session = (
            session_str
            if isinstance(session_str, MessageSession)
            else MessageSession.from_str(session_str)
        )
    except Exception as exc:  # noqa: BLE001 - 与核心一致，非法会话只记日志
        logger.error(f"[IRMIA] 定时任务会话非法: {exc}")
        return

    # 与核心一致地构造事件：session、extras、plugins_name、role 全部保留
    cron_event = CronMessageEvent(
        context=ctx,
        session=session,
        message=message,
        extras=extras,
        message_type=session.message_type,
    )
    core_cfg = ctx.get_config(umo=cron_event.unified_msg_origin)
    enabled_plugins_name = core_cfg.get("plugin_set", ["*"])
    cron_event.plugins_name = (
        None if enabled_plugins_name == ["*"] else enabled_plugins_name
    )
    cron_payload = extras.get("cron_payload", {}) or {}
    admin_ids = core_cfg.get("admins_id", [])
    if admin_ids:
        cron_event.role = (
            "admin" if cron_payload.get("sender_id") in admin_ids else "member"
        )
    if cron_payload.get("origin", "tool") == "api":
        cron_event.role = "admin"

    req = ProviderRequest()
    await runtime._bind_master_conversation(master_umo, req)
    if req.conversation is None:
        logger.warning("[IRMIA] 定时任务未能绑定主脑历史，本次交回 AstrBot 原生流程")
        await original(
            message=message,
            session_str=session_str,
            extras=extras,
            delivery_session_str=delivery_session_str,
        )
        return
    req.prompt = build_cron_user_prompt(message, extras, delivery_session_str)

    misc_cfg = (core_cfg.get("agent_runner", {}).get("config", {}) or {}).get("misc", {}) or {}
    agent_max_step = coerce_int_config(
        misc_cfg.get("max_steps", 128),
        default=128,
        min_value=1,
        field_name="agent_runner.config.misc.max_steps",
    )
    eff_cfg = cfg if cfg is not None else runtime.cfg()
    runner = await runtime.run_master_agent_turn(
        cron_event,
        req=req,
        max_steps=agent_max_step,
        cfg=eff_cfg,
        scene="定时任务",
        attach_send_tool=bool(delivery_session_str),
        core_cfg=core_cfg,
    )
    if runner is None:
        raise RuntimeError("Failed to build main agent for cron job.")

    llm_resp = runner.get_final_llm_resp()
    if runner.state == AgentState.ERROR:
        detail = (
            f": {llm_resp.completion_text}"
            if llm_resp and llm_resp.completion_text
            else ""
        )
        raise RuntimeError(f"Cron agent run ended in ERROR state{detail}")

    cron_meta = extras.get("cron_job", {}) or {}
    summary_note = (
        f"[CronJob] {cron_meta.get('name') or cron_meta.get('id', 'unknown')}: "
        f"{cron_meta.get('description', '')} "
        f" triggered at {cron_meta.get('run_started_at', 'unknown time')}, "
    )
    if llm_resp and llm_resp.role == "assistant":
        summary_note += (
            f"I finished this job, here is the result: {llm_resp.completion_text}"
        )

    wrote = await runtime.persist_master_summary(
        master_umo, cron_event, req, summary_note, scene="定时任务"
    )
    if wrote:
        logger.info(
            f"[IRMIA 定时任务] {cron_meta.get('name') or cron_meta.get('id')} 已写回主脑历史"
        )
