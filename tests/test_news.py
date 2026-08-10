"""ニュースを素材にするお題生成のテスト。

Gemini / Discord は呼ばない。プロンプトの組み立て（_build_prompt）と本文の取り出しは
純関数として、ニュースの取得と素材の優先順位はスタブを差し込んで検証する。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

import intro_topic.cog as botmod
from intro_topic.cog import NEWS_FETCH_LIMIT, NEWS_MAX_AGE_DAYS, NEWS_TEXT_MAX, _news_message_text
from intro_topic.store import State
from intro_topic.topic_generator import TOPIC_FORMATS, TopicResult, _build_prompt

NEWS_CHANNEL_ID = 333

NEWS = "大手 AI 企業が新しいマルチモーダルモデルを発表した。画像と音声を同時に扱えるという。"
INTRO = "はじめまして。写真を撮るのが好きな大学1年です。よろしくお願いします。"


# --- ニュース分岐 ---------------------------------------------------------


def test_ニュース本文がプロンプトに入る():
    prompt = _build_prompt(None, None, None, None, NEWS)
    assert NEWS in prompt


def test_ニュースを入口に日常の問いへ置き換えるよう指示する():
    prompt = _build_prompt(None, None, None, None, NEWS)
    assert "日常の経験・感覚で答えられる問い" in prompt
    assert "固有名詞・専門用語は topic_question に入れない" in prompt
    assert "extracted_interests は空リスト" in prompt


def test_ニュース指定時は自己紹介より優先する():
    # 自己紹介を渡していてもニュース経路になる（intro_text は無視する）
    prompt = _build_prompt(INTRO, None, None, None, NEWS)
    assert NEWS in prompt
    assert INTRO not in prompt
    assert "自己紹介文を参考に" not in prompt


def test_ニュース未指定なら従来の分岐のまま():
    assert "自己紹介文を参考に" in _build_prompt(INTRO, None, None, None)
    assert "自己紹介文はありません" in _build_prompt(None, None, None, None)


# --- 後段の合成 -----------------------------------------------------------


def test_直近のお題と指定形式と良い例が付く():
    prompt = _build_prompt(None, ["前回のお題"], "values", ["良かったお題"], NEWS)
    assert "# 最近投稿したお題" in prompt
    assert "- 前回のお題" in prompt
    assert "価値観型" in prompt
    assert "`values`" in prompt
    assert "# 実際に反応が良かったお題の例" in prompt
    assert "- 良かったお題" in prompt


def test_後段の合成は自己紹介経路と同じ形で付く():
    # 素材の入口が変わるだけで、後段（直近お題・指定形式・良い例）は共通のまま
    marker = "\n\n# 最近投稿したお題"
    news_prompt = _build_prompt(None, ["前回のお題"], "values", ["良かったお題"], NEWS)
    intro_prompt = _build_prompt(INTRO, ["前回のお題"], "values", ["良かったお題"])
    assert news_prompt[news_prompt.index(marker) :] == intro_prompt[intro_prompt.index(marker) :]


# --- 本文の取り出し -------------------------------------------------------


def _message(message_id=401, *, content="", embeds=()):
    """discord.Message のうち本文の取り出しに使う属性だけを持つスタブ。"""
    return SimpleNamespace(
        id=message_id,
        content=content,
        embeds=[SimpleNamespace(title=title, description=desc) for title, desc in embeds],
    )


def test_本文だけの投稿はそのまま使う():
    assert _news_message_text(_message(content="本日のAIニュース")) == "本日のAIニュース"


def test_embedだけの投稿もタイトルと説明を拾う():
    text = _news_message_text(_message(embeds=[("見出し", "説明文")]))
    assert text == "見出し\n説明文"


def test_本文とembedの両方があれば繋げる():
    text = _news_message_text(_message(content="本文", embeds=[("見出し", "説明文")]))
    assert text == "本文\n見出し\n説明文"


def test_空の要素は飛ばす():
    text = _news_message_text(_message(content="本文", embeds=[(None, "説明文")]))
    assert text == "本文\n説明文"


def test_全部空なら空文字を返す():
    assert _news_message_text(_message(content="  ", embeds=[(None, None)])) == ""


# --- ニュースの取得 -------------------------------------------------------


def _news_channel(messages, *, error=None):
    """discord.TextChannel の代わり。history() の呼び出し引数を記録する。"""
    calls: dict = {}

    async def history(**kwargs):
        calls.update(kwargs)
        if error is not None:
            raise error
        for message in messages:
            yield message

    channel = MagicMock(spec=discord.TextChannel)
    channel.history = history
    return channel, calls


def _bot_with_news(make_bot, messages, *, error=None, state=None, **overrides):
    bot = make_bot(state, news_channel_id=NEWS_CHANNEL_ID, **overrides)
    channel, calls = _news_channel(messages, error=error)
    bot.channels[NEWS_CHANNEL_ID] = channel
    return bot, calls


def test_未設定ならチャンネルを見に行かない(make_bot):
    bot = make_bot()  # news_channel_id=0
    looked: list[int] = []
    bot.get_channel = looked.append
    assert asyncio.run(bot._pick_news_text()) is None
    assert looked == []


def test_チャンネルが見つからなければ使わない(make_bot):
    bot = make_bot(news_channel_id=NEWS_CHANNEL_ID)  # channels に登録しない
    assert asyncio.run(bot._pick_news_text()) is None


def test_新しい順に見て最初の非空を使う(make_bot):
    # history は新しい順（oldest_first=False）で返ってくる想定
    messages = [
        _message(401),  # 本文も embed も無いので飛ばす
        _message(402, content="新しめのニュース"),
        _message(403, content="古いニュース"),
    ]
    bot, _ = _bot_with_news(make_bot, messages)
    assert asyncio.run(bot._pick_news_text()) == ("新しめのニュース", 402)


def test_取得条件は直近数日を新しい順で読む(make_bot, now):
    # after を指定すると discord.py は oldest_first を True にするため、False の明示が必須
    bot, calls = _bot_with_news(make_bot, [_message(401, content="ニュース")])
    asyncio.run(bot._pick_news_text())
    assert calls["limit"] == NEWS_FETCH_LIMIT
    assert calls["after"] == now - timedelta(days=NEWS_MAX_AGE_DAYS)
    assert calls["oldest_first"] is False


def test_使える本文が無ければ使わない(make_bot):
    bot, _ = _bot_with_news(make_bot, [_message(401), _message(402)])
    assert asyncio.run(bot._pick_news_text()) is None


def test_取得に失敗しても落ちない(make_bot):
    error = discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "boom")
    bot, _ = _bot_with_news(make_bot, [], error=error)
    assert asyncio.run(bot._pick_news_text()) is None


def test_長い本文は切り詰める(make_bot):
    bot, _ = _bot_with_news(make_bot, [_message(401, content="ニ" * (NEWS_TEXT_MAX + 100))])
    text, message_id = asyncio.run(bot._pick_news_text())
    assert len(text) == NEWS_TEXT_MAX
    assert text.endswith("…")
    assert message_id == 401


# --- 素材の優先順位 -------------------------------------------------------


class _GenerateSpy:
    """generate_topic の差し替え。呼び出し引数を記録して固定のお題を返す。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> tuple[TopicResult, None]:
        self.calls.append((args, kwargs))
        return (
            TopicResult(
                extracted_interests=[],
                lead_in="",
                topic_question="生成されたお題？",
                used_search=False,
                format="values",
                poll_options=None,
            ),
            None,
        )


class _NewsSpy:
    """_pick_news_text の差し替え。呼ばれた回数を数える。"""

    def __init__(self, value: tuple[str, int] | None) -> None:
        self.calls = 0
        self.value = value

    async def __call__(self) -> tuple[str, int] | None:
        self.calls += 1
        return self.value


def _make_poster(make_bot, monkeypatch, state, *, news=("ニュース本文", 777), **overrides):
    """DRY_RUN でお題を1件作らせる用意（生成とニュース取得はスタブ）。"""
    spy = _GenerateSpy()
    monkeypatch.setattr(botmod, "generate_topic", spy)
    bot = make_bot(state, dry_run=True, **overrides)

    async def refresh(source):
        return "ok"

    bot._refresh_source = refresh
    news_spy = _NewsSpy(news)
    bot._pick_news_text = news_spy
    return bot, spy, news_spy


def test_新規キューがあればニュースを見に行かない(
    make_bot, monkeypatch, make_queue_item
):
    state = State(queue=[make_queue_item(101)])
    bot, spy, news_spy = _make_poster(make_bot, monkeypatch, state)
    summary = asyncio.run(bot._post_once())
    assert "種別=新規" in summary
    assert news_spy.calls == 0
    assert spy.calls[0][1]["news_text"] is None


def test_未使用のプールはニュースより優先する(make_bot, monkeypatch, make_pool_item):
    state = State(intro_pool=[make_pool_item(201, used_count=0)])
    bot, spy, news_spy = _make_poster(make_bot, monkeypatch, state)
    summary = asyncio.run(bot._post_once())
    assert "種別=プール" in summary
    assert news_spy.calls == 0
    assert spy.calls[0][1]["news_text"] is None


def test_使用済みプールしか無ければニュースを使う(make_bot, monkeypatch, make_pool_item, iso):
    state = State(intro_pool=[make_pool_item(201, used_count=1, last_used_at=iso(days=3))])
    bot, spy, _ = _make_poster(make_bot, monkeypatch, state)
    summary = asyncio.run(bot._post_once())
    assert "種別=ニュース" in summary
    args, kwargs = spy.calls[0]
    assert kwargs["news_text"] == "ニュース本文"
    assert args[0] is None  # ニュース経路では自己紹介を渡さない


def test_ニュースが無ければ使用済みプールに戻る(make_bot, monkeypatch, make_pool_item, iso):
    state = State(intro_pool=[make_pool_item(201, used_count=1, last_used_at=iso(days=3))])
    bot, spy, _ = _make_poster(make_bot, monkeypatch, state, news=None)
    summary = asyncio.run(bot._post_once())
    assert "種別=プール" in summary
    assert spy.calls[0][1]["news_text"] is None


def test_素材が何も無ければニュースを使う(make_bot, monkeypatch):
    bot, spy, _ = _make_poster(make_bot, monkeypatch, State())
    summary = asyncio.run(bot._post_once())
    assert "種別=ニュース" in summary
    assert spy.calls[0][1]["news_text"] == "ニュース本文"


def test_ニュース未設定なら従来どおりの引数で生成する(
    make_bot, monkeypatch, make_pool_item, iso
):
    # news_channel_id=0（既定）では _pick_news_text をスタブせず実物を通す
    spy = _GenerateSpy()
    monkeypatch.setattr(botmod, "generate_topic", spy)
    pool_item = make_pool_item(201, used_count=1, last_used_at=iso(days=3))
    bot = make_bot(State(intro_pool=[pool_item]), dry_run=True)

    async def refresh(source):
        return "ok"

    bot._refresh_source = refresh
    summary = asyncio.run(bot._post_once())
    args, kwargs = spy.calls[0]
    assert "種別=プール" in summary
    # 位置引数（自己紹介 / キー / モデル / 直近お題 / 指定形式 / 良い例）は従来のまま
    assert len(args) == 6
    assert args[0] == pool_item.content  # 使用済みプールでも従来どおり素材になる
    assert args[1:4] == ("GEMINI-KEY-SECRET", "m", [])
    assert args[4] in TOPIC_FORMATS  # 形式はローテーションで決まるので値は問わない
    assert args[5] == []
    assert kwargs == {"news_text": None}


def test_ニュース未設定で素材も無ければ汎用のまま(make_bot, monkeypatch):
    spy = _GenerateSpy()
    monkeypatch.setattr(botmod, "generate_topic", spy)
    bot = make_bot(dry_run=True)
    summary = asyncio.run(bot._post_once())
    args, kwargs = spy.calls[0]
    assert "種別=汎用" in summary
    assert args[0] is None
    assert kwargs == {"news_text": None}


def test_ニュース経路の生成失敗は定型で埋めて素材を減らさない(
    make_bot, monkeypatch, make_pool_item, iso
):
    def _boom(*args, **kwargs):
        raise RuntimeError("Gemini 障害")

    monkeypatch.setattr(botmod, "generate_topic", _boom)
    pool_item = make_pool_item(201, used_count=1, last_used_at=iso(days=3))
    bot = make_bot(State(intro_pool=[pool_item]), dry_run=True)
    bot._pick_news_text = _NewsSpy(("ニュース本文", 777))
    summary = asyncio.run(bot._post_once())
    assert "お題の生成に失敗しました (種別=ニュース)" in summary
    assert "種別=定型" in summary
    # ニュースは使い捨て。キューもプールも触らない
    assert bot.state.queue == []
    assert [(p.message_id, p.used_count) for p in bot.state.intro_pool] == [(201, 1)]
