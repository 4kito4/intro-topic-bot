"""2点計測の時点判定・ギブアップ判定・few-shot 用の絞り込みのテスト。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from intro_topic.cog import _due_checkpoint_hours, _is_measure_given_up, _measure_checkpoints
from intro_topic.store import PendingMeasurement, State, TopicStat

CHECKPOINTS = (6, 24)


def _settings(settings, **overrides) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(settings), **overrides})


@pytest.fixture
def make_pending(iso):
    def _make(*, hours_ago: int, measured_count: int = 0) -> PendingMeasurement:
        return PendingMeasurement(
            message_id=301,
            topic="お題",
            format="values",
            posted_at=iso(hours=hours_ago),
            measured_count=measured_count,
        )

    return _make


# --- 計測時点 ---------------------------------------------------------------


def test_finalが正なら2点計測(settings):
    assert _measure_checkpoints(settings) == (6, 24)


def test_finalが0なら1点計測に戻る(settings):
    assert _measure_checkpoints(_settings(settings, measure_final_after_hours=0)) == (6,)


def test_1点目に未達なら計測しない(make_pending, now):
    assert _due_checkpoint_hours(make_pending(hours_ago=3), now, CHECKPOINTS) is None


def test_1点目に到達したら1点目を返す(make_pending, now):
    assert _due_checkpoint_hours(make_pending(hours_ago=6), now, CHECKPOINTS) == 6


def test_1点目を計測済みで2点目に未達なら計測しない(make_pending, now):
    pending = make_pending(hours_ago=10, measured_count=1)
    assert _due_checkpoint_hours(pending, now, CHECKPOINTS) is None


def test_2点目に到達したら2点目を返す(make_pending, now):
    pending = make_pending(hours_ago=24, measured_count=1)
    assert _due_checkpoint_hours(pending, now, CHECKPOINTS) == 24


def test_1点計測なら2点目は発生しない(make_pending, now):
    pending = make_pending(hours_ago=100, measured_count=1)
    assert _due_checkpoint_hours(pending, now, (6,)) is None


def test_計測回数が時点数を超えていれば完了扱い(make_pending, now):
    pending = make_pending(hours_ago=100, measured_count=2)
    assert _due_checkpoint_hours(pending, now, CHECKPOINTS) is None


# --- ギブアップ -------------------------------------------------------------


def test_猶予内なら諦めない(make_pending, now):
    # 最終計測時点 24h + 猶予 72h = 96h
    assert _is_measure_given_up(make_pending(hours_ago=96), now, CHECKPOINTS, 72) is False


def test_猶予を過ぎたら諦める(make_pending, now):
    assert _is_measure_given_up(make_pending(hours_ago=97), now, CHECKPOINTS, 72) is True


def test_1点計測なら猶予も1点目基準(make_pending, now):
    assert _is_measure_given_up(make_pending(hours_ago=79), now, (6,), 72) is True
    assert _is_measure_given_up(make_pending(hours_ago=77), now, (6,), 72) is False


# --- few-shot の絞り込み ----------------------------------------------------


def _stat(topic: str, *, replies: int, at_hours: int, iso) -> TopicStat:
    return TopicStat(
        topic=topic,
        format="values",
        replies=replies,
        reactions=0,
        votes=0,
        measured_at=iso(hours=1),
        at_hours=at_hours,
    )


def test_few_shotは1点目の行だけを見る(make_bot, iso):
    # 同じお題の 6h 行と 24h 行が混ざっても、few-shot には1回しか出ない
    state = State(
        topic_stats=[
            _stat("お題A", replies=1, at_hours=6, iso=iso),
            _stat("お題A", replies=5, at_hours=24, iso=iso),
        ]
    )
    assert make_bot(state)._good_examples() == ["お題A"]


def test_24h行だけのお題はfew_shotに出ない(make_bot, iso):
    state = State(topic_stats=[_stat("お題B", replies=9, at_hours=24, iso=iso)])
    assert make_bot(state)._good_examples() == []


def test_1点目のスコア順に並ぶ(make_bot, iso):
    state = State(
        topic_stats=[
            _stat("低", replies=1, at_hours=6, iso=iso),
            _stat("高", replies=3, at_hours=6, iso=iso),
            # 24h の高スコア行は順位に影響しない
            _stat("低", replies=99, at_hours=24, iso=iso),
        ]
    )
    assert make_bot(state)._good_examples() == ["高", "低"]
