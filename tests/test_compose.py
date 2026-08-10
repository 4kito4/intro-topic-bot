"""投稿本文の組み立て（固定の器・見出しのテーマ語・リード文・サブテキスト）のテスト。"""

from __future__ import annotations

import pytest

from intro_topic.cog import _compose_poll_header, _compose_text_body, _with_role_mention
from intro_topic.topic_generator import TOPIC_FORMATS, TopicResult

TITLE = "💭 今日のお題"
FOOTER = "一言でも、リアクションだけでも歓迎"
LEAD_IN = "最近、AIに任せられることがどんどん増えていますね。"
QUESTION = "最近撮った写真は？"
THEME = "カメラロール"


def _result(fmt: str = "values", *, lead_in: str = "", theme: str = "") -> TopicResult:
    return TopicResult(
        extracted_interests=["写真"],
        lead_in=lead_in,
        topic_question=QUESTION,
        used_search=False,
        format=fmt,
        theme=theme,
    )


# --- 固定の器 -------------------------------------------------------------


@pytest.mark.parametrize("fmt", TOPIC_FORMATS)
def test_タイトルは形式によらず固定(fmt):
    body = _compose_text_body(_result(fmt, lead_in=LEAD_IN, theme=THEME), TITLE)
    assert body.splitlines()[0] == f"{TITLE}：{THEME}"


def test_タイトルは設定で差し替えられる():
    assert _compose_text_body(_result(), "今週のひとこと") == f"今週のひとこと\n**{QUESTION}**"


# --- 見出しのテーマ語 -----------------------------------------------------


def test_テーマ語は見出しに続けて出す():
    # 通知・チャンネル一覧のプレビューは本文の先頭しか出ないので、そこに題材を載せる
    text = _compose_text_body(_result(theme=THEME), TITLE)
    assert text == f"{TITLE}：{THEME}\n**{QUESTION}**"


def test_テーマ語がなければ見出しだけになる():
    assert _compose_text_body(_result(), TITLE).splitlines()[0] == TITLE


def test_空白だけのテーマ語は付かない():
    assert _compose_text_body(_result(theme="  "), TITLE) == _compose_text_body(_result(), TITLE)


def test_差し替えたタイトルにもテーマ語が付く():
    # TOPIC_TITLE は運用者が変えられるので、どの見出しでも結合できること
    text = _compose_text_body(_result(theme=THEME), "今週のひとこと")
    assert text.splitlines()[0] == f"今週のひとこと：{THEME}"


# --- 通常投稿 -------------------------------------------------------------


def test_リード文ありの本文():
    text = _compose_text_body(_result(lead_in=LEAD_IN), TITLE)
    assert text == f"{TITLE}\n{LEAD_IN}\n**{QUESTION}**"


def test_リード文なしならタイトルの次が問い():
    assert _compose_text_body(_result(), TITLE) == f"{TITLE}\n**{QUESTION}**"


def test_フッタはサブテキスト行として末尾に付く():
    text = _compose_text_body(_result(lead_in=LEAD_IN), TITLE, FOOTER)
    assert text == f"{TITLE}\n{LEAD_IN}\n**{QUESTION}**\n-# {FOOTER}"


def test_空白だけのフッタは付かない():
    assert _compose_text_body(_result(), TITLE, "   ") == _compose_text_body(_result(), TITLE)


# --- 投票投稿 -------------------------------------------------------------


def test_投票の本文には問いを入れない():
    header = _compose_poll_header(_result("choice", lead_in="どっち派？"), TITLE)
    assert header == f"{TITLE}\nどっち派？"
    assert QUESTION not in header


def test_投票でリード文がなければタイトルだけ():
    assert _compose_poll_header(_result("choice"), TITLE) == TITLE


def test_投票の本文の見出しにもテーマ語が付く():
    header = _compose_poll_header(_result("choice", lead_in="どっち派？", theme=THEME), TITLE)
    assert header == f"{TITLE}：{THEME}\nどっち派？"


def test_投票の本文にもサブテキストが付く():
    header = _compose_poll_header(_result("choice", lead_in="どっち派？"), TITLE, FOOTER)
    assert header == f"{TITLE}\nどっち派？\n-# {FOOTER}"


def test_投票でリード文がなくてもサブテキストは付く():
    assert _compose_poll_header(_result("choice"), TITLE, FOOTER) == f"{TITLE}\n-# {FOOTER}"


# --- オプトイン通知 -------------------------------------------------------


def test_通知ロールが未設定ならメンション行は入らない():
    body = _compose_text_body(_result(), TITLE)
    assert _with_role_mention(body, 0) == body


def test_通知ロール設定時は先頭にメンション行が入る():
    body = _compose_text_body(_result(), TITLE)
    assert _with_role_mention(body, 123) == f"<@&123>\n{body}"


def test_投票の本文にもメンション行が入る():
    header = _compose_poll_header(_result("choice", lead_in="どっち派？"), TITLE)
    assert _with_role_mention(header, 123).splitlines() == ["<@&123>", TITLE, "どっち派？"]
