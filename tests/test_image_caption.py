"""测试图片描述的统一口径：共用 AstrBot 提示词、按配置截断、换压缩后仍命中缓存。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from irmia.read_channel_tool import execute_read_channel
from irmia.store import IrmiaStore
from irmia.vision import VisionStore
from irmia.vision_describe import (
    KIND_FULL,
    build_qa_prompt,
    extract_captions_from_text,
    read_core_caption_prompt,
    read_core_caption_provider_id,
    render_with_captions,
    sanitize_caption_text,
    truncate_caption,
)

UMO = "aiocqhttp:GroupMessage:606"
LONG_CAPTION = "# Image Type 手机截图 # OCR Text " + "细节" * 200


class _FakeConfig(dict):
    """模拟 AstrBotConfig：直接用 dict 的 get。"""


class _FakeContext:
    """只提供 get_config，够测提示词与模型 id 的取法。"""

    def __init__(self, settings: dict) -> None:
        self._settings = settings

    def get_config(self, umo: str | None = None) -> _FakeConfig:
        return _FakeConfig(provider_settings=self._settings)


def test_core_prompt_is_shared_with_astrbot() -> None:
    """提示词与模型 id 都从 AstrBot 的 provider_settings 里取。"""
    ctx = _FakeContext(
        {
            "image_caption_prompt": "随便描述一下这张图",
            "default_image_caption_provider_id": "CPA2/vision",
        }
    )
    assert read_core_caption_prompt(ctx) == "随便描述一下这张图"
    assert read_core_caption_provider_id(ctx) == "CPA2/vision"


def test_core_prompt_falls_back_when_missing() -> None:
    """AstrBot 没配时用官方默认那句，不抛异常。"""

    class _Broken:
        def get_config(self, umo: str | None = None) -> object:
            raise RuntimeError("no config")

    assert "Please describe the image" in read_core_caption_prompt(_Broken())
    assert read_core_caption_prompt(_FakeContext({})).startswith("Please describe")
    assert read_core_caption_provider_id(_FakeContext({})) == ""


def test_qa_prompt_keeps_base_template() -> None:
    """带问题的提问要在同一份模板后面追加，不另起一套。"""
    prompt = build_qa_prompt("基础模板", "图里有几个人")
    assert prompt.startswith("基础模板")
    assert "图里有几个人" in prompt


def test_truncate_and_sanitize() -> None:
    """按字数裁并补省略号；方括号换全角，避免撑坏占位符。"""
    assert truncate_caption("abcdef", 3) == "ab…"
    assert truncate_caption("abc", 10) == "abc"
    assert truncate_caption("abcdef", 0) == "abcdef"
    assert sanitize_caption_text("a[b]c\nd") == "a［b］c d"
    assert extract_captions_from_text("[图片: 白猫][图片: 黑狗] 尾巴") == ["白猫", "黑狗"]


def test_render_truncates_both_bare_and_described() -> None:
    """裸占位符用识别结果，已带描述的按配置再裁一刀。"""
    bare = render_with_captions("[图片] 看这个", ["很长的描述" * 30], 20)
    assert bare.startswith("[图片: ") and bare.endswith("] 看这个")
    assert len(bare) < 60
    described = render_with_captions(f"[图片: {'长' * 100}] 后面", None, 12)
    assert described.startswith("[图片: ")
    assert described.endswith("] 后面")
    assert "长" * 100 not in described


def test_vision_store_hits_old_brief_cache(tmp_path: Path) -> None:
    """同一原图可复用旧简述，另一张图不能近似命中全文缓存。"""
    vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
    vstore.open()
    vstore.put_desc("sha-new", "brief", "", "abcdef0123456789", "model", "旧的一句话")
    hit = vstore.find_desc("sha-new", KIND_FULL, "", "abcdef0123456789")
    assert hit == "旧的一句话"
    assert vstore.find_desc("sha-other", KIND_FULL, "", "abcdef0123456789") is None
    strict = vstore.find_desc("sha-new", KIND_FULL, "", "abcdef0123456789", False)
    assert strict is None
    vstore.close()


def test_read_channel_uses_config_chars(tmp_path: Path) -> None:
    """读群展示原文行时按 image_brief_max_chars 裁描述，完整描述仍在账本里。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "chars")
        await store.open()
        await store.ensure_message_recorded(
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="606",
            sender_id="1",
            sender_name="阿甲",
            text="热身一句",
            ts=1700000000.0,
        )
        await execute_read_channel(store=store, sid=UMO)
        await store.ensure_message_recorded(
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="606",
            sender_id="2",
            sender_name="阿乙",
            text=f"[图片: {LONG_CAPTION}] 这单好多钱",
            ts=1700000100.0,
        )
        tight = await execute_read_channel(
            store=store, sid=UMO, image_brief_max_chars=24
        )
        assert "这单好多钱" in tight
        assert LONG_CAPTION not in tight
        line = next(row for row in tight.splitlines() if "[图片:" in row)
        assert len(line) < 60

        await store.ensure_message_recorded(
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="606",
            sender_id="3",
            sender_name="阿丙",
            text=f"[图片: {LONG_CAPTION}]",
            ts=1700000200.0,
        )
        wide = await execute_read_channel(
            store=store, sid=UMO, image_brief_max_chars=200
        )
        assert "# Image Type" not in wide and "# OCR Text" not in wide
        assert "手机截图｜" + "细节" * 40 in wide
        await store.close()

    asyncio.run(_run())


def test_topic_keeps_full_caption_for_summary(tmp_path: Path) -> None:
    """概括话题用的行不裁图片描述，让小模型自己压。"""
    from irmia.config import load_irmia_config
    from irmia.topic import TopicSummarizer

    captured: list[str] = []

    class _Provider:
        async def text_chat(self, prompt: str, session_id: str, persist: bool) -> object:
            captured.append(prompt)
            return SimpleNamespace(completion_text='{"topic": "在聊截图"}')

    class _Captioner:
        async def caption_rows(self, rows, *, wait_sec, max_new):
            return {rows[0].id: [LONG_CAPTION]}

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "topic")
        await store.open()
        await store.ensure_message_recorded(
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="606",
            sender_id="1",
            sender_name="阿甲",
            text="[图片]",
            ts=1700000000.0,
            platform_message_id="p-1",
        )
        rows = store.fetch_messages(UMO, limit=10)
        summarizer = TopicSummarizer(store, SimpleNamespace(), SimpleNamespace(debug=lambda *_: None))
        summarizer.captioner = _Captioner()  # type: ignore[assignment]
        has_text, topic = await summarizer._summarize_rows(
            rows,
            load_irmia_config({"topic_options": {"line_max_chars": 20}}),
            _Provider(),
            lambda m: "阿甲",
        )
        assert has_text and topic == "在聊截图"
        assert LONG_CAPTION[:200] in captured[0]
        await store.close()

    asyncio.run(_run())


@pytest.mark.parametrize("allow_brief", [False, True])
def test_full_caption_cache_rejects_similar_images(
    tmp_path: Path, allow_brief: bool
) -> None:
    """完整描述只能精确命中原图，不能拿相似截图中的旧金额代替。"""
    vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
    vstore.open()
    try:
        vstore.put_desc(
            "original", KIND_FULL, "", "0000000000000909", "model", "金额 100.00"
        )
        assert (
            vstore.find_desc("other", KIND_FULL, "", "000000000000050d", allow_brief)
            is None
        )
        assert (
            vstore.find_desc("original", KIND_FULL, "", "0000000000000909", allow_brief)
            == "金额 100.00"
        )
    finally:
        vstore.close()


@pytest.mark.parametrize("first_result", ["error", "empty", "success", "all_failed"])
def test_caption_provider_chain_falls_back(tmp_path: Path, first_result: str) -> None:
    """模型存在但调用失败或返回空白时，继续尝试去重后的下一候选。"""
    from unittest.mock import AsyncMock
    from irmia.config import IrmiaConfig
    from irmia.vision import KeptImage
    from irmia.vision_describe import Captioner

    async def _run() -> None:
        first = (
            AsyncMock(side_effect=RuntimeError("unavailable"))
            if first_result in ("error", "all_failed")
            else AsyncMock(
                return_value=SimpleNamespace(
                    completion_text="首选结果" if first_result == "success" else ""
                )
            )
        )
        second = (
            AsyncMock(side_effect=RuntimeError("also unavailable"))
            if first_result == "all_failed"
            else AsyncMock(return_value=SimpleNamespace(completion_text="后备结果"))
        )
        providers = {
            "first": SimpleNamespace(
                text_chat=first, meta=lambda: SimpleNamespace(id="first")
            ),
            "second": SimpleNamespace(
                text_chat=second, meta=lambda: SimpleNamespace(id="second")
            ),
        }
        context = SimpleNamespace(
            get_config=lambda umo=None: {
                "provider_settings": {"default_image_caption_provider_id": "first"}
            },
            get_provider_by_id=providers.get,
            get_using_provider_async=AsyncMock(return_value=providers["second"]),
        )
        cfg = IrmiaConfig(vision_provider_ids=["first", "second", "second"])
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        try:
            captioner = Captioner(
                vstore,
                context,
                lambda: cfg,
                asyncio.create_task,
                SimpleNamespace(debug=lambda *_: None),
            )
            kept = KeptImage(0, "original", "", str(tmp_path / "image.jpg"))
            got = await captioner.describe_for_tool(UMO, kept, "")

            expected = {"success": "首选结果", "all_failed": ""}.get(
                first_result, "后备结果"
            )
            assert got == expected
            assert first.await_count == (1 if first_result == "success" else 2)
            assert second.await_count == (
                0
                if first_result == "success"
                else 2
                if first_result == "all_failed"
                else 1
            )
            if first_result == "all_failed":
                assert vstore.stats()[1] == 0
        finally:
            vstore.close()

    asyncio.run(_run())


@pytest.mark.parametrize("index", [1, 2])
def test_read_image_uses_full_caption_for_selected_image(
    tmp_path: Path, index: int
) -> None:
    """账本文本含方括号和混合占位符时，仍按图片编号返回完整精确缓存。"""
    from unittest.mock import AsyncMock
    from irmia.config import IrmiaConfig
    from irmia.read_image_tool import execute_read_image
    from irmia.vision_describe import Captioner

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "ledger")
        await store.open()
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        context = SimpleNamespace(
            get_using_provider_async=AsyncMock(
                side_effect=AssertionError("应使用精确缓存")
            )
        )
        captioner = Captioner(
            vstore,
            context,
            IrmiaConfig,
            asyncio.create_task,
            SimpleNamespace(debug=lambda *_: None),
        )
        try:
            msg_id = await store.ensure_message_recorded(
                umo=UMO,
                text="[图片: 截图包含 [SUCCESS] 后面还有重要内容] [图片]",
                platform_message_id="p-images",
            )
            for idx in range(2):
                path = tmp_path / f"image-{idx}.jpg"
                path.write_bytes(b"cached image; no decoder required")
                vstore.add_kept(UMO, "p-images", idx, f"sha-{idx}", "", str(path))
                vstore.put_desc(
                    f"sha-{idx}",
                    KIND_FULL,
                    "",
                    "",
                    "model",
                    f"第 {idx + 1} 张完整描述 [SUCCESS] 后面还有重要内容",
                )
            out = await execute_read_image(
                store=store,
                vstore=vstore,
                captioner=captioner,
                sid=UMO,
                msg_id=msg_id,
                index=index,
                keep_days=3,
            )

            assert out == f"第 {index} 张完整描述 [SUCCESS] 后面还有重要内容"
            context.get_using_provider_async.assert_not_awaited()
        finally:
            vstore.close()
            await store.close()

    asyncio.run(_run())


def test_vision_zero_config_bounds_and_cleanup_never_expires(tmp_path: Path) -> None:
    """cache_ttl_days 与 read_max_chars 允许填 0，且 ttl=0 时清理不删旧描述。"""
    import time

    from irmia.config import load_irmia_config

    cfg = load_irmia_config(
        {"vision_options": {"cache_ttl_days": 0, "read_max_chars": 0}}
    )
    assert cfg.vision_cache_ttl_days == 0
    assert cfg.read_image_max_chars == 0

    fallback = load_irmia_config(
        {"vision_options": {"cache_ttl_days": -1, "read_max_chars": False}}
    )
    assert fallback.vision_cache_ttl_days == 30
    assert fallback.read_image_max_chars == 500

    vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
    vstore.open()
    try:
        vstore.put_desc("sha-old", KIND_FULL, "", "", "model", "很久之前的描述")
        vstore._conn.execute(
            "UPDATE image_desc SET last_hit_at=? WHERE sha256=?",
            (time.time() - 60 * 86400, "sha-old"),
        )
        vstore._conn.commit()
        _, _, removed_desc = vstore.cleanup(keep_days=3, desc_ttl_days=0)
        assert removed_desc == 0
        assert vstore.find_desc("sha-old", KIND_FULL) == "很久之前的描述"
    finally:
        vstore.close()


@pytest.mark.parametrize("question", ["", "图里写了啥"])
def test_describe_for_tool_respects_read_max_chars(
    tmp_path: Path, question: str
) -> None:
    """read_image 超长时按 read_max_chars 本地分页续读，设为 0 返回纯原文且不破坏缓存。"""
    from irmia.config import IrmiaConfig
    from irmia.vision import KeptImage
    from irmia.vision_describe import KIND_QA, Captioner

    async def _run() -> None:
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        long_text = "图内正文" * 200
        kind = KIND_QA if question else KIND_FULL
        vstore.put_desc("sha-long", kind, question, "", "model", long_text)
        cfg = IrmiaConfig(read_image_max_chars=500)
        captioner = Captioner(
            vstore,
            SimpleNamespace(),
            lambda: cfg,
            asyncio.create_task,
            SimpleNamespace(debug=lambda *_: None),
        )
        try:
            kept = KeptImage(0, "sha-long", "", str(tmp_path / "x.jpg"))
            page1 = await captioner.describe_for_tool(UMO, kept, question, offset=0)
            body1, footer1 = page1.split("\n", 1)
            assert body1 == long_text[:500]
            assert "第 1~500 / 共 800 字" in footer1
            assert "offset=500" in footer1
            assert ("question=" in footer1) is (not question)

            page2 = await captioner.describe_for_tool(UMO, kept, question, offset=500)
            body2, footer2 = page2.split("\n", 1)
            assert body1 + body2 == long_text
            assert "第 501~800 / 共 800 字，已到末尾" in footer2

            overflow = await captioner.describe_for_tool(
                UMO, kept, question, offset=900
            )
            assert "已超出" in overflow and "800" in overflow

            cfg.read_image_max_chars = 0
            full = await captioner.describe_for_tool(UMO, kept, question)
            assert full == long_text
            assert vstore.find_desc("sha-long", kind, question, "", False) == long_text
        finally:
            vstore.close()

    asyncio.run(_run())


def test_condense_caption_for_brief_preserves_content() -> None:
    """行内预览剥离 Markdown 标题壳与强调符，不误伤正文英文词，纯中文与 max_chars=0 原样保留。"""
    from irmia.vision_describe import condense_caption_for_brief

    ui_sample = (
        "# Image Type UI 界面局部截图（自动补全菜单） "
        "# OCR Text **左侧菜单：** - **re**load-plugins - **AI** 助手"
    )
    condensed = condense_caption_for_brief(ui_sample)
    assert "# Image Type" not in condensed and "# OCR Text" not in condensed
    assert "**" not in condensed
    assert condensed.startswith("UI 界面局部截图（自动补全菜单）｜左侧菜单：reload-plugins AI 助手")

    rendered_brief = render_with_captions("[图片]", [ui_sample], 80)
    assert rendered_brief.startswith("[图片: UI 界面局部截图")

    rendered_full = render_with_captions("[图片]", [ui_sample], 0)
    assert "# Image Type" in rendered_full

    plain = "一只橘猫趴在键盘上打盹"
    assert condense_caption_for_brief(plain) == plain


def test_caption_rows_cache_only_mode_skips_new_requests(tmp_path: Path) -> None:
    """max_new=0 仅复用已有缓存：未命中图保留原 [图片] 占位符，且不调用任何视觉模型。"""
    from unittest.mock import AsyncMock

    from irmia.config import IrmiaConfig
    from irmia.store import ChannelMessageRow
    from irmia.vision_describe import Captioner

    async def _run() -> None:
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        vstore.add_kept(UMO, "p-hit", 0, "sha-hit", "", str(tmp_path / "h.jpg"))
        vstore.add_kept(UMO, "p-miss", 0, "sha-miss", "", str(tmp_path / "m.jpg"))
        vstore.put_desc("sha-hit", KIND_FULL, "", "", "model", "已缓存白猫")

        spawn_calls: list[object] = []

        def _forbid_spawn(coro):
            spawn_calls.append(coro)
            raise AssertionError("cache_only 模式不应创建后台识图任务")

        context = SimpleNamespace(
            get_using_provider_async=AsyncMock(
                side_effect=AssertionError("cache_only 模式不应调用视觉模型")
            )
        )
        captioner = Captioner(
            vstore,
            context,
            IrmiaConfig,
            _forbid_spawn,
            SimpleNamespace(debug=lambda *_: None),
        )
        rows = [
            ChannelMessageRow(
                id=1,
                umo=UMO,
                platform_id="aiocqhttp",
                message_type="GroupMessage",
                session_id="606",
                platform_message_id="p-hit",
                sender_id="1",
                sender_name="甲",
                sender_card="",
                text="[图片] 看看这只猫",
                ts=1700000001.0,
                is_bot=False,
            ),
            ChannelMessageRow(
                id=2,
                umo=UMO,
                platform_id="aiocqhttp",
                message_type="GroupMessage",
                session_id="606",
                platform_message_id="p-miss",
                sender_id="2",
                sender_name="乙",
                sender_card="",
                text="[图片] 另一张新图",
                ts=1700000002.0,
                is_bot=False,
            ),
        ]
        try:
            captions = await captioner.caption_rows(rows, wait_sec=0, max_new=0)
            assert captions == {1: ["已缓存白猫"], 2: [""]}
            assert render_with_captions(rows[0].text, captions[1], 0) == "[图片: 已缓存白猫] 看看这只猫"
            assert render_with_captions(rows[1].text, captions[2], 0) == "[图片] 另一张新图"
            assert not spawn_calls
        finally:
            vstore.close()

    asyncio.run(_run())


def test_topic_summarizer_reads_cache_only_for_images(tmp_path: Path) -> None:
    """话题概括调用 caption_rows 时严格传 wait_sec=0, max_new=0，不触发新识图。"""
    from irmia.config import load_irmia_config
    from irmia.topic import TopicSummarizer

    recorded_kwargs: list[dict] = []

    class _SpyCaptioner:
        async def caption_rows(self, rows, *, wait_sec, max_new):
            recorded_kwargs.append({"wait_sec": wait_sec, "max_new": max_new})
            return {rows[0].id: [""]}

    class _Provider:
        async def text_chat(self, prompt: str, session_id: str, persist: bool) -> object:
            assert "[图片]" in prompt and "还没看清" not in prompt
            return SimpleNamespace(completion_text='{"topic": "发图闲聊"}')

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "topic_cache_only")
        await store.open()
        try:
            await store.ensure_message_recorded(
                umo=UMO, text="[图片] 晚安", platform_message_id="p-1"
            )
            rows = store.fetch_messages(UMO, limit=10)
            summarizer = TopicSummarizer(
                store, SimpleNamespace(), SimpleNamespace(debug=lambda *_: None)
            )
            summarizer.captioner = _SpyCaptioner()  # type: ignore[assignment]
            has_text, topic = await summarizer._summarize_rows(
                rows, load_irmia_config({}), _Provider(), lambda m: "甲"
            )
            assert has_text and topic == "发图闲聊"
            assert recorded_kwargs == [{"wait_sec": 0, "max_new": 0}]
        finally:
            await store.close()

    asyncio.run(_run())


def test_execute_read_image_passes_offset_and_clamps_negative(tmp_path: Path) -> None:
    """execute_read_image 透传 offset 续读本地缓存并将负数归零，不触发新识图。"""
    from unittest.mock import AsyncMock

    from irmia.config import IrmiaConfig
    from irmia.read_image_tool import execute_read_image
    from irmia.vision_describe import Captioner

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "ledger")
        await store.open()
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        context = SimpleNamespace(
            get_using_provider_async=AsyncMock(
                side_effect=AssertionError("续读缓存不应调用视觉模型")
            )
        )
        cfg = IrmiaConfig(read_image_max_chars=100)
        captioner = Captioner(
            vstore,
            context,
            lambda: cfg,
            asyncio.create_task,
            SimpleNamespace(debug=lambda *_: None),
        )
        try:
            msg_id = await store.ensure_message_recorded(
                umo=UMO, text="[图片]", platform_message_id="p-page"
            )
            path = tmp_path / "page.jpg"
            path.write_bytes(b"img")
            vstore.add_kept(UMO, "p-page", 0, "sha-page", "", str(path))
            full_text = "A" * 100 + "B" * 60
            vstore.put_desc("sha-page", KIND_FULL, "", "", "model", full_text)

            p1 = await execute_read_image(
                store=store,
                vstore=vstore,
                captioner=captioner,
                sid=UMO,
                msg_id=msg_id,
                offset=-10,
                keep_days=3,
            )
            assert p1.startswith("A" * 100 + "\n") and "offset=100" in p1

            p2 = await execute_read_image(
                store=store,
                vstore=vstore,
                captioner=captioner,
                sid=UMO,
                msg_id=msg_id,
                offset=100,
                keep_days=3,
            )
            assert p2.startswith("B" * 60 + "\n") and "已到末尾" in p2
            context.get_using_provider_async.assert_not_awaited()
        finally:
            vstore.close()
            await store.close()

    asyncio.run(_run())


def test_caption_rows_handles_task_error_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台识图任务抛错或被取消时降级为 None，不中断读群。"""
    from irmia.config import IrmiaConfig
    from irmia.store import ChannelMessageRow
    from irmia.vision_describe import Captioner

    async def _run() -> None:
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        vstore.add_kept(UMO, "p-err", 0, "sha-err", "", str(tmp_path / "e.jpg"))
        vstore.add_kept(UMO, "p-cancel", 0, "sha-cancel", "", str(tmp_path / "c.jpg"))
        captioner = Captioner(
            vstore,
            SimpleNamespace(),
            IrmiaConfig,
            asyncio.create_task,
            SimpleNamespace(debug=lambda *_: None),
        )

        async def _broken(umo, kept, question):
            if kept.sha256 == "sha-err":
                raise RuntimeError("disk full")
            raise asyncio.CancelledError()

        monkeypatch.setattr(captioner, "_describe", _broken)
        rows = [
            ChannelMessageRow(
                id=1,
                umo=UMO,
                platform_id="aiocqhttp",
                message_type="GroupMessage",
                session_id="606",
                platform_message_id="p-err",
                sender_id="1",
                sender_name="甲",
                sender_card="",
                text="[图片]",
                ts=1700000001.0,
                is_bot=False,
            ),
            ChannelMessageRow(
                id=2,
                umo=UMO,
                platform_id="aiocqhttp",
                message_type="GroupMessage",
                session_id="606",
                platform_message_id="p-cancel",
                sender_id="2",
                sender_name="乙",
                sender_card="",
                text="[图片]",
                ts=1700000002.0,
                is_bot=False,
            ),
        ]
        try:
            got = await captioner.caption_rows(rows, wait_sec=2, max_new=6)
            assert got == {1: [None], 2: [None]}
        finally:
            vstore.close()

    asyncio.run(_run())


def test_caption_rows_aligns_mixed_described_and_bare_images(
    tmp_path: Path,
) -> None:
    """同一消息混有已描述占位符和裸占位符时，按全图下标精准对应裸槽位。"""
    from irmia.config import IrmiaConfig
    from irmia.store import ChannelMessageRow
    from irmia.vision_describe import Captioner

    async def _run() -> None:
        vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
        vstore.open()
        vstore.add_kept(UMO, "p-mix", 0, "sha-0", "", str(tmp_path / "0.jpg"))
        vstore.add_kept(UMO, "p-mix", 1, "sha-1", "", str(tmp_path / "1.jpg"))
        vstore.put_desc("sha-0", KIND_FULL, "", "", "model", "第一张缓存不应错贴")
        vstore.put_desc("sha-1", KIND_FULL, "", "", "model", "第二张橘猫")
        captioner = Captioner(
            vstore,
            SimpleNamespace(),
            IrmiaConfig,
            asyncio.create_task,
            SimpleNamespace(debug=lambda *_: None),
        )
        row = ChannelMessageRow(
            id=10,
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="606",
            platform_message_id="p-mix",
            sender_id="1",
            sender_name="甲",
            sender_card="",
            text="[图片: 第一张已有描述] [图片]",
            ts=1700000010.0,
            is_bot=False,
        )
        try:
            captions = await captioner.caption_rows([row], wait_sec=2, max_new=6)
            assert captions == {10: ["第二张橘猫"]}
            rendered = render_with_captions(row.text, captions[10], 80)
            assert rendered == "[图片: 第一张已有描述] [图片: 第二张橘猫]"
        finally:
            vstore.close()

    asyncio.run(_run())


def test_make_kept_jpeg_skips_redecode_on_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复留底同一张图时直接复用已存 JPEG，不再二次解码。"""
    from PIL import Image

    from irmia.vision import make_kept_jpeg

    src = tmp_path / "src.png"
    Image.new("RGB", (32, 32), color="red").save(src, "PNG")
    keep_root = tmp_path / "kept"

    sha1, dhash1, path1 = make_kept_jpeg(str(src), keep_root)
    assert sha1 and dhash1 == "" and Path(path1).is_file()

    def _forbid_open(*args, **kwargs):
        raise AssertionError("同图留底不应再次打开解码图片")

    monkeypatch.setattr("irmia.vision.Image.open", _forbid_open)
    sha2, dhash2, path2 = make_kept_jpeg(str(src), keep_root)
    assert (sha2, dhash2, path2) == (sha1, "", path1)
