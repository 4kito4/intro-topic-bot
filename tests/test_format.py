"""お題形式のローテーションと投票選択肢の妥当性判定のテスト。"""

from __future__ import annotations

import pytest

import bot as botmod
from bot import _valid_poll_options
from store import State
from topic_generator import TOPIC_FORMATS


def test_履歴がなければ全形式が候補(make_bot):
    assert make_bot(State())._format_candidates() == list(TOPIC_FORMATS)


def test_直近2形式は候補から外れる(make_bot):
    bot = make_bot(State(posted_formats=["choice", "experience"]))
    candidates = bot._format_candidates()
    assert set(candidates) == set(TOPIC_FORMATS) - {"choice", "experience"}


def test_避けるのは末尾2件だけ(make_bot):
    bot = make_bot(State(posted_formats=["values", "aruaru", "choice", "experience"]))
    assert set(bot._format_candidates()) == {"values", "aruaru", "hypothetical"}


def test_全形式が直近に含まれるときは全形式に戻す(make_bot, monkeypatch):
    monkeypatch.setattr(botmod, "TOPIC_FORMATS", ("choice", "experience"))
    bot = make_bot(State(posted_formats=["choice", "experience"]))
    assert bot._format_candidates() == ["choice", "experience"]


def test_選ばれる形式は必ず候補内(make_bot):
    bot = make_bot(State(posted_formats=["choice", "experience"]))
    picked = {bot._pick_format() for _ in range(50)}
    assert picked <= set(TOPIC_FORMATS) - {"choice", "experience"}


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (None, False),
        ([], False),
        (["犬"], False),
        (["犬", "猫"], True),
        (["春", "夏", "秋", "冬"], True),
        (["a", "b", "c", "d", "e"], False),
        (["犬", ""], False),
        (["犬", "   "], False),
        (["あ" * 55, "猫"], True),
        (["あ" * 56, "猫"], False),
    ],
)
def test_投票の選択肢の妥当性(options, expected):
    assert _valid_poll_options(options) is expected
