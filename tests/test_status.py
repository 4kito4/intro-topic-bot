"""/topic status の整形（_format_status_summary）のテスト。

コマンドのガワ（interaction のやり取り）は Discord API に触れるためテスト対象外。
"""

from __future__ import annotations

from types import SimpleNamespace

from intro_topic.cog import _format_status_summary
from intro_topic.store import State


def _with(settings, **overrides):
    return SimpleNamespace(**{**vars(settings), **overrides})


# --- 秘密の扱い -----------------------------------------------------------


def test_トークンとAPIキーの値は出さない(settings):
    text = _format_status_summary(settings, State())
    assert "DISCORD-TOKEN-SECRET" not in text
    assert "GEMINI-KEY-SECRET" not in text


# --- モード ---------------------------------------------------------------


def test_キー未設定なら定型お題モードと出す(settings):
    text = _format_status_summary(_with(settings, gemini_api_key=""), State())
    assert "定型お題モード" in text
    assert "GEMINI_API_KEY" in text  # 設定の有無だけを出す


def test_キーがあればモデル名を出す(settings):
    text = _format_status_summary(settings, State())
    assert "モデル=m" in text
    assert "定型お題モード" not in text


def test_一時停止中が分かる(settings):
    assert "一時停止中" in _format_status_summary(settings, State(paused=True))
    assert "稼働中" in _format_status_summary(settings, State())


def test_DRY_RUNと投票の有無が分かる(settings):
    text = _format_status_summary(_with(settings, dry_run=True, use_poll=False), State())
    assert "DRY_RUN=True" in text
    assert "投票=無効" in text


def test_通知ロールは未設定ならなしと出す(settings):
    assert "通知ロール=なし" in _format_status_summary(settings, State())
    assert "通知ロール=555" in _format_status_summary(
        _with(settings, topic_ping_role_id=555), State()
    )


# --- 設定値 ---------------------------------------------------------------


def test_チャンネルIDを出す(settings):
    text = _format_status_summary(settings, State())
    assert "投稿先=222" in text
    assert "自己紹介=111" in text
    assert "ニュース=なし" in text
    assert "運用ログ=なし" in text


def test_ニュースチャンネルを設定していればIDを出す(settings):
    text = _format_status_summary(_with(settings, news_channel_id=333), State())
    assert "ニュース=333" in text


def test_管理コマンドの実行者を出す(settings):
    # 運営が複数人のとき「自分が実行できる側にいるか」を Discord 上から確かめられるように
    text = _format_status_summary(
        _with(settings, owner_user_id=42, owner_role_id=777), State()
    )
    assert "ユーザー=42" in text
    assert "運用ロール=777" in text


def test_実行者が未設定なら未設定と出す(settings):
    text = _format_status_summary(settings, State())  # どちらも 0
    assert "ユーザー=未設定" in text
    assert "運用ロール=なし" in text


def test_投稿タイミングを出す(settings):
    text = _format_status_summary(settings, State())
    assert "間隔48時間" in text
    assert "19〜22時 JST" in text
    assert "検知から30分後以降" in text


def test_静穏パラメータを出す(settings):
    text = _format_status_summary(settings, State())
    assert "直近60分に3件以上" in text
    assert "45分" in text
    assert "まばらなら15分" in text


def test_計測の時点を出す(settings):
    text = _format_status_summary(settings, State())
    assert "6時間後 → 24時間後" in text
    assert "最終計測から72時間" in text


def test_1点計測なら2点目を出さない(settings):
    text = _format_status_summary(_with(settings, measure_final_after_hours=0), State())
    assert "6時間後（1点計測）" in text


def test_自己紹介の扱いを出す(settings):
    text = _format_status_summary(settings, State())
    assert "最小50文字" in text
    assert "保持90日" in text
    assert "オプトアウト🚫" in text
