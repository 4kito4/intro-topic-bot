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
class State:
    processed_ids: list[int] = field(default_factory=list)
    queue: list[QueueItem] = field(default_factory=list)
    last_posted_at: str | None = None
    last_seen_at: str | None = None
    last_chat_activity: str | None = None

    def is_processed(self, message_id: int) -> bool:
        return message_id in self.processed_ids or any(
            item.message_id == message_id for item in self.queue
        )


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return State(
        processed_ids=raw.get("processed_ids", []),
        queue=[QueueItem(**item) for item in raw.get("queue", [])],
        last_posted_at=raw.get("last_posted_at"),
        last_seen_at=raw.get("last_seen_at"),
        last_chat_activity=raw.get("last_chat_activity"),
    )


def save_state(path: Path, state: State) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp_path, path)
