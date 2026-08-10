"""自己紹介→お題投下機能の拡張パッケージ。

既存の discord.py 製 bot へは、このフォルダを丸ごとコピーして次の1行を足すだけで載る::

    await bot.load_extension("intro_topic")

このパッケージはパッケージ外を一切 import しない（自己完結）。組み込み手順は
リポジトリの INTEGRATION.md、単体で動かす手順は README.md にある。
"""

from __future__ import annotations

from discord.ext import commands

from .cog import COG_NAME, IntroTopicCog
from .config import Settings, load_settings

__all__ = ["COG_NAME", "IntroTopicCog", "Settings", "load_settings", "setup"]


async def setup(bot: commands.Bot) -> None:
    """load_extension のエントリポイント。設定は環境変数（と `.env`）から読む。"""
    await bot.add_cog(IntroTopicCog(bot, load_settings()))
