"""本体の Cog。自己紹介を検知し、雑談チャンネルが静かなときに話題を投稿する。

ホスト bot（単体起動の `bot.py` でも既存 bot でも）へ `load_extension` で載せる前提。
イベントはすべて `Cog.listener` なので、ホスト bot 側の `on_message` 等を奪わない。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Settings
from .store import (
    PendingMeasurement,
    PoolItem,
    QueueItem,
    State,
    TopicStat,
    load_state,
    save_state,
)
from .topic_generator import (
    FORMAT_LABELS,
    TOPIC_FORMATS,
    ReviewResult,
    TopicFormat,
    TopicResult,
    fallback_topic,
    generate_topic,
)

logger = logging.getLogger("intro_topic_bot")

COG_NAME = "IntroTopic"  # ホスト bot から get_cog(COG_NAME) で取り出すための名前

JST = timezone(timedelta(hours=9), name="JST")
MAX_RETRIES = 3
RETRY_BACKOFF_MINUTES = (5, 20, 60)  # 生成失敗が続いたときに次の試行まで空ける時間
BACKFILL_LIMIT = 50  # 起動時にオフライン中の自己紹介を拾う件数
# /topic backfill で指定できる走査件数の範囲（既定値は .env の BACKFILL_SCAN_LIMIT）
BACKFILL_SCAN_MIN = 1
BACKFILL_SCAN_MAX = 500
RECENT_TOPICS_KEPT = 10  # 多様性確保のためプロンプトに渡す直近お題の件数
RECENT_FORMATS_AVOIDED = 2  # 次のお題形式を選ぶときに避ける直近形式の件数
FALLBACK_SOURCE = "定型"  # 内蔵の定型お題を投稿したときのログ・応答に出す種別

# ニュース素材（NEWS_CHANNEL_ID を設定したときだけ使う）。
# 運用で調整する値ではないのでモジュール定数にする（.env に出さない）
NEWS_FETCH_LIMIT = 10  # ニュースチャンネルを遡って本文を探す件数
NEWS_MAX_AGE_DAYS = 3  # これより古い投稿は「直近のニュース」とみなさない
NEWS_TEXT_MAX = 1000  # 生成プロンプトへ渡すニュース本文の上限
NEWS_SOURCE = "ニュース"  # ニュースからお題を作ったときのログ・応答に出す種別
# 元メッセージを取り直せなかった周回の応答。本文を失わないよう破棄せず次の周回に回す
SKIP_SUMMARY = "投稿を見送りました: 元の自己紹介を再取得できませんでした"

# Discord の投票の制約（question 300 文字 / answer 55 文字 / 選択肢は最大10件）
POLL_DURATION = timedelta(hours=48)
POLL_QUESTION_MAX_LEN = 300
POLL_OPTION_MAX_LEN = 55
POLL_MIN_OPTIONS = 2
POLL_MAX_OPTIONS = 4

MEASURE_HISTORY_LIMIT = 100  # 返信数を数えるときに遡る雑談チャンネルの件数
TOPIC_STATS_KEPT = 60  # 反応の計測結果を保持する件数（1お題あたり最大2行のため保持お題数は従来同等）
GOOD_EXAMPLES_KEPT = 3  # few-shot として生成プロンプトに渡すお題の件数
REPLY_WEIGHT = 3  # 返信は最も価値の高い反応
VOTE_WEIGHT = 2  # 次に投票、リアクションは 1
# 形式の重み付けを平滑化する仮想サンプル数。運用チューニング対象ではないので .env に出さない
FORMAT_PRIOR_WEIGHT = 2

RefreshVerdict = Literal["ok", "optout", "too_short"]
RefreshOutcome = Literal["ok", "discarded", "skip"]
BackfillVerdict = Literal["ok", "bot", "dup", "too_old", "optout", "short"]
LogKind = Literal["success", "info", "warning", "error"]

# 運用ログの色。表示のための定数であり運用で調整する閾値ではないので .env には出さない
LOG_COLORS: dict[LogKind, int] = {
    "success": 0x57F287,  # 緑: 正常に完了した
    "info": 0x99AAB5,  # 灰: 記録だけ
    "warning": 0xFEE75C,  # 黄: 投稿しなかった・後で再試行する
    "error": 0xED4245,  # 赤: 人が対処する必要がある
}
EMBED_DESCRIPTION_MAX = 4096  # Discord の embed description 上限
LOG_TEXT_MAX = 1900  # フォールバック時の上限（content 2000 制限に余裕を持たせる）
INTERACTION_TEXT_MAX = 1900  # スラッシュコマンドの応答上限（同じく 2000 に余裕を持たせる）
STATS_RECENT_TOPICS = 10  # /topic stats に並べる直近お題の件数
STATS_TOPIC_MAX_LEN = 40  # 同上。1行が長くなりすぎないよう切り詰める


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _valid_poll_options(options: list[str] | None) -> bool:
    """Discord の投票に使える選択肢か判定する（2〜4件、空文字なし、各55文字以内）。"""
    if options is None or not (POLL_MIN_OPTIONS <= len(options) <= POLL_MAX_OPTIONS):
        return False
    return all(bool(o.strip()) and len(o) <= POLL_OPTION_MAX_LEN for o in options)


def _reaction_score(stat: TopicStat) -> int:
    """お題の反応の強さ。会話が続いた返信を最も高く評価する。"""
    return stat.replies * REPLY_WEIGHT + stat.votes * VOTE_WEIGHT + stat.reactions


def _review_label(review: ReviewResult | None) -> str:
    if review is None:
        return "未実施"
    return "合格" if review.approved else "不合格"


def _compose_body(title: str, lead_in: str, question: str | None, footer: str) -> str:
    """投稿の器。毎回同じ並び（タイトル → 導入文 → 太字の問い → サブテキスト）にする。

    形式によって見た目を変えない（連投しても同じ枠に見えるほうが定期投稿として自然）。
    空の要素は行ごと飛ばす。question=None は投票用（問いは投票側に入るので本文に入れない）。
    """
    footer = footer.strip()
    lines = [
        title,
        lead_in,
        f"**{question}**" if question else "",
        f"-# {footer}" if footer else "",  # -# は Discord のサブテキスト（小さい薄字）記法
    ]
    return "\n".join(line for line in lines if line)


def _title_with_theme(title: str, theme: str) -> str:
    """見出し行。テーマ語があれば「見出し：テーマ語」にする。

    通知とチャンネル一覧のプレビューには本文の先頭しか出ないので、そこに何の話かを
    載せるための結合。TOPIC_TITLE は運用者が変えられるため、どんな見出しでも成立する
    書式にする。theme が空（モデルが返さなかった回・定型お題の想定外）なら従来の見出しだけ。
    """
    theme = theme.strip()
    return f"{title}：{theme}" if theme else title


def _compose_text_body(result: TopicResult, title: str, footer: str = "") -> str:
    """通常投稿の本文。問いは太字にして目に留まりやすくする。"""
    return _compose_body(
        _title_with_theme(title, result.theme), result.lead_in, result.topic_question, footer
    )


def _compose_poll_header(result: TopicResult, title: str, footer: str = "") -> str:
    """投票として投稿するときの本文。問いは投票側に入るので本文には入れない。"""
    return _compose_body(_title_with_theme(title, result.theme), result.lead_in, None, footer)


def _with_role_mention(body: str, role_id: int) -> str:
    """オプトイン通知。ロールが設定されているときだけ本文の先頭にメンション行を足す。

    通知を受け取るかはメンバーがそのロールを付けるかで決まるので、ステルス設計は崩れない
    （個人へのメンションではなく、通知を希望した人だけが入るロールを呼ぶ）。
    """
    return f"<@&{role_id}>\n{body}" if role_id else body


def _format_weights(
    stats: list[TopicStat], at_hours: int, formats: Sequence[TopicFormat]
) -> list[float]:
    """お題形式ごとの選択重み。反応スコアの平均をベイズ平滑化して求める。

    観測の少ない形式は prior（全体平均）へ寄るので、少数の当たり外れで形式が固定されない。
    at_hours の行だけを使う（同じお題が2行あるため、混ぜると比較が新旧値の混在になる）。
    """
    target = [s for s in stats if s.at_hours == at_hours]
    if not target:
        return [1.0] * len(formats)
    prior = sum(_reaction_score(s) for s in target) / len(target)
    if prior <= 0:
        # 全お題が反応ゼロでも重みが全ゼロ（random.choices が ValueError）にならないようにする
        prior = 1.0
    weights = []
    for fmt in formats:
        scores = [_reaction_score(s) for s in target if s.format == fmt]
        weights.append(
            (FORMAT_PRIOR_WEIGHT * prior + sum(scores)) / (FORMAT_PRIOR_WEIGHT + len(scores))
        )
    return weights


def _measure_checkpoints(settings: Settings) -> tuple[int, ...]:
    """反応を計測する時点（投稿からの経過時間）。MEASURE_FINAL_AFTER_HOURS が 0 なら1点計測。"""
    if settings.measure_final_after_hours <= 0:
        return (settings.measure_after_hours,)
    return (settings.measure_after_hours, settings.measure_final_after_hours)


def _due_checkpoint_hours(
    pending: PendingMeasurement, now: datetime, checkpoints: tuple[int, ...]
) -> int | None:
    """次に計測すべき時点を返す。まだ来ていない / 全て計測済みなら None。"""
    if pending.measured_count >= len(checkpoints):
        return None
    hours = checkpoints[pending.measured_count]
    elapsed = now - datetime.fromisoformat(pending.posted_at)
    return hours if elapsed >= timedelta(hours=hours) else None


def _is_measure_given_up(
    pending: PendingMeasurement, now: datetime, checkpoints: tuple[int, ...], giveup_hours: int
) -> bool:
    """最終計測時点から猶予を過ぎたか。取得失敗が続く pending を無限リトライさせない。"""
    elapsed = now - datetime.fromisoformat(pending.posted_at)
    return elapsed > timedelta(hours=checkpoints[-1] + giveup_hours)


def _truncate(text: str, limit: int) -> str:
    """上限を超える文字列を省略記号付きで切り詰める。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _log_embed(kind: LogKind, title: str, body: str) -> discord.Embed:
    """運用ログの embed。種別ごとに色を変え、一覧から異常を拾いやすくする。"""
    return discord.Embed(
        title=title,
        description=_truncate(body, EMBED_DESCRIPTION_MAX),
        color=LOG_COLORS[kind],
    )


def _log_fallback_text(title: str, body: str) -> str:
    """Embed Links 権限がないときの代替テキスト。タイトル込みで content 上限に収める。"""
    return _truncate(f"**{title}**\n{body}", LOG_TEXT_MAX)


def _startup_summary(state: State, dry_run: bool) -> str:
    """起動通知の本文。意図しない再起動に気づけるよう、そのときの状態を並べる。"""
    return f"DRY_RUN={dry_run} / paused={state.paused} / キュー{len(state.queue)}件"


def _role_ids(user: discord.User | discord.Member) -> list[int]:
    """実行者が持つロールの ID。

    `/topic` は guild_only なので実行者は通常 Member だが、型の上では User になる経路が
    残る（ロールという概念がない）。その場合はロール無しとして扱い、落とさない。
    """
    return [role.id for role in getattr(user, "roles", ())]


def _authorization_error(
    user_id: int, role_ids: Sequence[int], owner_user_id: int, owner_role_id: int
) -> str | None:
    """管理コマンドを実行してよいか判定する。駄目なら理由（実行者に返す文面）を返す。

    `OWNER_USER_ID` 本人か、`OWNER_ROLE_ID` のロールを持つメンバーなら実行できる（OR 判定）。
    運営が複数人いる場合はロール側だけを設定する運用もできる。
    未設定（0）の側は一致判定に使わない: ID が 0 の実行者・ロールと偶然一致する事故を防ぐため。
    """
    if owner_user_id == 0 and owner_role_id == 0:
        return (
            "OWNER_USER_ID / OWNER_ROLE_ID がどちらも未設定のため管理コマンドを使えません。"
            "運用者のユーザー ID か運用ロールの ID を設定して Bot を再起動してください。"
        )
    if owner_user_id != 0 and user_id == owner_user_id:
        return None
    if owner_role_id != 0 and owner_role_id in role_ids:
        return None
    return "このコマンドは Bot の運用者だけが実行できます。"


def _jst_text(when: datetime) -> str:
    return f"{when.astimezone(JST):%m-%d %H:%M} JST"


def _recent_topic_lines(stats: list[TopicStat]) -> list[str]:
    """直近のお題を「6h→24h」の形で並べる。1お題が複数行あるのでお題ごとにまとめる。"""
    grouped: dict[str, dict[int, int]] = {}
    for stat in stats:
        grouped.setdefault(stat.topic, {})[stat.at_hours] = _reaction_score(stat)
    lines = []
    for topic, scores in list(grouped.items())[-STATS_RECENT_TOPICS:]:
        measured = " → ".join(f"{hours}h={scores[hours]}" for hours in sorted(scores))
        lines.append(f"- {_truncate(topic, STATS_TOPIC_MAX_LEN)}: {measured}")
    return lines


def _format_stats_summary(
    state: State, settings: Settings, blocked_reason: str | None
) -> str:
    """/topic stats の本文。学習の中身と「今投稿できない理由」を運用者に見せる。"""
    at_hours = settings.measure_after_hours
    lines = [
        f"**状態**: {'一時停止中' if state.paused else '稼働中'} / DRY_RUN={settings.dry_run}",
        f"**投稿可否**: {blocked_reason or '投稿できます'}",
        "",
        f"**形式別の反応**（{at_hours}時間後の計測。重みは次のお題の選ばれやすさ）",
    ]
    weights = _format_weights(state.topic_stats, at_hours, TOPIC_FORMATS)
    for fmt, weight in zip(TOPIC_FORMATS, weights):
        scores = [
            _reaction_score(s)
            for s in state.topic_stats
            if s.at_hours == at_hours and s.format == fmt
        ]
        average = f"{sum(scores) / len(scores):.1f}" if scores else "-"
        lines.append(
            f"- {FORMAT_LABELS[fmt]}: {len(scores)}件 / 平均{average} / 重み{weight:.2f}"
        )
    lines += ["", f"**直近のお題**（最新{STATS_RECENT_TOPICS}件）"]
    lines += _recent_topic_lines(state.topic_stats) or ["- まだ計測結果がありません"]
    return _truncate("\n".join(lines), INTERACTION_TEXT_MAX)


def _format_queue_summary(state: State, now: datetime, settings: Settings) -> str:
    """/topic queue の本文。ステルス設計のため自己紹介の本文は出さず件数だけを見せる。"""
    ready = delayed = backoff = 0
    for item in state.queue:
        if item.next_retry_at is not None and datetime.fromisoformat(item.next_retry_at) > now:
            # 遅延待ちも兼ねている場合は、より説明力のあるバックオフ側で数える
            backoff += 1
        elif now - item.created_dt() < timedelta(minutes=settings.min_delay_minutes):
            delayed += 1
        else:
            ready += 1
    if state.last_posted_at is None:
        last_posted, next_post = "なし", "条件が揃い次第"
    else:
        posted_at = datetime.fromisoformat(state.last_posted_at)
        next_at = posted_at + timedelta(hours=settings.post_interval_hours)
        last_posted = _jst_text(posted_at)
        next_post = "条件が揃い次第" if next_at <= now else _jst_text(next_at)
    lines = [
        f"**状態**: {'一時停止中' if state.paused else '稼働中'}",
        f"**キュー**: {len(state.queue)}件"
        f"（投稿可能{ready} / 遅延待ち{delayed} / 生成リトライ待ち{backoff}）",
        f"**再利用プール**: {len(state.intro_pool)}件",
        f"**計測待ち**: {len(state.pending_measurements)}件",
        f"**前回の投稿**: {last_posted}",
        f"**次に投稿できる時刻**: {next_post}",
    ]
    return _truncate("\n".join(lines), INTERACTION_TEXT_MAX)


def _format_status_summary(settings: Settings, state: State) -> str:
    """/topic status の本文。運用者が .env を開かずに現在の設定を確認できるようにする。

    トークン・API キーの値は絶対に出さない（設定されているかどうかだけを出す）。
    """
    modes = [
        f"DRY_RUN={settings.dry_run}",
        "一時停止中" if state.paused else "稼働中",
        f"モデル={settings.gemini_model}"
        if settings.gemini_api_key
        else "定型お題モード (GEMINI_API_KEY 未設定)",
        f"投票={'有効' if settings.use_poll else '無効'}",
        f"通知ロール={settings.topic_ping_role_id or 'なし'}",
    ]
    measure = f"{settings.measure_after_hours}時間後"
    if settings.measure_final_after_hours > 0:
        measure += f" → {settings.measure_final_after_hours}時間後"
    else:
        measure += "（1点計測）"
    lines = [
        "**モード**: " + " / ".join(modes),
        f"**チャンネル**: 投稿先={settings.chat_channel_id} / "
        f"自己紹介={settings.intro_channel_id} / "
        f"ニュース={settings.news_channel_id or 'なし'} / "
        f"運用ログ={settings.log_channel_id or 'なし'}",
        # 運営が複数人のとき「自分が実行できる側にいるか」をここで確かめられるようにする
        f"**管理コマンドの実行者**: ユーザー={settings.owner_user_id or '未設定'} / "
        f"運用ロール={settings.owner_role_id or 'なし'}",
        f"**投稿タイミング**: 間隔{settings.post_interval_hours}時間 / "
        f"{settings.post_window_start}〜{settings.post_window_end}時 JST / "
        f"検知から{settings.min_delay_minutes}分後以降",
        f"**静穏判定**: 直近{settings.activity_window_minutes}分に"
        f"{settings.busy_threshold}件以上なら会話中とみなして静穏"
        f"{settings.quiet_busy_minutes}分 / まばらなら{settings.quiet_minutes}分",
        f"**反応の計測**: {measure} / "
        f"取得できなければ最終計測から{settings.measure_giveup_hours}時間で諦める",
        f"**自己紹介**: 最小{settings.min_intro_length}文字 / "
        f"保持{settings.pool_max_age_days}日 / オプトアウト{settings.optout_emoji}",
    ]
    return _truncate("\n".join(lines), INTERACTION_TEXT_MAX)


def _judge_refresh(
    message: discord.Message, optout_emoji: str, min_intro_length: int
) -> RefreshVerdict:
    """再取得した自己紹介を使ってよいか判定する（Discord API を叩かない）。

    オプトアウトは「本人以外が付けた場合」も有効。本人判定には reaction.users() の
    追加 fetch が必要でレート制限が増えるため、まずは誰が付けても破棄とする。
    """
    if any(str(reaction.emoji) == optout_emoji for reaction in message.reactions):
        return "optout"
    if len(message.content) < min_intro_length:
        return "too_short"
    return "ok"


def _backfill_verdict(
    message: discord.Message, state: State, settings: Settings, now: datetime
) -> BackfillVerdict:
    """過去の自己紹介を再利用プールへ取り込んでよいか判定する（Discord API を叩かない）。

    除外した分は state に書かないので、同じ範囲を再実行しても取り込み済みが dup になるだけ。
    """
    if message.author.bot:
        return "bot"
    # is_processed は processed_ids とキューしか見ないので、プールは自分で確認する
    if state.is_processed(message.id) or any(
        item.message_id == message.id for item in state.intro_pool
    ):
        return "dup"
    if message.created_at <= now - timedelta(days=settings.pool_max_age_days):
        # 取り込んでも _prune_expired ですぐ消えるので入れない（保持期限と同じ向きで判定する）
        return "too_old"
    verdict = _judge_refresh(message, settings.optout_emoji, settings.min_intro_length)
    if verdict == "optout":
        return "optout"
    if verdict == "too_short":
        return "short"
    return "ok"


def _news_message_text(message: discord.Message) -> str:
    """ニュース投稿から素材にする本文を取り出す（Discord API を叩かない）。

    ニュースが本文で流れるか embed で流れるかは運用者のサーバー次第なので両方を拾い、
    空の要素は行ごと飛ばして連結する。
    """
    parts = [message.content]
    for embed in message.embeds:
        parts.append(embed.title or "")
        parts.append(embed.description or "")
    return "\n".join(part for part in (p.strip() for p in parts) if part)


class IntroTopicCog(commands.Cog, name=COG_NAME):
    """自己紹介の検知からお題投稿・反応計測までを担う本体。

    ホスト bot への影響を最小にするための約束事:
    - イベントは `Cog.listener`（追加式）だけを使う。ホスト bot の `on_message` を奪わない
    - 独自のプレフィックスコマンドを持たない。操作はすべて `/topic` に集約する
    - `tree.sync` はここから呼ばない。同期のタイミングはホスト bot の運用に任せる
    - `cog_unload` でワーカーとコマンドを完全に片付ける（load_extension の行を消せば撤収できる）
    """

    def __init__(self, bot: commands.Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self.state: State = load_state(settings.state_path)
        self.topic_commands = TopicCommands(self)
        # 投稿処理はワーカーと /topic now の両方から呼ばれるので直列化する
        self._post_lock = asyncio.Lock()
        # 再接続で on_ready が再度呼ばれても、起動通知は初回だけにする
        self._startup_done = False
        # 起動時の補完（_backfill_intros / _init_chat_activity）が終わるまでワーカーを待たせる
        self._startup_ready = asyncio.Event()

    def save(self) -> None:
        save_state(self.settings.state_path, self.state)

    # --- ライフサイクル -----------------------------------------------------

    async def cog_load(self) -> None:
        """load_extension で呼ばれる。ワーカーの起動と `/topic` のツリー登録だけを行う。

        `tree.sync` は呼ばない: 同期は API のレート制限が厳しく、ホスト bot が自分の
        タイミングでまとめて実行するのが行儀のよい形なので、こちらからは触らない。
        """
        self.bot.tree.add_command(self.topic_commands)
        self.post_worker.start()

    def cog_unload(self) -> None:
        """unload_extension で呼ばれる。ワーカーを止め、`/topic` をツリーから外す。

        止め方は `stop()`（現在の周回の完了を待つ）ではなく `cancel()` にする。
        ワーカーは5分間 sleep している時間のほうが長く、`stop()` だと実際に止まるのが
        次の周回まで遅れるため。撤収の指示に対して確実に即座に止まるほうを取る。
        """
        self.post_worker.cancel()
        self.bot.tree.remove_command(self.topic_commands.name)

    # --- イベント -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        logger.info("ログイン完了: %s", self.bot.user)
        await self._backfill_intros()
        await self._init_chat_activity()
        self._startup_ready.set()
        if self._startup_done:
            return
        self._startup_done = True
        if not self.settings.gemini_api_key:
            logger.info(
                "GEMINI_API_KEY が未設定のため定型お題モードで動作します"
                "（自己紹介は読まず、内蔵のお題だけを投稿します）"
            )
        if self.settings.owner_user_id == 0 and self.settings.owner_role_id == 0:
            logger.warning(
                "OWNER_USER_ID / OWNER_ROLE_ID がどちらも未設定です。"
                "/topic コマンドは誰も実行できません"
            )
        await self.log_event(
            "info", "起動しました", _startup_summary(self.state, self.settings.dry_run)
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.channel.id == self.settings.chat_channel_id:
            self._record_chat_activity(message.created_at)
            return
        if message.channel.id != self.settings.intro_channel_id:
            return
        self._enqueue_intro(message)

    # --- 検知・補完 ---------------------------------------------------------

    def _enqueue_intro(self, message: discord.Message) -> None:
        self.state.last_seen_at = message.created_at.isoformat()
        if len(message.content) < self.settings.min_intro_length:
            logger.info("短すぎるためスキップ: message_id=%s", message.id)
            self.state.processed_ids.append(message.id)
            self.save()
            return
        if self.state.is_processed(message.id):
            self.save()
            return
        # 同じ人が自己紹介を投稿し直した場合、古い方でお題を作らないよう差し替える。
        # 再利用プールは使用済み資産なので触らない
        for stale in [q for q in self.state.queue if q.author_id == message.author.id]:
            self.state.queue.remove(stale)
            self.state.processed_ids.append(stale.message_id)
            logger.info(
                "同一著者の旧項目を置換: 旧message_id=%s → 新message_id=%s",
                stale.message_id, message.id,
            )
        self.state.queue.append(
            QueueItem(
                message_id=message.id,
                author_id=message.author.id,
                author_display_name=message.author.display_name,
                content=message.content,
                created_at=message.created_at.isoformat(),
            )
        )
        self.save()
        logger.info("キューに追加: message_id=%s (キュー%d件)", message.id, len(self.state.queue))

    async def _backfill_intros(self) -> None:
        """Bot 停止中に投稿された自己紹介をキューに補完する。"""
        channel = self.bot.get_channel(self.settings.intro_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"自己紹介チャンネルが見つかりません: {self.settings.intro_channel_id}")
        after = (
            datetime.fromisoformat(self.state.last_seen_at)
            if self.state.last_seen_at
            else None
        )
        count = 0
        async for message in channel.history(limit=BACKFILL_LIMIT, after=after, oldest_first=True):
            if message.author.bot or self.state.is_processed(message.id):
                continue
            self._enqueue_intro(message)
            count += 1
        if count:
            logger.info("オフライン中の自己紹介を %d 件補完", count)

    async def _init_chat_activity(self) -> None:
        """Bot 停止中の雑談発言をウィンドウ分だけ履歴から取り直す。"""
        channel = self.bot.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        window_start = now_utc() - timedelta(minutes=self.settings.activity_window_minutes)
        self.state.chat_activity = []
        async for message in channel.history(limit=100, after=window_start, oldest_first=True):
            if not message.author.bot:
                self.state.chat_activity.append(message.created_at.isoformat())
        self.save()

    def _record_chat_activity(self, at: datetime) -> None:
        cutoff = at - timedelta(minutes=self.settings.activity_window_minutes)
        self.state.chat_activity = [
            t for t in self.state.chat_activity if datetime.fromisoformat(t) > cutoff
        ]
        self.state.chat_activity.append(at.isoformat())
        self.save()

    # --- 投稿ワーカー -------------------------------------------------------

    @tasks.loop(minutes=5)
    async def post_worker(self) -> None:
        """想定外の例外でループが停止しないよう、本体処理を包んで次周回へ継続する。"""
        try:
            await self._post_worker_body()
        except Exception as exc:
            logger.exception("投稿ワーカーで想定外のエラー。次周回に継続します")
            # 通知は repr だけにする（フルトレースはローカルログで追う）
            await self.log_event("error", "投稿ワーカーで想定外のエラー", repr(exc))

    @post_worker.before_loop
    async def _before_post_worker(self) -> None:
        """1周目を起動処理の後ろに回す。

        ワーカーは cog_load（＝ログイン前）で開始するので、そのまま走らせると
        チャンネルを引けない状態で1周目が動いてしまう。on_ready のキュー補完と
        雑談発言の取り直しが終わってから回し始める。
        """
        await self._startup_ready.wait()

    async def _post_worker_body(self) -> None:
        self._prune_expired()
        await self._measure_pending()
        # ロック取得後にも再判定するが、無駄なロック競合を減らすためここでも見る
        reason = self._blocked_reason()
        if reason is not None:
            logger.debug("投稿見送り: %s", reason)
            return
        await self._generate_and_post(enforce_conditions=True)

    async def _generate_and_post(self, enforce_conditions: bool = False) -> str:
        """お題を1件生成して投稿し、結果のサマリを返す（投稿しなかった場合も理由を返す）。

        投稿全体をロックで直列化する。さらに enforce_conditions=True（ワーカー経路）は
        ロックを取ってから投稿条件を再判定する: 手動投稿の生成待ち（数秒〜数十秒）の間に
        ワーカーが発火すると、古い last_posted_at で「投稿可」と判定されたまま待たされ、
        ロック解放直後に2件目が出てしまうため。手動経路は投稿条件を無視して投稿する。
        """
        async with self._post_lock:
            if enforce_conditions:
                reason = self._blocked_reason()
                if reason is not None:
                    logger.debug("投稿見送り(再判定): %s", reason)
                    return f"投稿を見送りました: {reason}"
            return await self._post_once()

    async def _post_once(self) -> str:
        """候補を選んでお題を生成し投稿する。投稿条件の判定は呼び出し側で済ませておく。"""
        if not self.settings.gemini_api_key:
            # 定型お題モード。自己紹介は読めないのでキュー・プールは消費しない
            return await self._post_fallback("GEMINI_API_KEY が未設定")
        # 優先順位: 新規キュー → 未使用プール → ニュース → 再利用プール → 汎用お題。
        # 全員の自己紹介を必ず1回は使った上で、古い素材の使い回しより新鮮なニュースを優先する。
        # 生成直前に元メッセージを取り直し、破棄された候補はその周回のうちに次へ送る
        chosen, skipped = await self._take_source(self._pick_item())
        if skipped:
            return SKIP_SUMMARY
        # プールの候補は1周回につき1件だけ試す。未使用（used_count==0）ならここで使い、
        # 使用済みならニュースが取れなかったときの控えとして残す
        pool_candidate = self._pick_pool_item() if chosen is None else None
        news: tuple[str, int] | None = None
        if pool_candidate is not None and pool_candidate.used_count == 0:
            chosen, skipped = await self._take_source(pool_candidate)
            if skipped:
                return SKIP_SUMMARY
            pool_candidate = None  # 採用済み or 破棄済みなので控えには回さない
        if chosen is None:
            news = await self._pick_news_text()
            if news is None and pool_candidate is not None:
                chosen, skipped = await self._take_source(pool_candidate)
                if skipped:
                    return SKIP_SUMMARY
        item = chosen if isinstance(chosen, QueueItem) else None
        pool_item = chosen if isinstance(chosen, PoolItem) else None
        news_text: str | None = None
        if item is not None:
            source, source_id, intro_text = "新規", item.message_id, item.content
        elif pool_item is not None:
            source, source_id, intro_text = "プール", pool_item.message_id, pool_item.content
        elif news is not None:
            news_text, news_id = news
            source, source_id, intro_text = NEWS_SOURCE, news_id, None
        else:
            source, source_id, intro_text = "汎用", None, None
        required_format = self._pick_format()
        try:
            result, review = await asyncio.to_thread(
                generate_topic,
                intro_text,
                self.settings.gemini_api_key,
                self.settings.gemini_model,
                self.state.posted_topics[-RECENT_TOPICS_KEPT:],
                required_format,
                self._good_examples(),
                news_text=news_text,
            )
        except Exception:
            if item is None:
                # プール・ニュース・汎用由来は捨てるものがないのでリトライ管理は不要
                logger.exception("話題生成に失敗 (種別=%s, message_id=%s)", source, source_id)
                return (
                    f"お題の生成に失敗しました (種別={source})\n"
                    + await self._post_fallback("お題の生成に失敗")
                )
            item.retry_count += 1
            logger.exception(
                "話題生成に失敗 (message_id=%s, %d/%d 回目)",
                item.message_id, item.retry_count, MAX_RETRIES,
            )
            log_kind: LogKind
            if item.retry_count >= MAX_RETRIES:
                logger.error("リトライ上限に達したため破棄: message_id=%s", item.message_id)
                self.state.queue.remove(item)
                self.state.processed_ids.append(item.message_id)
                log_kind = "error"
                log_title = "生成に失敗し続けたため自己紹介を破棄しました"
                log_body = f"message_id={item.message_id} / {MAX_RETRIES}回連続で失敗"
            else:
                # 失敗が続くほど間隔を空ける（API 障害中に無駄打ちしない）
                minutes = RETRY_BACKOFF_MINUTES[
                    min(item.retry_count - 1, len(RETRY_BACKOFF_MINUTES) - 1)
                ]
                next_retry = now_utc() + timedelta(minutes=minutes)
                item.next_retry_at = next_retry.isoformat()
                logger.info(
                    "次のリトライは%d分後: message_id=%s", minutes, item.message_id
                )
                log_kind = "warning"
                log_title = "お題の生成に失敗しました"
                log_body = (
                    f"message_id={item.message_id} / {item.retry_count}/{MAX_RETRIES}回目\n"
                    f"次のリトライ: {next_retry.astimezone(JST):%m-%d %H:%M} JST"
                )
            self.save()
            await self.log_event(log_kind, log_title, log_body)
            # キュー項目は破棄・バックオフのどちらでも次周回に任せ、この周回は定型お題で埋める
            return (
                f"{log_title}\n{log_body}\n" + await self._post_fallback("お題の生成に失敗")
            )

        if self.settings.dry_run:
            return "[DRY_RUN] 投稿予定\n" + await self._report_dry_run(source, result, review)

        channel = self.bot.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        posted, is_poll = await self._send_topic(channel, result)

        now = now_utc()
        self._record_post(result, posted.id, is_poll, now)
        if item is not None:
            self.state.queue.remove(item)
            self.state.processed_ids.append(item.message_id)
            # 新規が来ない期間もお題を出せるよう、本文は破棄せず再利用プールへ移す
            self.state.intro_pool.append(
                PoolItem(
                    message_id=item.message_id,
                    content=item.content,
                    created_at=item.created_at,
                    used_count=1,
                    last_used_at=now.isoformat(),
                )
            )
        elif pool_item is not None:
            pool_item.used_count += 1
            pool_item.last_used_at = now.isoformat()
        self.save()
        review_label = _review_label(review)
        logger.info(
            "話題を投稿 (種別=%s, message_id=%s, 形式=%s, 検索使用=%s, 審査=%s): %s",
            source, source_id, result.format, result.used_search, review_label,
            result.topic_question,
        )
        summary = (
            f"種別={source} / 形式={result.format} / 審査={review_label}\n"
            f"{result.topic_question}"
        )
        if review is not None and not review.approved:
            summary += f"\n審査不合格のまま採用: {review.reason}"
        await self.log_event("success", "お題を投稿しました", summary)
        return f"お題を投稿しました\n{summary}"

    def _record_post(
        self, result: TopicResult, message_id: int, is_poll: bool, now: datetime
    ) -> None:
        """投稿したお題を計測待ちと重複回避の履歴に記録する（定型お題も同じ扱い）。"""
        self.state.pending_measurements.append(
            PendingMeasurement(
                message_id=message_id,
                topic=result.topic_question,
                format=result.format,
                posted_at=now.isoformat(),
                is_poll=is_poll,
            )
        )
        self.state.last_posted_at = now.isoformat()
        self.state.posted_topics = (self.state.posted_topics + [result.topic_question])[
            -RECENT_TOPICS_KEPT:
        ]
        # ローテーションは実際に投稿された形式を基準にする（申告形式を採用）
        self.state.posted_formats = (self.state.posted_formats + [result.format])[
            -RECENT_TOPICS_KEPT:
        ]

    async def _post_fallback(self, reason: str) -> str:
        """内蔵の定型お題を投稿する。Gemini が使えない周回でも定期投稿を絶やさない。

        自己紹介を読んでいないので、キュー・プールの項目は消費しない
        （キュー項目のリトライ管理は呼び出し側の既存処理に任せる）。
        """
        result = fallback_topic(self.state.posted_topics[-RECENT_TOPICS_KEPT:])
        if self.settings.dry_run:
            return "[DRY_RUN] 投稿予定\n" + await self._report_dry_run(
                FALLBACK_SOURCE, result, None
            )
        channel = self.bot.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        posted, is_poll = await self._send_topic(channel, result)
        self._record_post(result, posted.id, is_poll, now_utc())
        self.save()
        logger.info(
            "定型お題を投稿 (理由=%s, message_id=%s, 形式=%s): %s",
            reason, posted.id, result.format, result.topic_question,
        )
        summary = (
            f"種別={FALLBACK_SOURCE} / 形式={result.format} / 理由={reason}\n"
            f"{result.topic_question}"
        )
        # 投稿自体は成功しているので success。問題がある場合は直前に別の警告を出している
        await self.log_event("success", "定型お題を投稿しました", summary)
        return f"定型お題を投稿しました\n{summary}"

    async def _report_dry_run(
        self, source: str, result: TopicResult, review: ReviewResult | None
    ) -> str:
        """DRY_RUN: 投稿予定の内容を出すだけで state は一切変更しない。戻り値は予定の文面。"""
        as_poll = (
            self.settings.use_poll
            and result.format == "choice"
            and _valid_poll_options(result.poll_options)
        )
        lines = [
            f"種別={source} / 形式={result.format} / 審査={_review_label(review)}",
            "",
            _compose_poll_header(
                result, self.settings.topic_title, self.settings.topic_footer
            )
            if as_poll
            else _compose_text_body(
                result, self.settings.topic_title, self.settings.topic_footer
            ),
        ]
        if as_poll:
            lines.append(f"投票: {result.topic_question}")
            lines.append("選択肢: " + " / ".join(result.poll_options or []))
        if self.settings.topic_ping_role_id:
            # 注記だけにする（メンション文字列を書くとログチャンネルで実際に通知が飛ぶ）
            lines.append(f"通知: ロールID={self.settings.topic_ping_role_id}")
        if review is not None and not review.approved:
            lines.append(f"審査コメント: {review.reason}")
        text = "\n".join(lines)
        logger.info("DRY_RUN のため投稿せず: %s", text)
        await self.log_event("warning", "[DRY_RUN] 投稿予定", text)
        return text

    async def log_event(self, kind: LogKind, title: str, body: str = "") -> None:
        """運用ログを種別ごとの色付き embed でチャンネルに流す。

        except 節からも呼ぶため、送信の失敗はすべてここで握って本体・ワーカーを止めない。
        Embed Links 権限がない場合はプレーンテキストで送り直す。
        """
        if not self.settings.log_channel_id:
            return
        channel = self.bot.get_channel(self.settings.log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "ログチャンネルが見つかりません: %s", self.settings.log_channel_id
            )
            return
        try:
            await channel.send(embed=_log_embed(kind, title, body))
            return
        except discord.Forbidden as exc:
            # Embed Links 権限がないケース。テキストなら通ることがある
            logger.warning("embed を送信できないためテキストで送り直します: %s", exc)
        except discord.HTTPException as exc:
            logger.warning("ログチャンネルへの送信に失敗: %s", exc)
            return
        try:
            await channel.send(_log_fallback_text(title, body))
        except discord.HTTPException as exc:
            logger.warning("ログチャンネルへのテキスト送信にも失敗: %s", exc)

    async def _send_topic(
        self, channel: discord.TextChannel, result: TopicResult
    ) -> tuple[discord.Message, bool]:
        """お題を投稿する。二択型で選択肢が妥当なら Discord の投票にする。

        戻り値: (投稿したメッセージ, 投票として投稿したか)
        """
        role_id = self.settings.topic_ping_role_id
        # ロールメンションを実際に飛ばすには明示的な許可が要る。
        # 未設定なら None を渡し、既定（Client の allowed_mentions）のままにする
        mentions = (
            discord.AllowedMentions(roles=[discord.Object(id=role_id)]) if role_id else None
        )
        if self.settings.use_poll and result.format == "choice":
            if _valid_poll_options(result.poll_options):
                poll = discord.Poll(
                    question=result.topic_question[:POLL_QUESTION_MAX_LEN],
                    duration=POLL_DURATION,
                    multiple=False,
                )
                for option in result.poll_options or []:
                    poll.add_answer(text=option)
                header = _with_role_mention(
                    _compose_poll_header(
                        result, self.settings.topic_title, self.settings.topic_footer
                    ),
                    role_id,
                )
                return await channel.send(header, poll=poll, allowed_mentions=mentions), True
            logger.info(
                "二択型だが選択肢が投票に使えないため通常投稿にする: %s", result.poll_options
            )

        text = _with_role_mention(
            _compose_text_body(
                result, self.settings.topic_title, self.settings.topic_footer
            ),
            role_id,
        )
        return await channel.send(text, allowed_mentions=mentions), False

    async def _thread_replies(self, message: discord.Message) -> int:
        """お題に立ったスレッドの発言数。Bot はスレッドに投稿しないので全件を返信として数える。

        message_count は削除を減算しない概算だが、ランキング用のシグナルとしては十分。
        取得できなければ 0 として計測自体は成立させる。
        """
        thread = message.thread
        if thread is None:
            if not message.flags.has_thread:
                return 0
            # アーカイブ済みだと message.thread が None になる（スレッド ID = 元メッセージ ID）
            try:
                fetched = await self.bot.fetch_channel(message.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "スレッドを取得できないため返信0として計測: message_id=%s (%s)",
                    message.id, exc,
                )
                return 0
            if not isinstance(fetched, discord.Thread):
                return 0
            thread = fetched
        return thread.message_count or 0

    async def _measure_pending(self) -> None:
        """投稿から一定時間が経ったお題の反応（返信・リアクション・投票）を集計する。

        1つのお題を checkpoints の回数だけ計測し、そのつど at_hours 付きの行を追記する。
        """
        now = now_utc()
        checkpoints = _measure_checkpoints(self.settings)
        changed = False
        due: list[tuple[PendingMeasurement, int]] = []
        for pending in list(self.state.pending_measurements):
            if pending.measured_count >= len(checkpoints):
                # 運用中に MEASURE_FINAL_AFTER_HOURS を 0 へ戻した場合もここで完了扱いになる
                self.state.pending_measurements.remove(pending)
                changed = True
                continue
            at_hours = _due_checkpoint_hours(pending, now, checkpoints)
            if at_hours is not None:
                due.append((pending, at_hours))
        if not due:
            if changed:
                self.save()
            return
        channel = self.bot.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        for pending, at_hours in due:
            try:
                message = await channel.fetch_message(pending.message_id)
            except discord.NotFound:
                logger.info("計測対象のお題が削除済み: message_id=%s", pending.message_id)
                self.state.pending_measurements.remove(pending)
                changed = True
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                if _is_measure_given_up(
                    pending, now, checkpoints, self.settings.measure_giveup_hours
                ):
                    logger.warning(
                        "計測できないまま猶予(%d時間)を過ぎたため破棄: message_id=%s (%s)",
                        self.settings.measure_giveup_hours, pending.message_id, exc,
                    )
                    self.state.pending_measurements.remove(pending)
                    changed = True
                    await self.log_event(
                        "warning",
                        "お題の反応を計測できないため諦めました",
                        f"message_id={pending.message_id} / "
                        f"猶予{self.settings.measure_giveup_hours}時間を超過\n{pending.topic}",
                    )
                    continue
                logger.warning(
                    "お題の反応を取得できないため次周回に持ち越し: message_id=%s (%s)",
                    pending.message_id, exc,
                )
                continue

            replies = 0
            async for posted in channel.history(
                after=datetime.fromisoformat(pending.posted_at), limit=MEASURE_HISTORY_LIMIT
            ):
                reference = posted.reference
                if reference is not None and reference.message_id == pending.message_id:
                    replies += 1
            replies += await self._thread_replies(message)
            reactions = sum(r.count for r in message.reactions)
            votes = message.poll.total_votes if message.poll is not None else 0

            self.state.topic_stats.append(
                TopicStat(
                    topic=pending.topic,
                    format=pending.format,
                    replies=replies,
                    reactions=reactions,
                    votes=votes,
                    measured_at=now.isoformat(),
                    at_hours=at_hours,
                )
            )
            pending.measured_count += 1
            if pending.measured_count >= len(checkpoints):
                self.state.pending_measurements.remove(pending)
            changed = True
            logger.info(
                "お題の反応を計測 (%d時間後, message_id=%s, 返信=%d, リアクション=%d, 投票=%d): %s",
                at_hours, pending.message_id, replies, reactions, votes, pending.topic,
            )
        if changed:
            self.state.topic_stats = self.state.topic_stats[-TOPIC_STATS_KEPT:]
            self.save()

    def _good_examples(self) -> list[str]:
        """反応が良かったお題を few-shot 用に返す（スコア上位3件。反応ゼロは除く）。

        1点目の計測行だけを見る。1お題が最大2行あるため、混ぜると同じお題が重複する。
        """
        scored = [
            (_reaction_score(s), s.topic)
            for s in self.state.topic_stats
            if s.at_hours == self.settings.measure_after_hours
        ]
        ranked = sorted(
            (pair for pair in scored if pair[0] > 0), key=lambda pair: pair[0], reverse=True
        )
        return [topic for _, topic in ranked[:GOOD_EXAMPLES_KEPT]]

    async def _take_source(
        self, candidate: QueueItem | PoolItem | None
    ) -> tuple[QueueItem | PoolItem | None, bool]:
        """候補の元メッセージを取り直し、この周回で使ってよいか判定する。

        戻り値: (使える候補。使えないなら None, 今回の投稿を見送るか)
        """
        if candidate is None:
            return None, False
        outcome = await self._refresh_source(candidate)
        if outcome == "skip":
            logger.debug("投稿見送り: 元メッセージを再取得できなかった")
            return None, True
        return (candidate if outcome == "ok" else None), False

    async def _refresh_source(self, source: QueueItem | PoolItem) -> RefreshOutcome:
        """生成直前に元メッセージを1件だけ取り直し、削除・編集・オプトアウトに追随する。

        戻り値: "ok"（使ってよい）/ "discarded"（破棄したので次の候補へ）/ "skip"（今回は見送り）
        """
        channel = self.bot.get_channel(self.settings.intro_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"自己紹介チャンネルが見つかりません: {self.settings.intro_channel_id}")
        try:
            message = await channel.fetch_message(source.message_id)
        except discord.NotFound:
            logger.info("元メッセージが削除済みのため破棄: message_id=%s", source.message_id)
            self._discard_source(source)
            return "discarded"
        except (discord.Forbidden, discord.HTTPException) as exc:
            # 権限・ネットワークの一時障害で本文を失わないよう、破棄せず見送る
            logger.warning(
                "元メッセージを取得できないため今回は見送り: message_id=%s (%s)",
                source.message_id, exc,
            )
            return "skip"

        verdict = _judge_refresh(
            message, self.settings.optout_emoji, self.settings.min_intro_length
        )
        if verdict == "optout":
            logger.info(
                "オプトアウト(%s)により破棄: message_id=%s",
                self.settings.optout_emoji, source.message_id,
            )
            self._discard_source(source)
            return "discarded"
        if verdict == "too_short":
            logger.info("編集後の本文が短すぎるため破棄: message_id=%s", source.message_id)
            self._discard_source(source)
            return "discarded"
        if message.content != source.content:
            logger.info("元メッセージの編集を反映: message_id=%s", source.message_id)
            source.content = message.content
            self.save()
        return "ok"

    def _discard_source(self, source: QueueItem | PoolItem) -> None:
        """使えなくなった自己紹介をキュー / 再利用プールから取り除く。"""
        if isinstance(source, QueueItem):
            self.state.queue.remove(source)
            self.state.processed_ids.append(source.message_id)
        else:
            self.state.intro_pool.remove(source)
        self.save()

    def _prune_expired(self) -> None:
        """プライバシー配慮: 保持期限を過ぎた自己紹介の本文をプールとキューから削除する。

        キューに滞留したまま期限を迎えた分も対象にする（本文を保持しているのは同じため）。
        消したキュー項目は再取り込みされないよう processed_ids へ移す。
        """
        cutoff = now_utc() - timedelta(days=self.settings.pool_max_age_days)
        kept_pool = [p for p in self.state.intro_pool if p.created_dt() > cutoff]
        kept_queue = [q for q in self.state.queue if q.created_dt() > cutoff]
        expired_pool = len(self.state.intro_pool) - len(kept_pool)
        expired_queue = [q for q in self.state.queue if q.created_dt() <= cutoff]
        if not expired_pool and not expired_queue:
            return
        self.state.intro_pool = kept_pool
        self.state.queue = kept_queue
        self.state.processed_ids.extend(item.message_id for item in expired_queue)
        self.save()
        logger.info(
            "保持期限(%d日)を過ぎた自己紹介を削除: プール%d件 / キュー%d件",
            self.settings.pool_max_age_days, expired_pool, len(expired_queue),
        )

    async def _backfill_pool(self, limit: int, apply: bool) -> str:
        """#自己紹介 の過去ログを再利用プールへ取り込む。既定はプレビューのみ。

        キューではなくプールに入れる: キューは古い順×投稿間隔で消費するため、
        大量に積むと新規の自己紹介が何ヶ月も後回しになってしまう。
        last_seen_at は触らない（巻き戻すと未処理の区間を恒久的に見逃す）。
        """
        channel = self.bot.get_channel(self.settings.intro_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"自己紹介チャンネルが見つかりません: {self.settings.intro_channel_id}")
        now = now_utc()
        counts: Counter[BackfillVerdict] = Counter()
        added: list[PoolItem] = []
        scanned = 0
        async for message in channel.history(limit=limit):
            scanned += 1
            verdict = _backfill_verdict(message, self.state, self.settings, now)
            counts[verdict] += 1
            if verdict == "ok":
                added.append(
                    PoolItem(
                        message_id=message.id,
                        content=message.content,
                        created_at=message.created_at.isoformat(),
                        used_count=0,  # 未使用なので既存のプール項目より先に使われる
                    )
                )
        # DRY_RUN は state を一切変更しない保証があるので、apply 指定でもプレビューに落とす
        write = apply and not self.settings.dry_run
        if write and added:
            self.state.intro_pool.extend(added)
            self.save()
        summary = (
            f"走査{scanned} / 追加{len(added) if write else 0} / 重複{counts['dup']} / "
            f"期限超{counts['too_old']} / {self.settings.optout_emoji}{counts['optout']} / "
            f"短文{counts['short']}"
        )
        if not apply:
            summary += f"\nプレビューのみ（{len(added)}件を取り込むには apply:True を指定）"
        elif not write:
            summary += f"\nDRY_RUN=true のため反映していません（対象{len(added)}件）"
        logger.info("バックフィル: %s", summary.replace("\n", " / "))
        await self.log_event("info", "自己紹介のバックフィル", summary)
        return summary

    def _pick_pool_item(self) -> PoolItem | None:
        """再利用プールから1件選ぶ。使用回数が少ないもの、同数なら最後に使ったのが古いものを優先。"""
        if not self.state.intro_pool:
            return None
        # last_used_at が None（未使用）は空文字扱いになり最優先で選ばれる
        return min(self.state.intro_pool, key=lambda p: (p.used_count, p.last_used_at or ""))

    async def _pick_news_text(self) -> tuple[str, int] | None:
        """ニュースチャンネルの直近投稿から素材を1件選ぶ。使えなければ None。

        戻り値: (プロンプトへ渡す本文, 元メッセージ ID)。
        state には何も書かない: ニュースは使い捨てで再利用プール・processed_ids に入れない。
        本文を保持しないのでプライバシー配慮の自動削除の対象にならず、同じニュースを二度使う
        可能性は「投稿間隔48時間 × 毎日配信 × 直近お題との重複禁止」で実用上抑えられる。
        """
        if not self.settings.news_channel_id:
            return None
        channel = self.bot.get_channel(self.settings.news_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "ニュースチャンネルが見つかりません: %s", self.settings.news_channel_id
            )
            return None
        after = now_utc() - timedelta(days=NEWS_MAX_AGE_DAYS)
        try:
            # after を指定すると oldest_first の既定が True になるため、新しい順を明示する
            async for message in channel.history(
                limit=NEWS_FETCH_LIMIT, after=after, oldest_first=False
            ):
                # ニュースは webhook / bot が投稿するのが普通なので author.bot で除外しない
                text = _news_message_text(message)
                if text:
                    return _truncate(text, NEWS_TEXT_MAX), message.id
        except discord.HTTPException as exc:  # Forbidden もここに含まれる
            # ニュースは必須の素材ではないので、汎用お題へ静かに落とす
            logger.warning("ニュースを取得できないため今回は使いません: %s", exc)
            return None
        logger.info("直近%d日のニュースに使える本文がありませんでした", NEWS_MAX_AGE_DAYS)
        return None

    def _format_candidates(self) -> list[TopicFormat]:
        """次に使えるお題形式の候補を返す。"""
        recent = self.state.posted_formats[-RECENT_FORMATS_AVOIDED:]
        candidates = [f for f in TOPIC_FORMATS if f not in recent]
        return candidates or list(TOPIC_FORMATS)

    def _pick_format(self) -> TopicFormat:
        """直近に使った形式を避けつつ、反応が良かった形式ほど選ばれやすくする。"""
        candidates = self._format_candidates()
        weights = _format_weights(
            self.state.topic_stats, self.settings.measure_after_hours, candidates
        )
        return random.choices(candidates, weights=weights)[0]

    def _blocked_reason(self) -> str | None:
        """投稿条件を満たさない場合、その理由を返す。満たすなら None。"""
        s = self.settings
        if self.state.paused:
            # 止めるのは自動投稿だけ。計測・キュー取り込み・手動投稿は続ける
            return "一時停止中 (/topic resume で再開)"
        now = now_utc()
        hour = now.astimezone(JST).hour
        if not (s.post_window_start <= hour < s.post_window_end):
            return f"投稿時間帯外 ({hour}時 JST)"
        if self.state.last_posted_at is not None:
            since_post = now - datetime.fromisoformat(self.state.last_posted_at)
            if since_post < timedelta(hours=s.post_interval_hours):
                return f"投稿間隔が未経過 (前回から{int(since_post.total_seconds() // 3600)}時間)"
        return self._quiet_blocked_reason(now)

    def _quiet_blocked_reason(self, now: datetime) -> str | None:
        """Discord の会話ラグを考慮した静穏判定。

        直近ウィンドウ内に一定数以上の発言があれば「会話中」とみなし、
        返信ラグの可能性があるため長めの静穏時間を要求する。
        発言がまばらなら短めの静穏時間で投稿してよい。
        """
        s = self.settings
        window = timedelta(minutes=s.activity_window_minutes)
        recent = [
            t
            for t in (datetime.fromisoformat(x) for x in self.state.chat_activity)
            if now - t <= window
        ]
        if not recent:
            return None
        quiet_for = now - max(recent)
        busy = len(recent) >= s.busy_threshold
        required = s.quiet_busy_minutes if busy else s.quiet_minutes
        if quiet_for < timedelta(minutes=required):
            mode = "会話中" if busy else "まばら"
            return (
                f"雑談チャンネルが静穏待ち ({mode}: 直近{s.activity_window_minutes}分に{len(recent)}件, "
                f"最終発言から{int(quiet_for.total_seconds() // 60)}分 < 必要{required}分)"
            )
        return None

    def _pick_item(self) -> QueueItem | None:
        """処理する自己紹介を選ぶ。古いものから順に処理し、遅延時間未経過のものは除外する。

        生成に失敗してバックオフ中（next_retry_at が未来）のものも対象外にする。
        """
        now = now_utc()
        eligible = [
            item
            for item in self.state.queue
            if now - item.created_dt() >= timedelta(minutes=self.settings.min_delay_minutes)
            and (item.next_retry_at is None or datetime.fromisoformat(item.next_retry_at) <= now)
        ]
        if not eligible:
            return None
        return min(eligible, key=lambda item: item.created_dt())


class TopicCommands(app_commands.Group):
    """運用者向けの管理コマンド。

    表示層（一覧に出すか）と実行層（実際に実行できるか）は別物にしている:
    - 表示: Manage Server 権限を持たないメンバーには一覧に出さない（ステルス設計の維持）
    - 実行: OWNER_USER_ID 本人、または OWNER_ROLE_ID のロールを持つメンバー
    そのため OWNER_ROLE_ID のロールが Manage Server を持たない場合、実行は許可されても
    一覧には出ない（サーバー設定 > 連携サービス で表示側を上書きできる）。
    応答はすべて ephemeral で本人にしか見えない。
    """

    def __init__(self, cog: IntroTopicCog) -> None:
        super().__init__(
            name="topic",
            description="お題botの管理",
            default_permissions=discord.Permissions(manage_guild=True),
            guild_only=True,
        )
        self.cog = cog

    async def _denied(self, interaction: discord.Interaction) -> bool:
        """実行者に権限がなければ理由を ephemeral で返して True。無言では終わらせない。"""
        settings = self.cog.settings
        error = _authorization_error(
            interaction.user.id,
            _role_ids(interaction.user),
            settings.owner_user_id,
            settings.owner_role_id,
        )
        if error is None:
            return False
        logger.info("権限のない管理コマンド実行: user_id=%s", interaction.user.id)
        await interaction.response.send_message(error, ephemeral=True)
        return True

    @app_commands.command(name="now", description="投稿条件を無視してお題を1件投稿する")
    async def now(self, interaction: discord.Interaction) -> None:
        if await self._denied(interaction):
            return
        # 生成に3秒以上かかるので defer が必須
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self.cog._generate_and_post()
        except Exception as exc:
            logger.exception("/topic now の処理に失敗")
            summary = f"投稿に失敗しました: {exc!r}"
        if self.cog.state.paused:
            summary += "\n※一時停止中です（自動投稿は止まったままです）"
        await interaction.followup.send(
            _truncate(summary, INTERACTION_TEXT_MAX), ephemeral=True
        )

    @app_commands.command(name="stats", description="反応の計測結果と形式ごとの選ばれやすさを見る")
    async def stats(self, interaction: discord.Interaction) -> None:
        if await self._denied(interaction):
            return
        text = _format_stats_summary(
            self.cog.state, self.cog.settings, self.cog._blocked_reason()
        )
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="queue", description="キュー・プール・次の投稿予定を見る")
    async def queue(self, interaction: discord.Interaction) -> None:
        if await self._denied(interaction):
            return
        text = _format_queue_summary(self.cog.state, now_utc(), self.cog.settings)
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="status", description="現在の設定（.env の反映内容）を見る")
    async def status(self, interaction: discord.Interaction) -> None:
        if await self._denied(interaction):
            return
        text = _format_status_summary(self.cog.settings, self.cog.state)
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(
        name="backfill", description="過去の自己紹介を再利用プールへ取り込む"
    )
    @app_commands.describe(
        limit="遡る件数（未指定なら .env の BACKFILL_SCAN_LIMIT）",
        apply="true で実際に取り込む（既定はプレビューのみ）",
    )
    async def backfill(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, BACKFILL_SCAN_MIN, BACKFILL_SCAN_MAX] | None = None,
        apply: bool = False,
    ) -> None:
        if await self._denied(interaction):
            return
        # 走査に3秒以上かかるので defer が必須
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self.cog._backfill_pool(
                limit or self.cog.settings.backfill_scan_limit, apply
            )
        except Exception as exc:
            logger.exception("/topic backfill の処理に失敗")
            summary = f"バックフィルに失敗しました: {exc!r}"
        await interaction.followup.send(
            _truncate(summary, INTERACTION_TEXT_MAX), ephemeral=True
        )

    @app_commands.command(name="pause", description="自動投稿を一時停止する")
    async def pause(self, interaction: discord.Interaction) -> None:
        if await self._denied(interaction):
            return
        await self._set_paused(interaction, True)

    @app_commands.command(name="resume", description="自動投稿を再開する")
    async def resume(self, interaction: discord.Interaction) -> None:
        if await self._denied(interaction):
            return
        await self._set_paused(interaction, False)

    async def _set_paused(self, interaction: discord.Interaction, paused: bool) -> None:
        label = "一時停止" if paused else "再開"
        if self.cog.state.paused == paused:
            await interaction.response.send_message(f"すでに{label}しています。", ephemeral=True)
            return
        self.cog.state.paused = paused
        # 永続化しないと PC 再起動で pause が揮発し、勝手に投稿が再開してしまう
        self.cog.save()
        await interaction.response.send_message(f"自動投稿を{label}しました。", ephemeral=True)
        logger.info("自動投稿を%s: user_id=%s", label, interaction.user.id)
        await self.cog.log_event(
            "info", f"自動投稿を{label}しました", f"実行者: user_id={interaction.user.id}"
        )
