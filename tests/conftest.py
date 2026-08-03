"""テスト共通のフィクスチャ。Discord / Gemini は一切呼ばない。

discord.Client を実体化せず、必要なメソッドだけを持つスタブに
IntroTopicBot のメソッドを載せて検証する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot as botmod
from bot import IntroTopicBot
from store import PoolItem, QueueItem, State

UTC = timezone.utc
NOW = datetime(2026, 7, 30, 11, 0, tzinfo=UTC)  # JST 20:00（投稿時間帯内）


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """now_utc() を固定して時刻依存のテストを安定させる。"""
    monkeypatch.setattr(botmod, "now_utc", lambda: NOW)
    return NOW


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def iso():
    """NOW から遡った ISO 8601 文字列を作る。"""

    def _iso(days=0, hours=0, minutes=0):
        return (NOW - timedelta(days=days, hours=hours, minutes=minutes)).isoformat()

    return _iso


@pytest.fixture
def settings():
    return SimpleNamespace(
        min_delay_minutes=30,
        quiet_minutes=15,
        quiet_busy_minutes=45,
        activity_window_minutes=60,
        busy_threshold=3,
        post_interval_hours=48,
        post_window_start=19,
        post_window_end=22,
        min_intro_length=50,
        pool_max_age_days=90,
        optout_emoji="🚫",
        use_poll=True,
        measure_after_hours=6,
        measure_final_after_hours=24,
        measure_giveup_hours=72,
        dry_run=False,
        log_channel_id=0,
        owner_user_id=0,
        intro_channel_id=111,
        chat_channel_id=222,
        gemini_api_key="k",
        gemini_model="m",
    )


class BotStub:
    """IntroTopicBot の純ロジック部分だけを借りたスタブ。"""

    def __init__(self, state: State, settings: SimpleNamespace) -> None:
        self.state = state
        self.settings = settings
        self.saved = 0

    def save(self) -> None:
        self.saved += 1

    _pick_item = IntroTopicBot._pick_item
    _pick_pool_item = IntroTopicBot._pick_pool_item
    _format_candidates = IntroTopicBot._format_candidates
    _pick_format = IntroTopicBot._pick_format
    _blocked_reason = IntroTopicBot._blocked_reason
    _quiet_blocked_reason = IntroTopicBot._quiet_blocked_reason
    _good_examples = IntroTopicBot._good_examples
    _is_manual_trigger = IntroTopicBot._is_manual_trigger


@pytest.fixture
def make_bot(settings):
    def _make(state: State | None = None, **overrides) -> BotStub:
        merged = SimpleNamespace(**{**vars(settings), **overrides})
        return BotStub(state if state is not None else State(), merged)

    return _make


@pytest.fixture
def make_queue_item(iso):
    def _make(message_id=101, *, minutes_ago=60, content="a" * 60, next_retry_at=None):
        return QueueItem(
            message_id=message_id,
            author_id=1,
            author_display_name="name",
            content=content,
            created_at=iso(minutes=minutes_ago),
            next_retry_at=next_retry_at,
        )

    return _make


@pytest.fixture
def make_pool_item(iso):
    def _make(message_id=201, *, used_count=0, last_used_at=None, days_ago=10):
        return PoolItem(
            message_id=message_id,
            content="b" * 60,
            created_at=iso(days=days_ago),
            used_count=used_count,
            last_used_at=last_used_at,
        )

    return _make
