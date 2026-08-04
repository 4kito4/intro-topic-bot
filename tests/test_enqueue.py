"""自己紹介のキュー取り込み（_enqueue_intro）のテスト。

Discord API は叩かないので、メッセージだけスタブに差し替えて検証する。
"""

from __future__ import annotations

from store import State


def _ids(state: State) -> list[int]:
    return [item.message_id for item in state.queue]


def test_自己紹介はキューに入る(make_bot, make_message):
    bot = make_bot()
    bot._enqueue_intro(make_message(101))
    assert _ids(bot.state) == [101]


def test_同じ著者の旧項目は新しい投稿で置換される(make_bot, make_message):
    bot = make_bot()
    bot._enqueue_intro(make_message(101, author_id=1))
    bot._enqueue_intro(make_message(102, author_id=1))
    assert _ids(bot.state) == [102]


def test_置換した旧項目は処理済みに移す(make_bot, make_message):
    # 再取り込みされて古い本文が復活しないようにする
    bot = make_bot()
    bot._enqueue_intro(make_message(101, author_id=1))
    bot._enqueue_intro(make_message(102, author_id=1))
    assert bot.state.processed_ids == [101]


def test_別の著者の項目は残る(make_bot, make_message):
    bot = make_bot()
    bot._enqueue_intro(make_message(101, author_id=1))
    bot._enqueue_intro(make_message(102, author_id=2))
    assert _ids(bot.state) == [101, 102]
    assert bot.state.processed_ids == []


def test_同じ著者が3回投稿しても最新だけが残る(make_bot, make_message):
    bot = make_bot()
    for message_id in (101, 102, 103):
        bot._enqueue_intro(make_message(message_id, author_id=1))
    assert _ids(bot.state) == [103]
    assert bot.state.processed_ids == [101, 102]


def test_短すぎる投稿では旧項目を置換しない(make_bot, make_message):
    # #自己紹介 での短い雑談で、本物の自己紹介を捨てないようにする
    bot = make_bot()
    bot._enqueue_intro(make_message(101, author_id=1))
    bot._enqueue_intro(make_message(102, author_id=1, content="よろしく"))
    assert _ids(bot.state) == [101]
    assert bot.state.processed_ids == [102]


def test_同じメッセージを取り込み直しても自分を消さない(make_bot, make_message):
    # 起動時のバックフィルが同じ範囲を再走査するケース
    bot = make_bot()
    bot._enqueue_intro(make_message(101, author_id=1))
    bot._enqueue_intro(make_message(101, author_id=1))
    assert _ids(bot.state) == [101]
    assert bot.state.processed_ids == []


def test_再利用プールは置換の対象外(make_bot, make_message, make_pool_item):
    # プールは使用済みの再利用資産なので、投稿し直しても残す
    bot = make_bot(State(intro_pool=[make_pool_item(201)]))
    bot._enqueue_intro(make_message(101, author_id=1))
    assert [p.message_id for p in bot.state.intro_pool] == [201]
