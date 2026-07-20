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
            self.state.last_chat_activity = message.created_at.isoformat()
            self.save()
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
        if self.state.last_chat_activity is not None:
            return
        channel = self.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        async for message in channel.history(limit=1):
            self.state.last_chat_activity = message.created_at.isoformat()
            self.save()

    # --- 投稿ワーカー -------------------------------------------------------

    @tasks.loop(minutes=5)
    async def post_worker(self) -> None:
        reason = self._blocked_reason()
        if reason is not None:
            logger.debug("投稿見送り: %s", reason)
            return
        item = self.state.queue[0]
        try:
            result = await asyncio.to_thread(
                generate_topic,
                item.content,
                self.settings.gemini_api_key,
                self.settings.gemini_model,
            )
        except Exception:
            item.retry_count += 1
            logger.exception(
                "話題生成に失敗 (message_id=%s, %d/%d 回目)",
                item.message_id, item.retry_count, MAX_RETRIES,
            )
            if item.retry_count >= MAX_RETRIES:
                logger.error("リトライ上限に達したため破棄: message_id=%s", item.message_id)
                self.state.queue.pop(0)
                self.state.processed_ids.append(item.message_id)
            self.save()
            return

        channel = self.get_channel(self.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {self.settings.chat_channel_id}")
        await channel.send(f"{result.intro_line}\n{result.topic_question}")

        self.state.queue.pop(0)
        self.state.processed_ids.append(item.message_id)
        self.state.last_posted_at = now_utc().isoformat()
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
        if not (s.active_hour_start <= hour < s.active_hour_end):
            return f"稼働時間外 ({hour}時 JST)"
        item = self.state.queue[0]
        if now - item.created_dt() < timedelta(minutes=s.min_delay_minutes):
            return "自己紹介からの遅延時間が未経過"
        if self.state.last_chat_activity is not None:
            quiet_for = now - datetime.fromisoformat(self.state.last_chat_activity)
            if quiet_for < timedelta(minutes=s.quiet_minutes):
                return f"雑談チャンネルが活発 (最終発言から{int(quiet_for.total_seconds() // 60)}分)"
        if self.state.last_posted_at is not None:
            since_post = now - datetime.fromisoformat(self.state.last_posted_at)
            if since_post < timedelta(minutes=s.cooldown_minutes):
                return "クールダウン中"
        return None


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
