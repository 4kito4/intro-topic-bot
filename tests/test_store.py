"""state.json の読み書き（ラウンドトリップと旧形式互換）のテスト。"""

from __future__ import annotations

import json

from store import (
    PendingMeasurement,
    PoolItem,
    QueueItem,
    State,
    TopicStat,
    load_state,
    save_state,
)


def _full_state(iso) -> State:
    return State(
        processed_ids=[1, 2],
        queue=[
            QueueItem(
                message_id=101,
                author_id=11,
                author_display_name="name",
                content="c" * 60,
                created_at=iso(hours=2),
                retry_count=1,
                next_retry_at=iso(minutes=-5),
            )
        ],
        last_posted_at=iso(hours=50),
        last_seen_at=iso(hours=1),
        chat_activity=[iso(minutes=10)],
        posted_topics=["お題1"],
        posted_formats=["choice"],
        intro_pool=[
            PoolItem(
                message_id=201,
                content="d" * 60,
                created_at=iso(days=10),
                used_count=2,
                last_used_at=iso(days=1),
            )
        ],
        pending_measurements=[
            PendingMeasurement(
                message_id=301, topic="お題2", format="values",
                posted_at=iso(hours=3), is_poll=True, measured_count=1,
            )
        ],
        topic_stats=[
            TopicStat(
                topic="お題3", format="aruaru", replies=2, reactions=3, votes=4,
                measured_at=iso(hours=4), at_hours=24,
            )
        ],
        paused=True,
    )


def test_全フィールドのラウンドトリップ(tmp_path, iso):
    path = tmp_path / "state.json"
    original = _full_state(iso)
    save_state(path, original)
    loaded = load_state(path)
    assert loaded == original


def test_保存はアトミックで一時ファイルを残さない(tmp_path, iso):
    path = tmp_path / "state.json"
    save_state(path, _full_state(iso))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_ファイルがなければ空のState(tmp_path):
    assert load_state(tmp_path / "missing.json") == State()


def test_旧state_jsonはデフォルトで補完される(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"processed_ids": [1]}), encoding="utf-8")
    loaded = load_state(path)
    assert loaded.processed_ids == [1]
    assert loaded.queue == []
    assert loaded.intro_pool == []
    assert loaded.posted_topics == []
    assert loaded.posted_formats == []
    assert loaded.pending_measurements == []
    assert loaded.topic_stats == []
    assert loaded.last_posted_at is None
    assert loaded.paused is False


def test_next_retry_atがないキュー項目も読める(tmp_path, iso):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "message_id": 101,
                        "author_id": 11,
                        "author_display_name": "name",
                        "content": "c" * 60,
                        "created_at": iso(hours=2),
                        "retry_count": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    item = load_state(path).queue[0]
    assert item.retry_count == 2
    assert item.next_retry_at is None


def test_計測フィールドがない旧state_jsonもデフォルトで読める(tmp_path, iso):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "pending_measurements": [
                    {
                        "message_id": 301,
                        "topic": "お題2",
                        "format": "values",
                        "posted_at": iso(hours=3),
                        "is_poll": True,
                    }
                ],
                "topic_stats": [
                    {
                        "topic": "お題3",
                        "format": "aruaru",
                        "replies": 2,
                        "reactions": 3,
                        "votes": 4,
                        "measured_at": iso(hours=4),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_state(path)
    assert loaded.pending_measurements[0].measured_count == 0
    # 旧データは6時間後の1点計測だった
    assert loaded.topic_stats[0].at_hours == 6
    assert loaded.paused is False


def test_旧形式のlast_chat_activityを移行する(tmp_path, iso):
    path = tmp_path / "state.json"
    stamp = iso(minutes=5)
    path.write_text(json.dumps({"last_chat_activity": stamp}), encoding="utf-8")
    assert load_state(path).chat_activity == [stamp]
