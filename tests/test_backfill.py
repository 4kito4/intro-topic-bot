"""過去の自己紹介の取り込み判定（_backfill_verdict）と保持期限の削除（_prune_expired）のテスト。

履歴の取得と書き込みは Discord API を叩くのでテスト対象外。判定は純関数に切り出してある。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from intro_topic.cog import _backfill_verdict
from intro_topic.store import PoolItem, QueueItem, State


@pytest.fixture
def make_message(now):
    """discord.Message のうち判定に使う属性だけを持つスタブ。"""

    def _make(message_id=301, *, is_bot=False, days_ago=1, content="a" * 60, reactions=()):
        return SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(bot=is_bot),
            created_at=now - timedelta(days=days_ago),
            content=content,
            reactions=[SimpleNamespace(emoji=e) for e in reactions],
        )

    return _make


@pytest.fixture
def verdict(settings, now):
    def _verdict(message, state=None):
        return _backfill_verdict(message, state if state is not None else State(), settings, now)

    return _verdict


# --- 判定 -----------------------------------------------------------------


def test_未処理の自己紹介は取り込む(make_message, verdict):
    assert verdict(make_message()) == "ok"


def test_botの投稿は取り込まない(make_message, verdict):
    assert verdict(make_message(is_bot=True)) == "bot"


def test_処理済みIDは重複扱い(make_message, verdict):
    assert verdict(make_message(301), State(processed_ids=[301])) == "dup"


def test_キューにあるものは重複扱い(make_message, make_queue_item, verdict):
    state = State(queue=[make_queue_item(301)])
    assert verdict(make_message(301), state) == "dup"


def test_プールにあるものは重複扱い(make_message, make_pool_item, verdict):
    # is_processed はプールを見ないので、ここが漏れると二重登録になる
    state = State(intro_pool=[make_pool_item(301)])
    assert verdict(make_message(301), state) == "dup"


def test_保持期限を過ぎたものは取り込まない(make_message, verdict):
    # pool_max_age_days=90
    assert verdict(make_message(days_ago=91)) == "too_old"


def test_保持期限ちょうどは取り込まない(make_message, verdict):
    assert verdict(make_message(days_ago=90)) == "too_old"


def test_保持期限内なら取り込む(make_message, verdict):
    assert verdict(make_message(days_ago=89)) == "ok"


def test_オプトアウト済みは取り込まない(make_message, verdict):
    assert verdict(make_message(reactions=("🚫",))) == "optout"


def test_短すぎる投稿は取り込まない(make_message, verdict):
    # min_intro_length=50
    assert verdict(make_message(content="よろしく")) == "short"


def test_オプトアウトは短文より優先して報告する(make_message, verdict):
    assert verdict(make_message(content="よろしく", reactions=("🚫",))) == "optout"


def test_期限超過はオプトアウトより先に判定する(make_message, verdict):
    assert verdict(make_message(days_ago=100, reactions=("🚫",))) == "too_old"


# --- 保持期限の削除 -------------------------------------------------------


def _queue_item(message_id, created_at):
    return QueueItem(
        message_id=message_id,
        author_id=1,
        author_display_name="name",
        content="a" * 60,
        created_at=created_at,
    )


def _pool_item(message_id, created_at):
    return PoolItem(message_id=message_id, content="b" * 60, created_at=created_at)


def test_期限内のものは残る(make_bot, iso):
    state = State(
        queue=[_queue_item(101, iso(days=89))],
        intro_pool=[_pool_item(201, iso(days=89))],
    )
    bot = make_bot(state)
    bot._prune_expired()
    assert len(state.queue) == 1
    assert len(state.intro_pool) == 1
    assert bot.saved == 0  # 変化がなければ保存もしない


def test_期限を過ぎたプール項目を削除する(make_bot, iso):
    state = State(intro_pool=[_pool_item(201, iso(days=91)), _pool_item(202, iso(days=10))])
    bot = make_bot(state)
    bot._prune_expired()
    assert [p.message_id for p in state.intro_pool] == [202]
    assert bot.saved == 1


def test_期限を過ぎたキュー項目も削除する(make_bot, iso):
    # キューに滞留したままの自己紹介も本文を持つので、同じ期限で消す
    state = State(queue=[_queue_item(101, iso(days=91)), _queue_item(102, iso(days=1))])
    bot = make_bot(state)
    bot._prune_expired()
    assert [q.message_id for q in state.queue] == [102]
    assert bot.saved == 1


def test_削除したキュー項目は処理済みに移す(make_bot, iso):
    # processed_ids に入れておかないと起動時の補完で拾い直してしまう
    state = State(queue=[_queue_item(101, iso(days=91))])
    bot = make_bot(state)
    bot._prune_expired()
    assert state.processed_ids == [101]


def test_期限内のキュー項目は処理済みにしない(make_bot, iso):
    state = State(queue=[_queue_item(101, iso(days=1))])
    bot = make_bot(state)
    bot._prune_expired()
    assert state.processed_ids == []
