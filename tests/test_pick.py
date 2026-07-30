"""候補選択（_pick_item / _pick_pool_item）のテスト。"""

from __future__ import annotations

from store import State


def test_キューは古いものから処理する(make_bot, make_queue_item):
    old = make_queue_item(101, minutes_ago=300)
    mid = make_queue_item(102, minutes_ago=120)
    new = make_queue_item(103, minutes_ago=60)
    bot = make_bot(State(queue=[new, old, mid]))
    assert bot._pick_item() is old


def test_遅延時間が未経過なら選ばれない(make_bot, make_queue_item):
    # min_delay_minutes=30
    bot = make_bot(State(queue=[make_queue_item(101, minutes_ago=10)]))
    assert bot._pick_item() is None


def test_バックオフ中の候補は除外される(make_bot, make_queue_item, iso):
    backoff = make_queue_item(101, minutes_ago=300, next_retry_at=iso(minutes=-20))
    ready = make_queue_item(102, minutes_ago=120)
    bot = make_bot(State(queue=[backoff, ready]))
    assert bot._pick_item() is ready


def test_バックオフが明けた候補は選ばれる(make_bot, make_queue_item, iso):
    expired = make_queue_item(101, minutes_ago=300, next_retry_at=iso(minutes=1))
    bot = make_bot(State(queue=[expired]))
    assert bot._pick_item() is expired


def test_全候補がバックオフ中ならNone(make_bot, make_queue_item, iso):
    bot = make_bot(
        State(queue=[make_queue_item(101, minutes_ago=300, next_retry_at=iso(minutes=-5))])
    )
    assert bot._pick_item() is None


def test_プールが空ならNone(make_bot):
    assert make_bot(State())._pick_pool_item() is None


def test_プールは使用回数が少ないものを優先する(make_bot, make_pool_item, iso):
    once = make_pool_item(201, used_count=1, last_used_at=iso(days=9))
    twice = make_pool_item(202, used_count=2, last_used_at=iso(days=30))
    bot = make_bot(State(intro_pool=[twice, once]))
    assert bot._pick_pool_item() is once


def test_使用回数が同じなら最後に使ったのが古いものを選ぶ(make_bot, make_pool_item, iso):
    recent = make_pool_item(201, used_count=1, last_used_at=iso(days=1))
    older = make_pool_item(202, used_count=1, last_used_at=iso(days=9))
    bot = make_bot(State(intro_pool=[recent, older]))
    assert bot._pick_pool_item() is older


def test_未使用のプール項目が最優先(make_bot, make_pool_item, iso):
    unused = make_pool_item(201, used_count=0, last_used_at=None)
    used = make_pool_item(202, used_count=1, last_used_at=iso(days=30))
    bot = make_bot(State(intro_pool=[used, unused]))
    assert bot._pick_pool_item() is unused
