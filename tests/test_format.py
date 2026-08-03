"""お題形式のローテーション・重み付け・投票選択肢の妥当性判定のテスト。"""

from __future__ import annotations

import pytest

import bot as botmod
from bot import _format_weights, _valid_poll_options
from store import State, TopicStat
from topic_generator import TOPIC_FORMATS

MEASURE_AT = 6  # conftest の settings.measure_after_hours


def _stat(fmt: str, *, replies: int, at_hours: int = MEASURE_AT) -> TopicStat:
    return TopicStat(
        topic=f"お題({fmt})",
        format=fmt,
        replies=replies,
        reactions=0,
        votes=0,
        measured_at="2026-07-30T10:00:00+00:00",
        at_hours=at_hours,
    )


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


# --- 形式の重み付け（乱数を使わず重みそのものを検証する） -------------------


def test_計測結果がなければ全形式が同じ重み():
    assert _format_weights([], MEASURE_AT, list(TOPIC_FORMATS)) == [1.0] * len(TOPIC_FORMATS)


def test_反応が良い形式ほど重みが大きい():
    # スコア: choice=9, experience=0 → prior=4.5
    stats = [_stat("choice", replies=3), _stat("experience", replies=0)]
    weights = _format_weights(stats, MEASURE_AT, ["choice", "experience", "values"])
    assert weights[0] == pytest.approx(6.0)  # (2*4.5 + 9) / (2+1)
    assert weights[1] == pytest.approx(3.0)  # (2*4.5 + 0) / (2+1)
    assert weights[0] > weights[2] > weights[1]


def test_未観測の形式はprior並みの重みになる():
    stats = [_stat("choice", replies=3), _stat("experience", replies=0)]
    # 未観測は (2*prior + 0) / (2+0) = prior。平均並みなので探索が止まらない
    assert _format_weights(stats, MEASURE_AT, ["values"]) == [pytest.approx(4.5)]


def test_反応ゼロが続いても重みは正のまま():
    stats = [_stat("choice", replies=0), _stat("experience", replies=0)]
    weights = _format_weights(stats, MEASURE_AT, list(TOPIC_FORMATS))
    assert all(w > 0 for w in weights)


def test_2点目の計測行は重みに影響しない():
    stats = [
        _stat("choice", replies=3),
        _stat("experience", replies=0),
        # 同じお題の 24h 行。混ぜると experience が過大評価される
        _stat("experience", replies=100, at_hours=24),
    ]
    weights = _format_weights(stats, MEASURE_AT, ["choice", "experience"])
    assert weights == [pytest.approx(6.0), pytest.approx(3.0)]


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
