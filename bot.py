"""エントリポイント。自己紹介を検知し、雑談チャンネルが静かなときに話題を投稿する。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

from config import STATE_PATH, Settings, load_settings
from store import QueueItem, State, load_state, save_state
from topic_generator import generate_topic

logger = logging.getLogger("intro_topic_bot")

JST = timezone(timedelta(hours=9), name="JST")
MAX_RETRIES = 3
BACKFILL_LIMIT = 50
RECENT_TOPICS_KEPT = 10  # 多様性確保のためプロンプトに渡す直近お題の件数


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class IntroTopicBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.state: State = load_state(STATE_PATH)

    def save(self) -> None:
        save_state(STATE_PATH, self.state)

    # --- イベント -----------------------------------------------------------

    async def on_ready(self) -> None:
        logger.info("ログイン完了: %s", self.user)
        await self._backfill_intros()
        await self._init_chat_activity()
        if not self.post_worker.is_running():
            self.post_worker.start()

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
        except Exception:
            logger.exception("投稿ワーカーで想定外のエラー。次周回に継続します")

    async def _post_worker_body(self) -> None:
        reason = self._blocked_reason()
        if reason is not None:
            logger.debug("投稿見送り: %s", reason)
            return
        item = self._pick_item()
        if item is None:
            logger.debug("投稿見送り: 遅延時間を満たす自己紹介がない")
            return
        try:
            result = await asyncio.to_thread(
                generate_topic,
                item.content,
                self.settings.gemini_api_key,
                self.settings.gemini_model,
                self.state.posted_topics[-RECENT_TOPICS_KEPT:],
            )
        except Exception:
            item.retry_count += 1
            logger.exception(
                "話題生成に失敗 (message_id=%s, %d/%d 回目)",
                item.message_id, item.retry_count, MAX_RETRIES,
            )
            if item.retry_count >= MAX_RETRIES:
                logger.error("リトライ上限に達したため破棄: message_id=%s", item.message_id)
                self.state.queue.remove(item)
                self.state.processed_ids.append(item.message_id)
            self.save()
            return

        channel = self.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        body = f"{result.lead_in}\n{result.topic_question}" if result.lead_in else result.topic_question
        await channel.send(f"💭 **お題**\n{body}")

        self.state.queue.remove(item)
        self.state.processed_ids.append(item.message_id)
        self.state.last_posted_at = now_utc().isoformat()
        self.state.posted_topics = (self.state.posted_topics + [result.topic_question])[
            -RECENT_TOPICS_KEPT:
        ]
        self.save()
        logger.info(
            "話題を投稿 (message_id=%s, 検索使用=%s): %s",
            item.message_id, result.used_search, result.topic_question,
        )

    def _blocked_reason(self) -> str | None:
        """投稿条件を満たさない場合、その理由を返す。満たすなら None。"""
        s = self.settings
        now = now_utc()
        if not self.state.queue:
            return "キューが空"
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
        """処理する自己紹介を選ぶ。新しいものを優先し、遅延時間未経過のものは除外する。"""
        now = now_utc()
        eligible = [
            item
            for item in self.state.queue
            if now - item.created_dt() >= timedelta(minutes=self.settings.min_delay_minutes)
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda item: item.created_dt())


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
