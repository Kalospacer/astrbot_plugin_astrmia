"""插件入口：装配 IrmiaRuntime，注册钩子与管理员指令。"""

from __future__ import annotations

import sys
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.permission import PermissionType

try:
    from .irmia.runtime import IrmiaRuntime
except ImportError:  # pragma: no cover - 顶层直接导入兼容
    from irmia.runtime import IrmiaRuntime


class AstrMiaPlugin(Star):
    """AstrMia 语义核心插件主类。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.runtime = IrmiaRuntime(
            context=context,
            config=config,
            logger=logger,
        )

    async def initialize(self) -> None:
        """插件启动时初始化存储、注册工具并启动呼吸心跳。"""
        await self.runtime.initialize()

    async def terminate(self) -> None:
        """插件卸载时停止心跳、回收后台任务并关闭数据库。"""
        await self.runtime.terminate()

    # 事件钩子

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.ALL, priority=sys.maxsize - 4
    )
    async def handle_incoming_message(self, event: AstrMessageEvent) -> None:
        """AstrHe 记账后执行信箱分流、攒批与通知装配。"""
        await self.runtime.on_incoming_event(event)

    @filter.on_waiting_llm_request(priority=sys.maxsize - 4)
    async def handle_waiting_llm_request(self, event: AstrMessageEvent) -> None:
        """读历史前取主脑锁。多会话同时改写会丢历史。"""
        await self.runtime.on_waiting_llm_request(event)

    @filter.on_llm_request(priority=sys.maxsize - 4)
    async def handle_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """绑定全局主脑历史、替换群点名半遮面通知、注入此刻层与外部会话未读信箱清单。"""
        await self.runtime.on_llm_request(event, req)

    @filter.on_llm_response(priority=sys.maxsize - 4)
    async def handle_llm_response(self, event: AstrMessageEvent, resp: Any) -> None:
        """标记已进入最终回复步。"""
        await self.runtime.on_llm_response(event, resp)

    @filter.on_llm_tool_respond()
    async def handle_tool_respond(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: Any,
        tool_result: Any,
    ) -> None:
        """标记 send_message_to_user 是否已发到当前窗口，并记账。"""
        await self.runtime.on_llm_tool_respond(
            event, tool, tool_args, tool_result
        )

    @filter.on_decorating_result(priority=sys.maxsize - 4)
    async def handle_decorating_result(self, event: AstrMessageEvent) -> None:
        """处理 [SILENT] 与 intercept_direct_reply。"""
        await self.runtime.on_decorating_result(event)

    @filter.after_message_sent(priority=sys.maxsize - 4)
    async def handle_after_message_sent(self, event: AstrMessageEvent) -> None:
        """记录普通直出回复到兜底账本。"""
        await self.runtime.on_after_message_sent(event)

    # 管理员指令

    @filter.command_group("astrmia")
    def astrmia_group(self) -> None:
        """AstrMia 管理指令组。"""
        pass

    @filter.permission_type(PermissionType.ADMIN)
    @astrmia_group.command("status")
    async def cmd_astrmia_status(self, event: AstrMessageEvent):
        """查看全局主脑会话、呼吸心跳参数与外部会话未读信箱状态。"""
        report = await self.runtime.status_report()
        yield event.plain_result(report)

    @filter.command_group("irmia")
    def irmia_group(self) -> None:
        """IRMIA 管理指令组（兼容别名）。"""
        pass

    @filter.permission_type(PermissionType.ADMIN)
    @irmia_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看全局主脑会话、呼吸心跳参数与外部会话未读信箱状态。"""
        report = await self.runtime.status_report()
        yield event.plain_result(report)

