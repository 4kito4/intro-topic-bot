"""state.json の読み書き。クラッシュ耐性のためアトミックに保存する。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class QueueItem:
    message_id: int
    author_id: int
    author_display_name: str
    content: str
    created_at: str  # ISO 8601 (aware)
    retry_count: int = 0

    def created_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at)


@dataclass
class PoolItem:
    """投稿に使い終わった自己紹介。新規が無い期間の話題ネタとして再利用する。"""

    message_id: int
    content: str
    created_at: str  # ISO 8601 (aware)
    used_count: int = 0
    last_used_at: str | None = None  # ISO 8601 (aware)

    def created_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at)


@dataclass
class PendingMeasurement:
    """投稿済みで、まだ反応を集計していないお題。"""

    message_id: int
    topic: str
    format: str
    posted_at: str  # ISO 8601 (aware)
    is_poll: bool = False


@dataclass
class TopicStat:
    """集計済みのお題の反応。few-shot に還元する。"""

    topic: str
    format: str
    replies: int
    reactions: int
    votes: int
    measured_at: str  # ISO 8601 (aware)


@dataclass
class State:
    processed_ids: list[int] = field(default_factory=list)
    queue: list[QueueItem] = field(default_factory=list)
    last_posted_at: str | None = None
    last_seen_at: str | None = None
    chat_activity: list[str] = field(default_factory=list)  # 雑談の直近発言時刻 (ISO)
    posted_topics: list[str] = field(default_factory=list)  # 投稿済みお題（多様性確保用、直近分）
    posted_formats: list[str] = field(default_factory=list)  # 投稿済みお題の形式（ローテーション用）
    intro_pool: list[PoolItem] = field(default_factory=list)  # 使用済み自己紹介の再利用プール
    # 反応の計測待ち / 計測済み（few-shot への還元用）
    pending_measurements: list[PendingMeasurement] = field(default_factory=list)
    topic_stats: list[TopicStat] = field(default_factory=list)

    def is_processed(self, message_id: int) -> bool:
        return message_id in self.processed_ids or any(
            item.message_id == message_id for item in self.queue
        )


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text(encoding="utf-8"))
    chat_activity = raw.get("chat_activity", [])
    # 旧形式 (last_chat_activity: str) からの移行
    if not chat_activity and raw.get("last_chat_activity"):
        chat_activity = [raw["last_chat_activity"]]
    return State(
        processed_ids=raw.get("processed_ids", []),
        queue=[QueueItem(**item) for item in raw.get("queue", [])],
        last_posted_at=raw.get("last_posted_at"),
        last_seen_at=raw.get("last_seen_at"),
        chat_activity=chat_activity,
        posted_topics=raw.get("posted_topics", []),
        posted_formats=raw.get("posted_formats", []),
        intro_pool=[PoolItem(**item) for item in raw.get("intro_pool", [])],
        pending_measurements=[
            PendingMeasurement(**item) for item in raw.get("pending_measurements", [])
        ],
        topic_stats=[TopicStat(**item) for item in raw.get("topic_stats", [])],
    )


def save_state(path: Path, state: State) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp_path, path)
