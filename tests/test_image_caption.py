"""测试图片描述的统一口径：共用 AstrBot 提示词、按配置截断、换压缩后仍命中缓存。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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
    """换了压缩参数后 dHash 仍能命中旧缓存，改档名不废老数据。"""
    vstore = VisionStore(tmp_path / "vision.db", tmp_path / "kept")
    vstore.open()
    vstore.put_desc("sha-new", "brief", "", "abcdef0123456789", "model", "旧的一句话")
    hit = vstore.find_desc("sha-other", KIND_FULL, "", "abcdef0123456789")
    assert hit == "旧的一句话"
    strict = vstore.find_desc("sha-other", KIND_FULL, "", "abcdef0123456789", False)
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
        assert LONG_CAPTION[:80] in wide
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
