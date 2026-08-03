"""エントリポイント。自己紹介を検知し、雑談チャンネルが静かなときに話題を投稿する。"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord.ext import tasks

from config import Settings, load_settings
from store import (
    PendingMeasurement,
    PoolItem,
    QueueItem,
    State,
    TopicStat,
    load_state,
    save_state,
)
from topic_generator import (
    FORMAT_EMOJI,
    TOPIC_FORMATS,
    ReviewResult,
    TopicFormat,
    TopicResult,
    generate_topic,
)

logger = logging.getLogger("intro_topic_bot")

JST = timezone(timedelta(hours=9), name="JST")
MAX_RETRIES = 3
RETRY_BACKOFF_MINUTES = (5, 20, 60)  # 生成失敗が続いたときに次の試行まで空ける時間
MANUAL_TRIGGER_COMMAND = "!topic now"  # オーナー専用の手動トリガー
BACKFILL_LIMIT = 50
RECENT_TOPICS_KEPT = 10  # 多様性確保のためプロンプトに渡す直近お題の件数
RECENT_FORMATS_AVOIDED = 2  # 次のお題形式を選ぶときに避ける直近形式の件数
DEFAULT_TOPIC_EMOJI = "💭"  # 未知の形式が来たときの見出し絵文字

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


def _topic_header(format: TopicFormat) -> str:
    """お題の見出し。形式ごとに絵文字を変えて連投時の単調さを減らす。"""
    return f"{FORMAT_EMOJI.get(format, DEFAULT_TOPIC_EMOJI)} **お題**"


def _with_footer(body: str, footer: str) -> str:
    """任意のフッタ（参加ハードルを下げる一言）を末尾に足す。空なら何も足さない。"""
    footer = footer.strip()
    return f"{body}\n\n{footer}" if footer else body


def _compose_text_body(result: TopicResult, footer: str = "") -> str:
    """通常投稿の本文。問いは太字にして目に留まりやすくする。"""
    question = f"**{result.topic_question}**"
    body = f"{result.lead_in}\n{question}" if result.lead_in else question
    return _with_footer(f"{_topic_header(result.format)}\n\n{body}", footer)


def _compose_poll_header(result: TopicResult, footer: str = "") -> str:
    """投票として投稿するときの本文。問いは投票側に入るので本文には入れない。"""
    header = _topic_header(result.format)
    body = f"{header}\n\n{result.lead_in}" if result.lead_in else header
    return _with_footer(body, footer)


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


class IntroTopicBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.state: State = load_state(settings.state_path)
        # 投稿処理はワーカーと手動トリガーの両方から呼ばれるので直列化する
        self._post_lock = asyncio.Lock()

    def save(self) -> None:
        save_state(self.settings.state_path, self.state)

    # --- イベント -----------------------------------------------------------

    async def on_ready(self) -> None:
        logger.info("ログイン完了: %s", self.user)
        await self._backfill_intros()
        await self._init_chat_activity()
        if not self.post_worker.is_running():
            self.post_worker.start()
        await self._log_event(
            "info", "起動しました", _startup_summary(self.state, self.settings.dry_run)
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        # 手動トリガーは自己紹介・雑談どちらのチャンネルでも受け付ける。
        # コマンド自体をキューや発言履歴に混ぜないよう、他の処理より先に return する
        if self._is_manual_trigger(message):
            logger.info("手動トリガーを受信: user_id=%s", message.author.id)
            try:
                summary = await self._generate_and_post()
            except Exception:
                logger.exception("手動トリガーでの投稿に失敗")
            else:
                logger.info("手動トリガーの処理が完了: %s", summary)
            return
        if message.channel.id == self.settings.chat_channel_id:
            self._record_chat_activity(message.created_at)
            return
        if message.channel.id != self.settings.intro_channel_id:
            return
        self._enqueue_intro(message)

    def _is_manual_trigger(self, message: discord.Message) -> bool:
        """オーナーが投稿タイミングを無視して1件出すためのコマンドか判定する。"""
        owner_id = self.settings.owner_user_id
        return (
            owner_id != 0
            and message.author.id == owner_id
            and message.content.strip() == MANUAL_TRIGGER_COMMAND
        )

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
        channel = self.get_channel(self.settings.intro_channel_id)
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
        channel = self.get_channel(self.settings.chat_channel_id)
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
            await self._log_event("error", "投稿ワーカーで想定外のエラー", repr(exc))

    async def _post_worker_body(self) -> None:
        self._prune_expired_pool()
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
        # 優先順位: 新規キュー → 再利用プール → 自己紹介を使わない汎用お題。
        # 生成直前に元メッセージを取り直し、破棄された候補はその周回のうちに次へ送る
        chosen: QueueItem | PoolItem | None = None
        for candidate in (self._pick_item(), self._pick_pool_item()):
            if candidate is None:
                continue
            outcome = await self._refresh_source(candidate)
            if outcome == "skip":
                logger.debug("投稿見送り: 元メッセージを再取得できなかった")
                return "投稿を見送りました: 元の自己紹介を再取得できませんでした"
            if outcome == "ok":
                chosen = candidate
                break
        item = chosen if isinstance(chosen, QueueItem) else None
        pool_item = chosen if isinstance(chosen, PoolItem) else None
        if item is not None:
            source, source_id, intro_text = "新規", item.message_id, item.content
        elif pool_item is not None:
            source, source_id, intro_text = "プール", pool_item.message_id, pool_item.content
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
            )
        except Exception:
            if item is None:
                # プール由来・汎用由来は捨てるものがないのでリトライ管理は不要
                logger.exception("話題生成に失敗 (種別=%s, message_id=%s)", source, source_id)
                return f"お題の生成に失敗しました (種別={source})"
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
            await self._log_event(log_kind, log_title, log_body)
            return f"{log_title}\n{log_body}"

        if self.settings.dry_run:
            return "[DRY_RUN] 投稿予定\n" + await self._report_dry_run(source, result, review)

        channel = self.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        posted, is_poll = await self._send_topic(channel, result)

        now = now_utc()
        self.state.pending_measurements.append(
            PendingMeasurement(
                message_id=posted.id,
                topic=result.topic_question,
                format=result.format,
                posted_at=now.isoformat(),
                is_poll=is_poll,
            )
        )
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
        self.state.last_posted_at = now.isoformat()
        self.state.posted_topics = (self.state.posted_topics + [result.topic_question])[
            -RECENT_TOPICS_KEPT:
        ]
        # ローテーションは実際に投稿された形式を基準にする（申告形式を採用）
        self.state.posted_formats = (self.state.posted_formats + [result.format])[
            -RECENT_TOPICS_KEPT:
        ]
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
        await self._log_event("success", "お題を投稿しました", summary)
        return f"お題を投稿しました\n{summary}"

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
            _compose_poll_header(result, self.settings.topic_footer)
            if as_poll
            else _compose_text_body(result, self.settings.topic_footer),
        ]
        if as_poll:
            lines.append(f"投票: {result.topic_question}")
            lines.append("選択肢: " + " / ".join(result.poll_options or []))
        if review is not None and not review.approved:
            lines.append(f"審査コメント: {review.reason}")
        text = "\n".join(lines)
        logger.info("DRY_RUN のため投稿せず: %s", text)
        await self._log_event("warning", "[DRY_RUN] 投稿予定", text)
        return text

    async def _log_event(self, kind: LogKind, title: str, body: str = "") -> None:
        """運用ログを種別ごとの色付き embed でチャンネルに流す。

        except 節からも呼ぶため、送信の失敗はすべてここで握って本体・ワーカーを止めない。
        Embed Links 権限がない場合はプレーンテキストで送り直す。
        """
        if not self.settings.log_channel_id:
            return
        channel = self.get_channel(self.settings.log_channel_id)
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
        if self.settings.use_poll and result.format == "choice":
            if _valid_poll_options(result.poll_options):
                poll = discord.Poll(
                    question=result.topic_question[:POLL_QUESTION_MAX_LEN],
                    duration=POLL_DURATION,
                    multiple=False,
                )
                for option in result.poll_options or []:
                    poll.add_answer(text=option)
                header = _compose_poll_header(result, self.settings.topic_footer)
                return await channel.send(header, poll=poll), True
            logger.info(
                "二択型だが選択肢が投票に使えないため通常投稿にする: %s", result.poll_options
            )

        text = _compose_text_body(result, self.settings.topic_footer)
        return await channel.send(text), False

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
                fetched = await self.fetch_channel(message.id)
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
        channel = self.get_channel(self.settings.chat_channel_id)
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
                    await self._log_event(
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

    async def _refresh_source(self, source: QueueItem | PoolItem) -> RefreshOutcome:
        """生成直前に元メッセージを1件だけ取り直し、削除・編集・オプトアウトに追随する。

        戻り値: "ok"（使ってよい）/ "discarded"（破棄したので次の候補へ）/ "skip"（今回は見送り）
        """
        channel = self.get_channel(self.settings.intro_channel_id)
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

    def _prune_expired_pool(self) -> None:
        """プライバシー配慮: 保持期限を過ぎた自己紹介本文をプールから削除する。"""
        cutoff = now_utc() - timedelta(days=self.settings.pool_max_age_days)
        kept = [p for p in self.state.intro_pool if p.created_dt() > cutoff]
        removed = len(self.state.intro_pool) - len(kept)
        if removed:
            self.state.intro_pool = kept
            self.save()
            logger.info(
                "保持期限(%d日)を過ぎた自己紹介をプールから削除: %d件",
                self.settings.pool_max_age_days, removed,
            )

    def _pick_pool_item(self) -> PoolItem | None:
        """再利用プールから1件選ぶ。使用回数が少ないもの、同数なら最後に使ったのが古いものを優先。"""
        if not self.state.intro_pool:
            return None
        # last_used_at が None（未使用）は空文字扱いになり最優先で選ばれる
        return min(self.state.intro_pool, key=lambda p: (p.used_count, p.last_used_at or ""))

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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    bot = IntroTopicBot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
