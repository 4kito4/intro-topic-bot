"""内蔵の定型お題（Gemini 不在でも投稿を絶やさない仕組み）のテスト。

投稿そのもの（Discord への送信）は API に触れるため DRY_RUN で検証する。
"""

from __future__ import annotations

import asyncio
import random

import pytest

import bot as botmod
from bot import RECENT_TOPICS_KEPT
from store import State
from topic_generator import FALLBACK_TOPICS, TOPIC_FORMATS, fallback_topic

QUESTIONS = [topic.topic_question for topic in FALLBACK_TOPICS]


def _boom(*args, **kwargs):
    raise RuntimeError("Gemini 障害")


# --- 定型お題の中身 -------------------------------------------------------


def test_お題は重複していない():
    assert len(set(QUESTIONS)) == len(FALLBACK_TOPICS)


def test_直近保持件数より多く用意する():
    # 直近お題が全て定型でも、必ず被らない候補が残るようにする
    assert len(FALLBACK_TOPICS) > RECENT_TOPICS_KEPT


def test_投票の選択肢を持たないので二択型は入れない():
    assert all(topic.format != "choice" for topic in FALLBACK_TOPICS)


def test_二択型以外の形式が満遍なく入っている():
    used = {topic.format for topic in FALLBACK_TOPICS}
    assert used == {f for f in TOPIC_FORMATS if f != "choice"}
    for fmt in used:
        assert sum(1 for topic in FALLBACK_TOPICS if topic.format == fmt) >= 2


@pytest.mark.parametrize("topic", FALLBACK_TOPICS, ids=lambda t: t.topic_question)
def test_お題は問いかけの体裁になっている(topic):
    assert topic.topic_question.endswith("？")


# --- fallback_topic -------------------------------------------------------


def test_生成結果はGeminiを使わない形になる():
    result = fallback_topic([])
    assert result.topic_question in QUESTIONS
    assert result.lead_in == ""
    assert result.used_search is False
    assert result.poll_options is None
    assert result.extracted_interests == []


def test_直近のお題と被らないものを選ぶ():
    recent = QUESTIONS[:-1]
    assert fallback_topic(recent).topic_question == QUESTIONS[-1]


def test_全候補が直近と被るなら全体から選ぶ():
    # 沈黙するくらいなら重複してでも投稿する
    assert fallback_topic(list(QUESTIONS)).topic_question in QUESTIONS


def test_乱数を渡せば結果が決まる():
    picked = [fallback_topic([], random.Random(7)).topic_question for _ in range(3)]
    assert len(set(picked)) == 1


# --- 投稿経路 -------------------------------------------------------------


def test_キー未設定ならGeminiを呼ばずに定型お題を投稿する(make_bot, monkeypatch):
    monkeypatch.setattr(botmod, "generate_topic", _boom)
    bot = make_bot(gemini_api_key="", dry_run=True)
    summary = asyncio.run(bot._post_once())
    assert "種別=定型" in summary
    assert any(q in summary for q in QUESTIONS)


def test_キー未設定でもキューは消費しない(make_bot, make_queue_item, monkeypatch):
    monkeypatch.setattr(botmod, "generate_topic", _boom)
    item = make_queue_item(101)
    bot = make_bot(State(queue=[item]), gemini_api_key="", dry_run=True)
    asyncio.run(bot._post_once())
    assert bot.state.queue == [item]
    assert item.retry_count == 0


def test_生成に失敗しても定型お題で投稿を絶やさない(make_bot, monkeypatch):
    monkeypatch.setattr(botmod, "generate_topic", _boom)
    bot = make_bot(dry_run=True)  # キューもプールも空なので種別=汎用
    summary = asyncio.run(bot._post_once())
    assert "お題の生成に失敗しました (種別=汎用)" in summary
    assert "種別=定型" in summary


def test_生成失敗時もキュー項目のリトライ管理は変わらない(
    make_bot, make_queue_item, monkeypatch
):
    monkeypatch.setattr(botmod, "generate_topic", _boom)
    item = make_queue_item(101)
    bot = make_bot(State(queue=[item]), dry_run=True)

    async def refresh(source):
        return "ok"

    bot._refresh_source = refresh
    summary = asyncio.run(bot._post_once())
    assert item.retry_count == 1
    assert item.next_retry_at is not None
    assert bot.state.queue == [item]  # 次の周回で再挑戦する
    assert "種別=定型" in summary


def test_定型お題も計測待ちと履歴に記録される(make_bot, now):
    bot = make_bot()
    result = fallback_topic([])
    bot._record_post(result, 999, False, now)
    assert [p.message_id for p in bot.state.pending_measurements] == [999]
    assert bot.state.posted_topics == [result.topic_question]
    assert bot.state.posted_formats == [result.format]
    assert bot.state.last_posted_at == now.isoformat()
