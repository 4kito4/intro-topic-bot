"""単体起動用のランチャー。`uv run python bot.py` でこの bot だけを常駐させる。

既存の bot へ組み込む場合、このファイルは不要（`intro_topic/` をコピーして
`await bot.load_extension("intro_topic")` を1行足すだけ）。手順は INTEGRATION.md にある。
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from intro_topic import COG_NAME, IntroTopicCog, load_settings

logger = logging.getLogger("intro_topic_bot")

EXTENSION = "intro_topic"


class IntroTopicLauncher(commands.Bot):
    """intro_topic だけを載せたホスト bot。

    Cog 側はツリーへ `/topic` を積むだけで sync しない（ホスト bot の同期運用を尊重する）。
    単体起動では同期する相手が自分しかいないので、その一回分をここで受け持つ。
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        # 独自のプレフィックスコマンドは持たない（`/topic` に集約している）
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        # 再接続で on_ready が再度呼ばれても、登録は初回だけにする
        self._commands_registered = False

    async def setup_hook(self) -> None:
        await self.load_extension(EXTENSION)

    async def on_ready(self) -> None:
        if self._commands_registered:
            return
        self._commands_registered = True
        await self._register_commands()

    async def _register_commands(self) -> None:
        """管理コマンドを雑談チャンネルのギルドへ登録する（ギルド同期なので即時反映される）。

        applications.commands スコープなしで招待していると sync が失敗するが、
        自動投稿は動かし続けたいので例外はここで止め、対処方法をログに出す。
        """
        cog = self.get_cog(COG_NAME)
        if not isinstance(cog, IntroTopicCog):  # setup_hook で読み込み済みのはず
            raise RuntimeError(f"拡張 {EXTENSION} が読み込まれていません")
        channel = self.get_channel(cog.settings.chat_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"雑談チャンネルが見つかりません: {cog.settings.chat_channel_id}")
        guild = channel.guild
        # Cog はツリーへグローバルコマンドとして積むので、ギルドへ写してから同期する
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:  # Forbidden もここに含まれる
            logger.exception("スラッシュコマンドを登録できませんでした")
            await cog.log_event(
                "error",
                "スラッシュコマンドを登録できませんでした",
                f"{exc!r}\n"
                "applications.commands スコープ付きの招待 URL で Bot を再招待し、"
                "Bot を再起動してください。復旧するまで管理コマンドは使えませんが、"
                "自動投稿と反応の計測は通常どおり動きます。",
            )
            return
        logger.info("管理コマンドを登録しました: guild_id=%s", guild.id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    # トークンを使うのは自分でログインする単体起動だけなので、必須判定もここに置く
    # （既存 bot へ組み込む場合はホスト bot がログインするため設定不要）
    if not settings.discord_token:
        raise RuntimeError("環境変数（または .env）に DISCORD_TOKEN が設定されていません")
    IntroTopicLauncher().run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
