"""IRMIA 内置识图能力：dHash 判重 + vision_cache 读写 + VL 描述生成。

为什么内置而不是依赖 astrbot_plugin_irmia_vision
------------------------------------------------
唤醒时既要话题概括、又要顺手把窗口里的图片转成描述，这两件事在同一个代码路径上。
跨插件调用要么走工具注册（绕），要么走内部 import（脆），不如把核心逻辑收进来：

1. **VL 调用**不自己拼 httpx + base64，直接用 AstrBot 的 Provider.text_chat()。
   它原生支持 image_urls 参数，鉴权、代理、重试、超时都跟主链路一致，也不会
   因为换了个 provider 就失效。
2. **dHash 判重**复制「此刻的心情」插件的算法（9×8 灰度差，64bit），
   汉明距离 ≤ 阈值算同一张。比 sha256 好的地方是表情包换尺寸、加边框还能认出来。
3. **缓存**沿用 vision_cache 的表结构（`image_cache`），路径默认指回
   astrbot_plugin_irmia_vision 的库，历史 28 条记录直接能用。多一层 TTL：
   `read_at` 过期的记录查询时跳过、清理时删除。

整条链路都是 best-effort：任何异常只记日志，绝不影响话题概括和主脑回复。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

#: 默认缓存库路径：沿用 irmia_vision 插件的库，历史记录可直接复用
DEFAULT_CACHE_DB = Path(
    "/AstrBot/data/plugin_data/astrbot_plugin_irmia_vision/vision_cache.db"
)

#: 建表语句与 astrbot_plugin_irmia_vision 的 image_cache 完全一致，两边可以共用同一份库
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_cache (
    sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    phash TEXT,
    model_id TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    result_id TEXT PRIMARY KEY,
    source_value TEXT,
    summary TEXT,
    text TEXT,
    tags TEXT,
    result_json TEXT NOT NULL,
    read_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sha256 ON image_cache(sha256);
CREATE INDEX IF NOT EXISTS idx_filename ON image_cache(filename);
CREATE INDEX IF NOT EXISTS idx_phash ON image_cache(phash);
CREATE INDEX IF NOT EXISTS idx_tags ON image_cache(tags);
"""

#: dHash 相似判定阈值（汉明距离），与「此刻的心情」保持一致
DHASH_THRESHOLD = 8

#: 模糊判重时最多比对多少条已缓存记录，避免缓存膨胀后拖慢唤醒
DHASH_SCAN_LIMIT = 200

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


# ---------------------------------------------------------------------------
# dHash
# ---------------------------------------------------------------------------


def compute_dhash(path: str) -> str:
    """计算图片的 dHash（9×8 灰度相邻像素比较，64bit），失败返回空串。

    Args:
        path: 图片本地路径。

    Returns:
        16 进制哈希字符串；无法读取或 PIL 缺失时返回空串。
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("L")
            image = image.resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(image.getdata())
    except Exception:
        return ""

    bits: list[bool] = []
    width, height = 9, 8
    for row in range(height):
        for col in range(width - 1):
            left = pixels[row * width + col]
            right = left if False else pixels[row * width + col + 1]
            bits.append(left > right)
    value = 0
    for index, bit in enumerate(bits):
        if bit:
            value += 1 << index
    return hex(value)[2:]


def hamming_distance(left: str, right: str) -> int:
    """两个 dHash 之间的汉明距离；任一为空时返回 64（视为完全不同）。"""
    if not left or not right:
        return 64
    try:
        left_bits = bin(int(left, 16))[2:].zfill(64)
        right_bits = bin(int(right, 16))[2:].zfill(64)
    except ValueError:
        return 64
    return sum(a != b for a, b in zip(left_bits, right_bits))


def sha256_of_file(path: str) -> str:
    """文件 sha256（精确判重用），失败返回空串。"""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def is_image_path(path: str) -> bool:
    """判断路径是否存在且后缀是支持的图片格式。"""
    if not path or not os.path.isfile(path):
        return False
    return Path(path).suffix.lower() in SUPPORTED_IMAGE_EXTS


# 只匹配「还没写过描述」的占位符：紧跟冒号或已经带描述的都不再动，防套娃
_BARE_PLACEHOLDER_RE = re.compile(r"\[图片\](?!\s*[:：])")


def replace_bare_placeholder(text: str, caption: str, placeholder: str = "[图片]") -> str | None:
    """把 text 里第一个干净的 `[图片]` 换成 `[图片: 描述]`。

    负向前瞻保证重复处理同一条消息不会套娃；描述为空、或没有可替换的占位符时返回 None。

    Args:
        text: 账本里该条消息的现有文本。
        caption: 模型给出的图片描述。
        placeholder: 占位符字面量，默认 `[图片]`。

    Returns:
        替换后的文本；无需替换时返回 None。
    """
    clean = re.sub(r"\s+", " ", str(caption or "")).strip()
    if not clean:
        return None
    original = str(text or "")
    if not _BARE_PLACEHOLDER_RE.search(original):
        return None
    # 统一成 [图片: 描述] 的形态，冒号留在方括号里面
    described = placeholder.rstrip("]") + f": {clean}]"
    replaced = _BARE_PLACEHOLDER_RE.sub(described, original, count=1)
    return replaced if replaced != original else None


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


@dataclass
class CachedVision:
    """一条命中的识图缓存。"""

    result_id: str
    summary: str
    text: str
    read_at: str
    dhash: str
    source_value: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(raw: str) -> datetime | None:
    """解析 ISO 时间戳（兼容带时区与不带时区两种写法）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_expired(read_at: str, ttl_days: int, *, now: datetime | None = None) -> bool:
    """判断缓存记录的 read_at 是否已超过 ttl_days 天。

    ttl_days <= 0 表示不过期；时间戳解析失败时按未过期处理（宁可留着也别瞎删）。
    """
    if ttl_days <= 0:
        return False
    parsed = _parse_iso(read_at)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - parsed) > timedelta(days=ttl_days)


class VisionCache:
    """vision_cache 的读写与清理（同步 sqlite，调用方自行决定是否丢线程池）。"""

    def __init__(self, db_path: Path | str = DEFAULT_CACHE_DB) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """建库建表（idempotent），目录不存在时自动创建。"""
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(CACHE_SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def open_safe(self) -> bool:
        """打开缓存但吞掉异常，返回是否可用（给 best-effort 调用方用）。"""
        try:
            self.open()
            return True
        except Exception:
            return False

    def _conn_or_open(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        return self._conn

    # ---------------- 查询 ----------------

    def find(
        self,
        *,
        path: str = "",
        sha256: str = "",
        filename: str = "",
        dhash: str = "",
        ttl_days: int = 0,
    ) -> CachedVision | None:
        """按优先级查缓存：source_value 精确 → sha256+filename → dHash 模糊。

        Args:
            path: 图片本地路径（最精确）。
            sha256: 文件哈希。
            filename: 文件名（dHash 模糊命中时用来补位）。
            dhash: 待查图片的 dHash，用于模糊判重。
            ttl_days: 过期天数，>0 时过期记录视为未命中。

        Returns:
            命中的缓存记录；未命中返回 None。
        """
        conn = self._conn_or_open()

        def _row_to_cached(row: sqlite3.Row) -> CachedVision:
            return CachedVision(
                result_id=str(row["result_id"]),
                summary=str(row["summary"] or ""),
                text=str(row["text"] or ""),
                read_at=str(row["read_at"] or ""),
                dhash=str(row["phash"] or ""),
                source_value=str(row["source_value"] or ""),
            )

        if path:
            row = conn.execute(
                "SELECT * FROM image_cache WHERE source_value = ? "
                "ORDER BY read_at DESC LIMIT 1",
                (path,),
            ).fetchone()
            if row is not None and not is_expired(row["read_at"], ttl_days):
                self._bump(str(row["result_id"]))
                return _row_to_cached(row)

        if sha256:
            row = conn.execute(
                "SELECT * FROM image_cache WHERE sha256 = ? AND filename = ? "
                "ORDER BY read_at DESC LIMIT 1",
                (sha256, filename),
            ).fetchone()
            if row is not None and not is_expired(row["read_at"], ttl_days):
                self._bump(str(row["result_id"]))
                return _row_to_cached(row)

        if dhash:
            rows = conn.execute(
                "SELECT * FROM image_cache WHERE phash IS NOT NULL AND phash != '' "
                "ORDER BY read_at DESC LIMIT ?",
                (DHASH_SCAN_LIMIT,),
            ).fetchall()
            for row in rows:
                if is_expired(row["read_at"], ttl_days):
                    continue
                if hamming_distance(dhash, str(row["phash"] or "")) <= DHASH_THRESHOLD:
                    self._bump(str(row["result_id"]))
                    return _row_to_cached(row)

        return None

    def _bump(self, result_id: str) -> None:
        """命中一次就把 hit_count 加一、last_hit_at 更新（失败无所谓）。"""
        try:
            conn = self._conn_or_open()
            conn.execute(
                "UPDATE image_cache SET hit_count = hit_count + 1, last_hit_at = ? "
                "WHERE result_id = ?",
                (_now_iso(), result_id),
            )
            conn.commit()
        except Exception:
            pass

    # ---------------- 写入 ----------------

    def insert(
        self,
        *,
        path: str,
        sha256: str,
        filename: str,
        dhash: str,
        model_id: str,
        summary: str,
        text: str,
        question: str = "",
        tags: Sequence[str] = (),
    ) -> str:
        """写入一条识图结果，返回 result_id。

        同一张图（sha256+filename 相同）重复写入时直接复用已有 result_id，
        不新增行，避免缓存里堆一堆同一张图的记录。
        """
        conn = self._conn_or_open()
        existing = None
        if sha256:
            existing = conn.execute(
                "SELECT result_id FROM image_cache WHERE sha256 = ? AND filename = ? "
                "LIMIT 1",
                (sha256, filename),
            ).fetchone()
        result_id = str(existing["result_id"]) if existing else f"res_{uuid.uuid4().hex[:12]}"

        if existing:
            conn.execute(
                "UPDATE image_cache SET summary = ?, text = ?, phash = ?, source_value = ?, "
                "model_id = ?, read_at = ?, last_hit_at = ? WHERE result_id = ?",
                (
                    summary,
                    text,
                    dhash,
                    path,
                    model_id,
                    _now_iso(),
                    _now_iso(),
                    result_id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO image_cache (sha256, filename, phash, model_id, question, "
                "result_id, source_value, summary, text, tags, result_json, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha256,
                    filename,
                    dhash,
                    model_id,
                    question,
                    result_id,
                    path,
                    summary,
                    text,
                    json.dumps(list(tags), ensure_ascii=False),
                    json.dumps(
                        {"summary": summary, "text": text, "tags": list(tags)},
                        ensure_ascii=False,
                    ),
                    _now_iso(),
                ),
            )
        conn.commit()
        return result_id

    # ---------------- 清理 ----------------

    def cleanup_expired(self, ttl_days: int) -> int:
        """删除 read_at 距今超过 ttl_days 的记录，返回删除条数。

        ttl_days <= 0 时不做任何事。
        """
        if ttl_days <= 0:
            return 0
        threshold = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        conn = self._conn_or_open()
        rows = conn.execute("SELECT result_id, read_at FROM image_cache").fetchall()
        stale: list[str] = []
        for row in rows:
            parsed = _parse_iso(row["read_at"])
            if parsed is not None and parsed < threshold:
                stale.append(str(row["result_id"]))
        if not stale:
            return 0
        conn.executemany(
            "DELETE FROM image_cache WHERE result_id = ?", [(rid,) for rid in stale]
        )
        conn.commit()
        return len(stale)

    def count(self) -> int:
        conn = self._conn_or_open()
        row = conn.execute("SELECT COUNT(*) AS c FROM image_cache").fetchone()
        return int(row["c"]) if row else 0


# ---------------------------------------------------------------------------
# VL 调用
# ---------------------------------------------------------------------------


def build_describe_prompt(
    paths: Sequence[str], max_chars: int = 60, question: str = ""
) -> str:
    """构造批量识图提示词：按顺序编号，要求逐张给一句话描述。

    带 question 时改成按问题回答（单张图的场景），否则是要通用描述。
    """
    ask = str(question or "").strip()
    if ask:
        return "\n".join(
            [
                f"下面有 {len(paths)} 张图片，按顺序编号 1 到 {len(paths)}。",
                f"针对每张图片回答这个问题：{ask}",
                f"每张的答案控制在 {max_chars} 字以内，只根据看得见的内容说，不要猜。",
                "",
                '只输出 JSON：{"captions": ["第1张的答案", "第2张的答案", ...]}',
            ]
        )
    return "\n".join(
        [
            f"下面有 {len(paths)} 张图片，按顺序编号 1 到 {len(paths)}。",
            f"每张用一句话描述画面里有什么（{max_chars} 字以内），包含能看清的文字。",
            "只描述看得见的内容，不要猜测。",
            "",
            '只输出 JSON：{"captions": ["第1张的描述", "第2张的描述", ...]}',
        ]
    )


def parse_captions(raw: str, expected: int) -> list[str]:
    """从模型输出里解析出描述列表；解析不出来时退回按行切分。"""
    import re

    text = str(raw or "").strip()
    if not text:
        return []
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            obj = json.loads(match.group(0))
            captions = obj.get("captions") if isinstance(obj, dict) else None
            if isinstance(captions, list):
                cleaned = [
                    re.sub(r"\s+", " ", str(c)).strip() for c in captions if str(c).strip()
                ]
                if cleaned:
                    return cleaned
        except Exception:
            pass
    # 兜底：按行拆，去掉编号前缀
    lines = [re.sub(r"^\s*\d+[.、:：)\]]\s*", "", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]


async def describe_images(
    provider: Any,
    paths: Sequence[str],
    *,
    model_id: str = "",
    max_chars: int = 60,
    timeout_sec: int = 60,
    question: str = "",
) -> list[str]:
    """调一次 VL 模型批量描述图片，返回与 paths 等长的描述列表。

    单张失败时对应位置为空串，不阻塞其余图片。

    Args:
        provider: AstrBot 的 Provider 实例（modalities 需含 image）。
        paths: 图片本地路径列表。
        model_id: 写缓存时记的模型名。
        max_chars: 每张描述的字数上限。
        timeout_sec: 单次调用超时。
        question: 想针对图片问的问题，留空则要求通用描述。

    Returns:
        描述列表，长度与 paths 相同。
    """
    valid = [p for p in paths if is_image_path(p)]
    if not valid or provider is None:
        return ["" for _ in paths]

    prompt = build_describe_prompt(valid, max_chars, question)
    resp = await asyncio.wait_for(
        provider.text_chat(
            prompt=prompt,
            image_urls=list(valid),
            session_id=f"irmia_vision_{uuid.uuid4().hex[:8]}",
            persist=False,
        ),
        timeout=timeout_sec,
    )
    raw = getattr(resp, "completion_text", "") or ""
    captions = parse_captions(raw, len(valid))
    if len(captions) < len(valid):
        captions = captions + [""] * (len(valid) - len(captions))

    # 把 valid 的结果映射回原始顺序（跳过非图片路径）
    result: list[str] = []
    idx = 0
    for path in paths:
        if is_image_path(path) and idx < len(captions):
            result.append(captions[idx])
            idx += 1
        else:
            result.append("")
    return result


async def describe_and_cache(
    cache: VisionCache,
    provider: Any,
    paths: Sequence[str],
    *,
    model_id: str = "",
    ttl_days: int = 7,
    max_chars: int = 60,
    timeout_sec: int = 60,
    logger: Any = None,
    question: str = "",
) -> list[str]:
    """查缓存优先，未命中的批量调 VL 并落库，返回与 paths 等长的描述。

    这是给 topic.py 用的主入口：一次调用就能把整个窗口的图补上描述。
    """
    results: list[str] = []
    pending: list[tuple[int, str, str, str]] = []  # (下标, 路径, sha256, dhash)

    for index, path in enumerate(paths):
        if not is_image_path(path):
            results.append("")
            continue
        sha256 = await asyncio.to_thread(sha256_of_file, path)
        dhash = await asyncio.to_thread(compute_dhash, path)
        try:
            hit = await asyncio.to_thread(
                cache.find,
                path=path,
                sha256=sha256,
                filename=os.path.basename(path),
                dhash=dhash,
                ttl_days=ttl_days,
            )
        except Exception:
            hit = None
        if hit is not None and (hit.summary or hit.text):
            results.append((hit.summary or hit.text).strip())
        else:
            results.append("")
            pending.append((index, path, sha256, dhash))

    if pending:
        try:
            captions = await describe_images(
                provider,
                [item[1] for item in pending],
                model_id=model_id,
                max_chars=max_chars,
                timeout_sec=timeout_sec,
                question=question,
            )
        except Exception as exc:
            if logger is not None:
                logger.debug(f"[IRMIA] 批量识图失败: {exc!r}")
            captions = ["" for _ in pending]

        for (index, path, sha256, dhash), caption in zip(pending, captions):
            if not caption:
                continue
            results[index] = caption
            try:
                await asyncio.to_thread(
                    cache.insert,
                    path=path,
                    sha256=sha256,
                    filename=os.path.basename(path),
                    dhash=dhash,
                    model_id=model_id or "unknown",
                    summary=caption[:200],
                    text=caption,
                )
            except Exception as exc:
                if logger is not None:
                    logger.debug(f"[IRMIA] 识图结果写缓存失败: {exc!r}")

    return results
