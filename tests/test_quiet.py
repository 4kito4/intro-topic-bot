"""静穏判定（_quiet_blocked_reason）のテスト。"""

from __future__ import annotations

from store import State


def _state(iso, *offsets_minutes):
    return State(chat_activity=[iso(minutes=m) for m in offsets_minutes])


def test_発言がなければ静穏(make_bot, now):
    bot = make_bot(State())
    assert bot._quiet_blocked_reason(now) is None


def test_ウィンドウ外の発言は無視される(make_bot, now, iso):
    # activity_window_minutes=60 より古い発言しかない
    bot = make_bot(_state(iso, 90, 120))
    assert bot._quiet_blocked_reason(now) is None


def test_会話中は45分の静穏を要求する(make_bot, now, iso):
    # 直近60分に3件（busy_threshold=3）= 会話中。最終発言から30分では足りない
    bot = make_bot(_state(iso, 30, 40, 50))
    reason = bot._quiet_blocked_reason(now)
    assert reason is not None
    assert "会話中" in reason
    assert "必要45分" in reason


def test_会話中でも45分経てば投稿できる(make_bot, now, iso):
    bot = make_bot(_state(iso, 45, 50, 55))
    assert bot._quiet_blocked_reason(now) is None


def test_まばらなら15分で投稿できる(make_bot, now, iso):
    bot = make_bot(_state(iso, 20, 40))
    assert bot._quiet_blocked_reason(now) is None


def test_まばらでも15分未満は待つ(make_bot, now, iso):
    bot = make_bot(_state(iso, 10, 40))
    reason = bot._quiet_blocked_reason(now)
    assert reason is not None
    assert "まばら" in reason
    assert "必要15分" in reason
