"""管理コマンド（/topic）の認可判定と応答整形のテスト。

interaction のやり取りはコマンドのガワ側にあり Discord API を叩くのでテスト対象外。
判定・整形はすべて純関数に切り出してあるのでここで検証する。
"""

from __future__ import annotations

from intro_topic.cog import _authorization_error, _format_queue_summary, _format_stats_summary
from intro_topic.store import PendingMeasurement, State, TopicStat

OWNER = 42
MEASURED_AT = "2026-07-30T11:00:00+00:00"


def _stat(topic="お題", fmt="choice", *, replies=0, reactions=0, votes=0, at_hours=6):
    return TopicStat(
        topic=topic,
        format=fmt,
        replies=replies,
        reactions=reactions,
        votes=votes,
        measured_at=MEASURED_AT,
        at_hours=at_hours,
    )


# --- 認可 -----------------------------------------------------------------


def test_オーナー本人だけが実行できる():
    assert _authorization_error(OWNER, OWNER) is None


def test_他人には理由を返す():
    error = _authorization_error(999, OWNER)
    assert error is not None
    assert "運用者" in error


def test_オーナー未設定なら設定方法を返す():
    error = _authorization_error(999, 0)
    assert error is not None
    assert "OWNER_USER_ID" in error


def test_オーナー未設定ならオーナー本人でも実行できない():
    # OWNER_USER_ID=0 と user_id が偶然一致する事故を防ぐ
    assert _authorization_error(0, 0) is not None


# --- /topic stats ---------------------------------------------------------


def test_計測結果がなくても出せる(settings):
    text = _format_stats_summary(State(), settings, None)
    assert "まだ計測結果がありません" in text
    assert "投稿できます" in text
    # 観測ゼロなら全形式が一様（現行の挙動と同じ）
    assert text.count("重み1.00") == 5


def test_投稿できない理由をそのまま出す(settings):
    text = _format_stats_summary(State(), settings, "投稿時間帯外 (10時 JST)")
    assert "投稿時間帯外 (10時 JST)" in text


def test_一時停止中であることが分かる(settings):
    assert "一時停止中" in _format_stats_summary(State(paused=True), settings, None)
    assert "稼働中" in _format_stats_summary(State(), settings, None)


def test_形式ごとの件数と平均が出る(settings):
    state = State(
        topic_stats=[
            _stat("A", "choice", replies=2),  # スコア6
            _stat("B", "choice", reactions=4),  # スコア4
            _stat("C", "values", reactions=1),  # スコア1
        ]
    )
    text = _format_stats_summary(state, settings, None)
    assert "二択対立型: 2件 / 平均5.0" in text
    assert "価値観型: 1件 / 平均1.0" in text
    assert "経験共有型: 0件 / 平均-" in text


def test_反応が良い形式ほど重みが大きい(settings):
    state = State(
        topic_stats=[_stat("A", "choice", replies=5), _stat("B", "values", reactions=0)]
    )
    text = _format_stats_summary(state, settings, None)
    choice = next(line for line in text.splitlines() if "二択対立型" in line)
    values = next(line for line in text.splitlines() if "価値観型" in line)
    assert float(choice.split("重み")[1]) > float(values.split("重み")[1])


def test_形式別集計は1点目の計測だけを見る(settings):
    state = State(topic_stats=[_stat("A", "choice", replies=9, at_hours=24)])
    text = _format_stats_summary(state, settings, None)
    assert "二択対立型: 0件" in text


def test_直近お題は2点の計測を並べて出す(settings):
    state = State(
        topic_stats=[
            _stat("お題A", replies=1, at_hours=6),  # スコア3
            _stat("お題A", replies=2, at_hours=24),  # スコア6
        ]
    )
    assert "- お題A: 6h=3 → 24h=6" in _format_stats_summary(state, settings, None)


def test_2点目が未計測なら1点目だけ出す(settings):
    state = State(topic_stats=[_stat("お題A", replies=1, at_hours=6)])
    assert "- お題A: 6h=3" in _format_stats_summary(state, settings, None)


def test_直近お題は最新10件で打ち切る(settings):
    state = State(topic_stats=[_stat(f"お題{i:02d}") for i in range(15)])
    text = _format_stats_summary(state, settings, None)
    assert "お題00" not in text
    assert "お題04" not in text
    assert "お題05" in text
    assert "お題14" in text


def test_長いお題は切り詰める(settings):
    state = State(topic_stats=[_stat("あ" * 100)])
    assert "…" in _format_stats_summary(state, settings, None)


# --- /topic queue ---------------------------------------------------------


def test_キューの内訳を出す(make_queue_item, settings, now, iso):
    state = State(
        queue=[
            make_queue_item(101, minutes_ago=120),  # 投稿可能
            make_queue_item(102, minutes_ago=5),  # 遅延待ち（min_delay_minutes=30）
            make_queue_item(103, minutes_ago=120, next_retry_at=iso(minutes=-10)),  # 未来
        ]
    )
    text = _format_queue_summary(state, now, settings)
    assert "**キュー**: 3件（投稿可能1 / 遅延待ち1 / 生成リトライ待ち1）" in text


def test_プールと計測待ちの件数を出す(make_pool_item, settings, now):
    state = State(
        intro_pool=[make_pool_item(201), make_pool_item(202)],
        pending_measurements=[
            PendingMeasurement(message_id=1, topic="t", format="choice", posted_at=MEASURED_AT)
        ],
    )
    text = _format_queue_summary(state, now, settings)
    assert "**再利用プール**: 2件" in text
    assert "**計測待ち**: 1件" in text


def test_自己紹介の本文は出さない(make_queue_item, make_pool_item, settings, now):
    state = State(queue=[make_queue_item(101)], intro_pool=[make_pool_item(201)])
    text = _format_queue_summary(state, now, settings)
    assert "aaa" not in text
    assert "bbb" not in text


def test_前回と次回の投稿時刻を出す(settings, now, iso):
    text = _format_queue_summary(State(last_posted_at=iso(hours=1)), now, settings)
    # NOW は JST 20:00。1時間前に投稿し、投稿間隔は48時間
    assert "**前回の投稿**: 07-30 19:00 JST" in text
    assert "**次に投稿できる時刻**: 08-01 19:00 JST" in text


def test_投稿履歴がなければ条件次第と出す(settings, now):
    text = _format_queue_summary(State(), now, settings)
    assert "**前回の投稿**: なし" in text
    assert "**次に投稿できる時刻**: 条件が揃い次第" in text


def test_投稿間隔を過ぎていれば条件次第と出す(settings, now, iso):
    text = _format_queue_summary(State(last_posted_at=iso(days=3)), now, settings)
    assert "**次に投稿できる時刻**: 条件が揃い次第" in text


def test_キューでも一時停止中が分かる(settings, now):
    assert "一時停止中" in _format_queue_summary(State(paused=True), now, settings)


# --- 一時停止と投稿条件 ---------------------------------------------------


def test_一時停止中は投稿条件を満たさない(make_bot):
    bot = make_bot(State(paused=True))
    reason = bot._blocked_reason()
    assert reason is not None
    assert "一時停止中" in reason


def test_再開すれば投稿できる(make_bot):
    assert make_bot(State(paused=False))._blocked_reason() is None


def test_一時停止は他の条件より先に判定される(make_bot, iso):
    # 投稿間隔も満たしていないが、理由としては一時停止を先に出す
    bot = make_bot(State(paused=True, last_posted_at=iso(hours=1)))
    assert "一時停止中" in (bot._blocked_reason() or "")
