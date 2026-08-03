"""運用ログの embed 化（色・切り詰め・テキストフォールバック）のテスト。

送信そのもの（チャンネル取得・send）は Discord API に触れるためテスト対象外。
Embed の生成は API を呼ばないので純ロジックとして検証できる。
"""

from __future__ import annotations

import pytest

from bot import (
    EMBED_DESCRIPTION_MAX,
    LOG_COLORS,
    LOG_TEXT_MAX,
    _log_embed,
    _log_fallback_text,
    _startup_summary,
    _truncate,
)
from store import State

# --- 色 -------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["success", "info", "warning", "error"])
def test_種別ごとに色が決まる(kind):
    embed = _log_embed(kind, "タイトル", "本文")
    assert embed.color is not None
    assert embed.color.value == LOG_COLORS[kind]


def test_種別ごとに違う色が割り当てられている():
    assert len(set(LOG_COLORS.values())) == len(LOG_COLORS)


def test_タイトルと本文がそのまま入る():
    embed = _log_embed("info", "タイトル", "本文")
    assert embed.title == "タイトル"
    assert embed.description == "本文"


# --- 切り詰め -------------------------------------------------------------


@pytest.mark.parametrize(
    ("length", "expected"),
    [(9, 9), (10, 10), (11, 10)],  # 上限ちょうどは切らない、超えたら省略記号込みで上限
)
def test_上限を超えた分だけ切り詰める(length, expected):
    assert len(_truncate("あ" * length, 10)) == expected


def test_切り詰めたことが分かるようにする():
    assert _truncate("あ" * 11, 10).endswith("…")


def test_embedの本文は上限内に収まる():
    embed = _log_embed("error", "タイトル", "あ" * (EMBED_DESCRIPTION_MAX + 100))
    assert embed.description is not None
    assert len(embed.description) == EMBED_DESCRIPTION_MAX


def test_フォールバックはタイトル込みで上限内に収まる():
    text = _log_fallback_text("タイトル", "あ" * (LOG_TEXT_MAX + 100))
    assert len(text) == LOG_TEXT_MAX


def test_フォールバックにはタイトルも入る():
    assert _log_fallback_text("タイトル", "本文") == "**タイトル**\n本文"


# --- 起動通知 -------------------------------------------------------------


def test_起動通知に状態が並ぶ(make_queue_item):
    state = State(paused=True, queue=[make_queue_item()])
    assert _startup_summary(state, dry_run=False) == "DRY_RUN=False / paused=True / キュー1件"


def test_起動通知はキューが空でも出せる():
    assert _startup_summary(State(), dry_run=True) == "DRY_RUN=True / paused=False / キュー0件"
