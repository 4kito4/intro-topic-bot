"""投稿本文の組み立て（形式別の見出し・リード文・フッタ）のテスト。"""

from __future__ import annotations

import pytest

from bot import DEFAULT_TOPIC_EMOJI, _compose_poll_header, _compose_text_body, _topic_header
from topic_generator import FORMAT_EMOJI, TOPIC_FORMATS, TopicResult

FOOTER = "リアクションだけでも歓迎です"


def _result(fmt: str = "values", *, lead_in: str = "") -> TopicResult:
    return TopicResult(
        extracted_interests=["写真"],
        lead_in=lead_in,
        topic_question="最近撮った写真は？",
        used_search=False,
        format=fmt,
    )


# --- 見出し ---------------------------------------------------------------


def test_全形式に見出し絵文字が定義されている():
    assert set(FORMAT_EMOJI) == set(TOPIC_FORMATS)


@pytest.mark.parametrize("fmt", TOPIC_FORMATS)
def test_見出しは形式ごとの絵文字になる(fmt):
    assert _topic_header(fmt) == f"{FORMAT_EMOJI[fmt]} **お題**"


def test_未知の形式は既定の絵文字にフォールバックする():
    assert _topic_header("unknown") == f"{DEFAULT_TOPIC_EMOJI} **お題**"


# --- 通常投稿 -------------------------------------------------------------


def test_リード文ありの本文():
    text = _compose_text_body(_result(lead_in="ふと思ったんだけど"))
    assert text == (
        f"{FORMAT_EMOJI['values']} **お題**\n"
        "\n"
        "ふと思ったんだけど\n"
        "**最近撮った写真は？**"
    )


def test_リード文なしでも見出しの次は空行1行():
    text = _compose_text_body(_result())
    assert text == f"{FORMAT_EMOJI['values']} **お題**\n\n**最近撮った写真は？**"


def test_フッタは本文の末尾に付く():
    text = _compose_text_body(_result(lead_in="ふと思ったんだけど"), FOOTER)
    assert text.endswith(f"**最近撮った写真は？**\n\n{FOOTER}")


def test_空白だけのフッタは付かない():
    assert _compose_text_body(_result(), "   ") == _compose_text_body(_result())


# --- 投票投稿 -------------------------------------------------------------


def test_投票の本文には問いを入れない():
    header = _compose_poll_header(_result("choice", lead_in="どっち派？"))
    assert header == f"{FORMAT_EMOJI['choice']} **お題**\n\nどっち派？"
    assert "最近撮った写真は？" not in header


def test_投票でリード文がなければ見出しだけ():
    assert _compose_poll_header(_result("choice")) == f"{FORMAT_EMOJI['choice']} **お題**"


def test_投票の本文にもフッタが付く():
    header = _compose_poll_header(_result("choice", lead_in="どっち派？"), FOOTER)
    assert header == f"{FORMAT_EMOJI['choice']} **お題**\n\nどっち派？\n\n{FOOTER}"


def test_投票でリード文がなくてもフッタは付く():
    header = _compose_poll_header(_result("choice"), FOOTER)
    assert header == f"{FORMAT_EMOJI['choice']} **お題**\n\n{FOOTER}"
