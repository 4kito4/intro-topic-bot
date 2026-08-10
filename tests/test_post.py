"""投稿の直列化と条件再判定（_generate_and_post）のテスト。

生成・投稿の本体（_post_once）は Gemini / Discord を叩くのでスタブに差し替え、
ロックとロック取得後の条件再判定だけを検証する。
"""

from __future__ import annotations

import asyncio

from intro_topic.store import State


class _PostSpy:
    """_post_once の差し替え。呼ばれた回数を数える。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return "お題を投稿しました"


def test_ワーカー経路は投稿条件を満たさなければ見送る(make_bot, iso):
    # post_interval_hours=48 なので1時間前の投稿では間隔が足りない
    bot = make_bot(State(last_posted_at=iso(hours=1)))
    bot._post_once = spy = _PostSpy()
    summary = asyncio.run(bot._generate_and_post(enforce_conditions=True))
    assert spy.calls == 0
    assert "投稿間隔が未経過" in summary


def test_ワーカー経路も条件を満たせば投稿する(make_bot, iso):
    bot = make_bot(State(last_posted_at=iso(days=3)))
    bot._post_once = spy = _PostSpy()
    summary = asyncio.run(bot._generate_and_post(enforce_conditions=True))
    assert spy.calls == 1
    assert summary == "お題を投稿しました"


def test_手動経路_topic_now_は投稿条件を無視して投稿する(make_bot, iso):
    bot = make_bot(State(last_posted_at=iso(hours=1)))
    bot._post_once = spy = _PostSpy()
    summary = asyncio.run(bot._generate_and_post())
    assert spy.calls == 1
    assert summary == "お題を投稿しました"


def test_投稿処理は同時に走らない(make_bot, iso):
    bot = make_bot(State(last_posted_at=iso(days=3)))
    running = 0
    peak = 0

    async def slow_post() -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)  # 生成待ちの代わりに他タスクへ譲る
        running -= 1
        return "お題を投稿しました"

    bot._post_once = slow_post

    async def main() -> None:
        await asyncio.gather(bot._generate_and_post(), bot._generate_and_post())

    asyncio.run(main())
    assert peak == 1


def test_topic_now_の生成中にワーカーが発火しても2件目は投稿されない(make_bot, iso, now):
    bot = make_bot(State(last_posted_at=iso(days=3)))
    posted = []

    async def slow_post() -> str:
        await asyncio.sleep(0)  # 生成待ちの間にワーカーのタスクが動く
        bot.state.last_posted_at = now.isoformat()
        posted.append("post")
        return "お題を投稿しました"

    bot._post_once = slow_post

    async def main() -> tuple[str, str]:
        return await asyncio.gather(
            bot._generate_and_post(),  # /topic now
            bot._generate_and_post(enforce_conditions=True),  # 5分ワーカー
        )

    _, worker_summary = asyncio.run(main())
    assert len(posted) == 1
    assert "投稿間隔が未経過" in worker_summary
