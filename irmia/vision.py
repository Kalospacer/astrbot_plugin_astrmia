"""识图存储：留底图与描述缓存，只用插件自己的数据目录。

AstrBot 在事件结束时删除预处理的临时图片，必须在入站钩子里另存缩小图，
之后按会话与平台消息编号读取；图片描述仅缓存，不回写聊天账本。
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

DHASH_THRESHOLD = 4
DHASH_SCAN_LIMIT = 500
KEEP_LONG_SIDE = 1024
KEEP_JPEG_QUALITY = 85
#: 孤儿文件至少存在这么久才清理，避开「已写盘、尚未登记」的瞬间
ORPHAN_GRACE_SEC = 600

VISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS kept_images (
    umo TEXT NOT NULL,
    platform_message_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    dhash TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (umo, platform_message_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_kept_created ON kept_images(created_at);
CREATE INDEX IF NOT EXISTS idx_kept_sha ON kept_images(sha256);
CREATE TABLE IF NOT EXISTS image_desc (
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    dhash TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_hit_at REAL NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sha256, kind, question)
);
CREATE INDEX IF NOT EXISTS idx_desc_kind_hit ON image_desc(kind, last_hit_at DESC);
"""


@dataclass
class KeptImage:
    """一条消息中按组件顺序编号的留底图。"""

    idx: int
    sha256: str
    dhash: str
    path: str


def compute_dhash(image: Image.Image) -> str:
    """计算 9×8 灰度相邻比较的 64 位 dHash。"""
    with image.convert("L").resize((9, 8), Image.Resampling.LANCZOS) as small:
        pixels = list(small.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                value |= 1 << (row * 8 + col)
    return f"{value:016x}"


def hamming_distance(a: str, b: str) -> int:
    """计算两条本插件生成的 dHash 的汉明距离。"""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def make_kept_jpeg(src_path: str, keep_root: Path) -> tuple[str, str, str]:
    """按原图哈希留底 JPEG；动图取首帧，透明区域贴白底，同图不重写。

    先写同目录临时文件再原子改名，并发写同一张图时读者只会看到完整文件。
    """
    raw = Path(src_path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    target = keep_root.resolve() / sha[:2] / f"{sha}.jpg"
    if not target.is_file():
        with Image.open(io.BytesIO(raw)) as original:
            if original.mode in ("RGBA", "LA") or (
                original.mode == "P" and "transparency" in original.info
            ):
                rgba = original.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = original.convert("RGB")
            image.thumbnail((KEEP_LONG_SIDE, KEEP_LONG_SIDE), Image.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.tmp")
            image.save(tmp, "JPEG", quality=KEEP_JPEG_QUALITY)
            try:
                os.replace(tmp, target)
            except PermissionError:
                # Windows 上目标正被另一线程读取时不允许覆盖；同 sha 内容相同，别人写好即可
                tmp.unlink()
    # 新图和重复图都按最终 JPEG 算 dHash。压缩后的哈希会不同。
    with Image.open(target) as image:
        return sha, compute_dhash(image), str(target)


class VisionStore:
    """同步识图库；调用方使用线程池，同一连接的事务串行执行。"""

    def __init__(self, db_path: Path, keep_root: Path) -> None:
        self.db_path = Path(db_path)
        self.keep_root = Path(keep_root)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        """打开独立数据库并建立留底、描述两张表。"""
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.keep_root.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(VISION_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def keep_image(self, umo: str, pmid: str, idx: int, src_path: str) -> None:
        """保存并登记留底图；缩放编码在锁外做。"""
        sha, dhash, path = make_kept_jpeg(src_path, self.keep_root)
        self.add_kept(umo, pmid, idx, sha, dhash, path)

    def add_kept(
        self, umo: str, pmid: str, idx: int, sha256: str, dhash: str, path: str
    ) -> None:
        """登记平台消息的第 idx 张图片，重复事件不延长保留时间。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO kept_images VALUES(?,?,?,?,?,?,?)",
                (umo, pmid, idx, sha256, dhash, path, time.time()),
            )
            self._conn.commit()

    def kept_for(self, umo: str, pmid: str) -> list[KeptImage]:
        """按组件编号返回指定消息的留底图。"""
        return self.kept_for_many(umo, [pmid]).get(pmid, [])

    def kept_for_many(
        self, umo: str, pmids: Sequence[str]
    ) -> dict[str, list[KeptImage]]:
        """一次查询取回展示窗口内的图片，按消息与组件编号分组。"""
        if not pmids:
            return {}
        with self._lock:
            marks = ",".join("?" for _ in pmids)
            rows = self._conn.execute(
                f"SELECT * FROM kept_images WHERE umo=? AND platform_message_id IN ({marks}) ORDER BY idx",
                (umo, *pmids),
            ).fetchall()
        result: dict[str, list[KeptImage]] = {}
        for row in rows:
            result.setdefault(row["platform_message_id"], []).append(
                KeptImage(
                    row["idx"],
                    row["sha256"],
                    row["dhash"],
                    row["path"],
                )
            )
        return result

    def find_desc(
        self,
        sha256: str,
        kind: str,
        question: str = "",
        dhash: str = "",
        allow_brief: bool = True,
    ) -> str | None:
        """精确查描述，图被重新压缩时按 dHash 近似命中；命中时更新最后使用时间。

        Args:
            sha256: 留底图的哈希。
            kind: 描述种类，`caption` 是完整描述，`qa` 是带问题的回答。
            question: 提问原文，只有 qa 才有。
            dhash: 留底图指纹，用于换压缩参数后的近似命中。
            allow_brief: 缺完整描述时要不要拿旧的一句话简述凑数。展示侧要，
                因为一句也比没有强；read_image 不要，它要的是完整描述。

        Returns:
            命中则返回描述原文，否则返回 None。
        """
        kinds = [kind]
        if kind == "caption" and allow_brief:
            kinds.append("brief")
        with self._lock:
            row = None
            for candidate in kinds:
                row = self._conn.execute(
                    "SELECT * FROM image_desc WHERE sha256=? AND kind=? AND question=?",
                    (sha256, candidate, question),
                ).fetchone()
                if row is not None:
                    break
            if row is None and kind in ("caption", "brief") and dhash:
                fallback_kinds = "('caption','brief')" if allow_brief else f"('{kind}')"
                recent = self._conn.execute(
                    f"SELECT * FROM image_desc WHERE kind IN {fallback_kinds} AND dhash!='' ORDER BY last_hit_at DESC LIMIT ?",
                    (DHASH_SCAN_LIMIT,),
                ).fetchall()
                row = next(
                    (
                        r
                        for r in recent
                        if hamming_distance(dhash, r["dhash"]) <= DHASH_THRESHOLD
                    ),
                    None,
                )
            if row is None:
                return None
            self._conn.execute(
                "UPDATE image_desc SET last_hit_at=?, hit_count=hit_count+1 WHERE sha256=? AND kind=? AND question=?",
                (time.time(), row["sha256"], row["kind"], row["question"]),
            )
            self._conn.commit()
            return row["text"]

    def put_desc(
        self,
        sha256: str,
        kind: str,
        question: str,
        dhash: str,
        model_id: str,
        text: str,
    ) -> None:
        """按原图、描述种类与问题保存结果，不混用详细描述或问答。"""
        with self._lock:
            now = time.time()
            self._conn.execute(
                "INSERT OR REPLACE INTO image_desc VALUES(?,?,?,?,?,?,?,?,0)",
                (sha256, kind, question, dhash, model_id, text, now, now),
            )
            self._conn.commit()

    def cleanup(self, keep_days: int, desc_ttl_days: int) -> tuple[int, int, int]:
        """清理过期留底行、无引用 JPEG 和长期未使用的描述。"""
        with self._lock:
            now = time.time()
            kept = self._conn.execute(
                "DELETE FROM kept_images WHERE created_at<?", (now - keep_days * 86400,)
            ).rowcount
            referenced = {
                r[0]
                for r in self._conn.execute("SELECT DISTINCT sha256 FROM kept_images")
            }
            files = 0
            for path in self.keep_root.glob("*/*"):
                if path.suffix not in (".jpg", ".tmp") or path.stem in referenced:
                    continue
                if now - path.stat().st_mtime < ORPHAN_GRACE_SEC:
                    continue
                path.unlink()
                files += 1
            for folder in self.keep_root.iterdir():
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            desc = 0
            if desc_ttl_days > 0:
                desc = self._conn.execute(
                    "DELETE FROM image_desc WHERE last_hit_at<?",
                    (now - desc_ttl_days * 86400,),
                ).rowcount
            self._conn.commit()
            return kept, files, desc

    def stats(self) -> tuple[int, int]:
        """返回留底引用数和描述缓存数。"""
        with self._lock:
            return (
                self._conn.execute("SELECT COUNT(*) FROM kept_images").fetchone()[0],
                self._conn.execute("SELECT COUNT(*) FROM image_desc").fetchone()[0],
            )
