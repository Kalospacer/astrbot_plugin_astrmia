"""活跃注意力门卫：状态机与熔断、固定窗防抖与竞态、四协议适配、端到端合流。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from irmia.active_gate import (
    AUTO_WAKE_COOLDOWN_SEC,
    ActiveAttentionGate,
    DecisionEvidence,
    parse_llm_probability,
)
from irmia.config import load_irmia_config
from irmia.runtime import IrmiaRuntime
from test_runtime_hooks import FakeContext, FakeEvent, FakeReq

GROUP = "aiocqhttp:GroupMessage:888888"
MASTER = "aiocqhttp:FriendMessage:114514"
LOG = logging.getLogger("test")


def _cfg(**active: Any):
    return load_irmia_config({"active_options": active})


def _evidence(image_paths: list[str] | None = None) -> DecisionEvidence:
    return DecisionEvidence(
        group_name="快乐开黑群",
        bot_last_text="[@114514]中午吃的啥",
        background=[{"from": "路人乙", "text": "今天好热"}],
        new_messages=[{"from": "哥哥", "text": "吃的牛肉饭", "tags": ["刚才被@对象"]}],
        image_paths=image_paths or [],
    )


async def _no_provider():
    return None


# 状态机与熔断


def test_state_machine_window_merge_and_fuse() -> None:
    """发言激活窗口、15 秒内分段合并并提取 @ 对象；连放 5 轮熔断，显式唤醒清零。"""
    gate = ActiveAttentionGate(LOG)
    assert gate.is_active(GROUP, 100.0, 5) is False

    gate.mark_bot_spoke(GROUP, "第一段", 100.0, 0)
    assert gate.state(GROUP) is None

    gate.mark_bot_spoke(GROUP, "[@114514]第一段", 100.0, 300)
    gate.mark_bot_spoke(GROUP, "第二段[@222]", 110.0, 300)
    st = gate.state(GROUP)
    assert st.last_bot_text == "[@114514]第一段 第二段[@222]"
    assert st.last_at_targets == {"114514", "222"}
    assert gate.is_active(GROUP, 409.0, 5) and not gate.is_active(GROUP, 411.0, 5)

    gate.mark_bot_spoke(GROUP, "过了很久的新发言", 200.0, 300)
    assert st.last_bot_text == "过了很久的新发言" and st.last_at_targets == set()

    now = 210.0
    for _ in range(5):
        now += AUTO_WAKE_COOLDOWN_SEC
        assert gate.commit_auto_wake(GROUP, 205.0, now, 5)
    assert gate.is_active(GROUP, now, 5) is False

    gate.note_explicit_wake(GROUP, "114514", now)
    assert gate.is_active(GROUP, now, 5)
    assert st.last_partner_id == "114514"
    assert gate.commit_auto_wake(GROUP, now - 1, now + 60, 5) is False


@pytest.mark.asyncio
async def test_fixed_debounce_window_single_leader_and_explicit_yield() -> None:
    """窗口内连发 3 条（含纯图片）只有最后一条带走整批；等待期间显式唤醒则退让。"""
    gate = ActiveAttentionGate(LOG)
    gate.mark_bot_spoke(GROUP, "在吗", 0.0, 10**10)
    items = [{"pmid": "a", "text": "哈哈"}, {"pmid": "b", "text": "[图片]"}, {"pmid": "c", "text": "对"}]

    async def _send(idx: int):
        await asyncio.sleep(idx * 0.01)
        return await gate.collect_batch(GROUP, items[idx], 0.08)

    results = await asyncio.gather(*(_send(i) for i in range(3)))
    leaders = [r for r in results if r is not None]
    assert len(leaders) == 1 and results[2] is not None
    batch, opened_at = leaders[0]
    assert [it["pmid"] for it in batch] == ["a", "b", "c"]
    assert gate.may_auto_wake(GROUP, opened_at, opened_at + 1, 5)

    gate.note_explicit_wake(GROUP, "333", opened_at + 0.5)
    assert gate.may_auto_wake(GROUP, opened_at, opened_at + 1, 5) is False


# 四协议适配


class Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def _gate_with(responses: list[httpx.Response]) -> tuple[ActiveAttentionGate, Recorder]:
    rec = Recorder(responses)
    return ActiveAttentionGate(LOG, transport=httpx.MockTransport(rec)), rec


@pytest.mark.asyncio
async def test_jev_protocol_bearer_noul_and_429_retry(monkeypatch) -> None:
    """jev：/v1/systemone + Bearer（留空回退环境变量）+ noul，429 后按 retry-after 重试。"""
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    gate, rec = _gate_with(
        [
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(200, json={"answers": {"should_reply": {"noul": 0.8}}}),
        ]
    )
    result = await gate.decide(_evidence(), _cfg(protocol="jev"), provider_resolver=_no_provider)
    await gate.close()

    assert result.triggered is True and result.probability == 0.8
    assert len(rec.requests) == 2
    req = rec.requests[-1]
    assert str(req.url) == "https://api.typesafe.ai/v1/systemone"
    assert req.headers["authorization"] == "Bearer env-key"
    body = json.loads(req.content)
    assert body["model"] == "jev-1.13.0"
    assert body["questions"]["should_reply"]["type"] == "noul"
    assert body["state"]["new_messages"][0]["text"] == "吃的牛肉饭"


@pytest.mark.asyncio
async def test_gemini_decision_protocol_with_kept_image(tmp_path) -> None:
    """gemini_decision：/v1beta/decisions + x-goog-api-key + noul（兼容反代 value），缺图自动跳过。"""
    image = tmp_path / "kept.jpg"
    image.write_bytes(b"jpeg-bytes")
    missing = tmp_path / "cleaned_up.jpg"
    gate, rec = _gate_with(
        [
            httpx.Response(200, json={"answers": {"should_reply": {"noul": 0.3, "confidence": 0.9}}}),
            httpx.Response(200, json={"answers": {"should_reply": {"value": 0.85, "confidence": 0.9}}}),
        ]
    )
    cfg = _cfg(protocol="gemini_decision", api_key="g-key", api_base="https://proxy.example/")
    result = await gate.decide(
        _evidence([str(missing), str(image)]), cfg, provider_resolver=_no_provider
    )
    fallback_result = await gate.decide(_evidence(), cfg, provider_resolver=_no_provider)
    await gate.close()

    assert result.triggered is False and result.probability == 0.3
    assert fallback_result.triggered is True and fallback_result.probability == 0.85
    req = rec.requests[0]
    assert str(req.url) == "https://proxy.example/v1beta/decisions"
    assert req.headers["x-goog-api-key"] == "g-key" and "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["model"] == "gemini-decision-1"
    assert body["questions"]["should_reply"]["type"] == "noul"
    assert body["input"][0]["type"] == "text"
    assert len(body["input"]) == 2
    assert body["input"][1] == {
        "type": "image",
        "mime_type": "image/jpeg",
        "data": base64.b64encode(b"jpeg-bytes").decode(),
    }


@pytest.mark.asyncio
async def test_openai_decision_protocol_predicate_array(tmp_path) -> None:
    """openai_decision：/v1/decisions + Bearer + predicate 数组 + probability，图片走 input_image。"""
    image = tmp_path / "kept.jpg"
    image.write_bytes(b"img")
    gate, rec = _gate_with(
        [
            httpx.Response(
                200,
                json={"answers": [{"name": "other", "probability": 0.1}, {"name": "should_reply", "probability": 0.75}]},
            )
        ]
    )
    cfg = _cfg(protocol="openai_decision", api_key="o-key", model="gpt-custom")
    result = await gate.decide(_evidence([str(image)]), cfg, provider_resolver=_no_provider)
    await gate.close()

    assert result.triggered is True and result.probability == 0.75
    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/decisions"
    assert req.headers["authorization"] == "Bearer o-key"
    body = json.loads(req.content)
    assert body["model"] == "gpt-custom"
    assert body["questions"] == [
        {"name": "should_reply", "type": "predicate", "instructions": body["questions"][0]["instructions"]}
    ]
    content = body["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1] == {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(b'img').decode()}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [httpx.Response(500, text="boom")],
        [httpx.Response(200, json={"answers": {"should_reply": {"noul": 1.7}}})],
        [httpx.Response(529, headers={"retry-after": "30"})],
    ],
)
async def test_http_protocol_failures_fail_closed(responses) -> None:
    """HTTP 错误、越界概率、重试预算耗尽一律按不回复处理。"""
    gate, _ = _gate_with(responses)
    result = await gate.decide(
        _evidence(), _cfg(protocol="jev", api_key="k"), provider_resolver=_no_provider
    )
    await gate.close()
    assert result.triggered is False and result.probability is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion, expected",
    [
        ('好的 {"reply": true, "probability": 0.9, "reason": "在回答"}', True),
        ('{"reply": true}', True),
        ('{"reply": false, "probability": 0.2}', False),
        ("我觉得应该回", False),
        (RuntimeError("provider down"), False),
    ],
)
async def test_llm_protocol_json_and_fail_closed(completion, expected) -> None:
    """llm：纯文本提示词、JSON 解析概率或 reply，异常与乱码按不回复处理。"""
    prompts: list[str] = []

    class _Provider:
        async def text_chat(self, prompt: str, session_id: str, persist: bool):
            assert persist is False
            prompts.append(prompt)
            if isinstance(completion, Exception):
                raise completion
            return SimpleNamespace(completion_text=completion)

    async def _resolve():
        return _Provider()

    gate = ActiveAttentionGate(LOG)
    result = await gate.decide(_evidence(), _cfg(protocol="llm"), provider_resolver=_resolve)
    assert result.triggered is expected
    assert "吃的牛肉饭" in prompts[0] and "刚才被@对象" in prompts[0]
    assert parse_llm_probability('{"probability": 0.4}') == 0.4


# 端到端合流


@dataclass
class _ToolContent:
    text: str


@dataclass
class _ToolResult:
    content: list[_ToolContent] = field(default_factory=list)


@pytest.mark.asyncio
async def test_active_followup_merges_into_group_wake(tmp_path, monkeypatch) -> None:
    """心跳在群里 @ 哥哥提问 → 哥哥不带 @ 回复 → 门卫放行合流，通知卡动静拆分；非活跃群零调用。"""
    ctx = FakeContext()
    rt = IrmiaRuntime(
        ctx,
        {
            "master_umo": MASTER,
            "heartbeat_enabled": False,
            "group_batch_window_sec": 0,
            "topic_summary_enabled": False,
            "image_keep_days": 0,
            "read_channel_image_enabled": False,
            "contacts_map": ["114514 = 哥哥"],
            "active_options": {"debounce_sec": 0, "protocol": "llm"},
        },
        LOG,
        data_dir=tmp_path,
    )
    prompts: list[str] = []

    class _Provider:
        async def text_chat(self, prompt: str, session_id: str, persist: bool):
            prompts.append(prompt)
            return SimpleNamespace(completion_text='{"reply": true, "probability": 0.9}')

    async def _resolve(umo: str, provider_id: str):
        return _Provider()

    monkeypatch.setattr(rt.topic_summarizer, "resolve_provider", _resolve)
    await rt.initialize()
    try:
        # 未发言的群：O(1) 拦截，不调判定模型
        quiet = FakeEvent("aiocqhttp:GroupMessage:777", "随便聊聊", sender_id="333")
        quiet.message_obj.message_id = "quiet_1"
        await rt.on_incoming_event(quiet)
        assert quiet.stopped is True and prompts == []

        first = FakeEvent(GROUP, "群里第一条", sender_id="222")
        first.message_obj.message_id = "grp_0"
        await rt.on_incoming_event(first)

        wake_event = FakeEvent(MASTER, "心跳", private=True)
        await rt.on_llm_tool_respond(
            wake_event,
            SimpleNamespace(name="send_message_to_user"),
            {"messages": [
                {"type": "mention_user", "mention_user_id": "114514"},
                {"type": "plain", "text": "中午吃的啥"},
            ]},
            _ToolResult([_ToolContent(f"Message sent to session {GROUP}")]),
        )
        assert rt.active_gate.is_active(GROUP, time.time(), 5)

        reply = FakeEvent(GROUP, "吃的牛肉饭", sender_id="114514", sender_name="Kalo")
        reply.message_obj.message_id = "grp_reply"
        await rt.on_incoming_event(reply)

        assert reply.stopped is False and reply.is_wake is True
        meta = reply.get_extra("irmia_mention_meta")
        assert meta["trigger_kind"] == "followup" and meta["trigger_pmids"] == ["grp_reply"]
        assert "刚才被@对象" in prompts[0] and "中午吃的啥" in prompts[0]

        req = FakeReq(prompt="吃的牛肉饭")
        await rt.on_llm_request(reply, req)
        assert "活跃对话跟进（哥哥）" in req.prompt and "吃的牛肉饭" in req.prompt
        assert "群里第一条" not in req.prompt
        temp = req.extra_user_content_parts[0]
        assert temp._no_save is True and "无需再次 read_channel" in temp.text

        # 主脑选择沉默：[SILENT] 不记账，也不续期活跃窗口
        before_ts = rt.active_gate.state(GROUP).last_bot_ts
        await rt.on_llm_response(reply)
        reply._result = SimpleNamespace(
            chain=["[SILENT]"],
            get_plain_text=lambda: "[SILENT]",
            is_llm_result=lambda: True,
            is_model_result=lambda: True,
        )
        await rt.on_decorating_result(reply)
        await rt.on_after_message_sent(reply)
        assert rt.active_gate.state(GROUP).last_bot_ts == before_ts
    finally:
        await rt.terminate()
