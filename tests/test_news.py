"""ニュースを素材にするお題生成のテスト。

Gemini は呼ばないので、プロンプトの組み立て（_build_prompt）だけを純関数として検証する。
"""

from __future__ import annotations

from topic_generator import _build_prompt

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
