"""既存 bot への組み込み（load_extension / unload_extension）のスモークテスト。

Discord へは接続しない。統合の約束——「フォルダをコピーして1行足すだけで `/topic` が載る」
「ホスト bot の on_message を奪わない」「unload すれば完全に撤収する」——が
リファクタで崩れていないかだけを確認する。
"""

from __future__ import annotations

import asyncio

import discord
import pytest
from discord.ext import commands

from intro_topic.cog import COG_NAME

EXTENSION = "intro_topic"
SUBCOMMANDS = {"now", "stats", "queue", "status", "backfill", "pause", "resume"}

# ホスト bot が環境変数だけで設定を渡す状況を再現するための最小セット。
# DISCORD_TOKEN は意図的に含めない（ログインするのはホスト bot なので、組み込みに
# トークンは要らない ＝ シークレットを二重管理させない、という約束をここで守る）
HOST_ENV = {
    "INTRO_CHANNEL_ID": "111",
    "CHAT_CHANNEL_ID": "222",
    "OWNER_USER_ID": "42",
}
# 開発環境の .env が混ざらないよう空にしておく項目（空文字なら既定値が使われる）
CLEARED_ENV = (
    "DISCORD_TOKEN",
    "GEMINI_API_KEY",
    "LOG_CHANNEL_ID",
    "NEWS_CHANNEL_ID",
    "DRY_RUN",
    "MEASURE_AFTER_HOURS",
    "MEASURE_FINAL_AFTER_HOURS",
)


@pytest.fixture
def host_env(monkeypatch, tmp_path):
    for name, value in HOST_ENV.items():
        monkeypatch.setenv(name, value)
    for name in CLEARED_ENV:
        monkeypatch.setenv(name, "")
    # state.json を実リポジトリに作らせない
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))


def _host_bot() -> commands.Bot:
    """既存 bot に見立てたホスト。独自のプレフィックスを持たせておく。"""
    intents = discord.Intents.default()
    intents.message_content = True
    return commands.Bot(command_prefix="!", intents=intents)


async def _settle() -> None:
    """キャンセルしたタスクが実際に終わるまでイベントループに譲る。"""
    for _ in range(10):
        await asyncio.sleep(0)


def _run(coro_factory):
    return asyncio.run(coro_factory())


# --- 組み込み -------------------------------------------------------------


def test_load_extensionでtopicコマンドが載る(host_env):
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        try:
            groups = {command.name: command for command in bot.tree.get_commands()}
            assert list(groups) == ["topic"]
            assert {c.name for c in groups["topic"].commands} == SUBCOMMANDS
        finally:
            await bot.close()

    _run(main)


def test_管理コマンドはサーバー管理権限者にだけ見える(host_env):
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        try:
            group = next(iter(bot.tree.get_commands()))
            assert group.default_permissions == discord.Permissions(manage_guild=True)
            assert group.guild_only is True
        finally:
            await bot.close()

    _run(main)


def test_on_messageは追加式のリスナーとして登録される(host_env):
    # ホスト bot の on_message を上書きせず listener として足す、が統合安全性の核
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        try:
            assert "on_message" in bot.extra_events
            modules = {func.__module__ for func in bot.extra_events["on_message"]}
            assert modules == {"intro_topic.cog"}
        finally:
            await bot.close()

    _run(main)


def test_プレフィックスコマンドを一切追加しない(host_env):
    # 既存 bot のコマンド名・プレフィックスと衝突しないこと
    async def main():
        bot = _host_bot()
        before = set(bot.commands)
        await bot.load_extension(EXTENSION)
        try:
            assert set(bot.commands) == before
        finally:
            await bot.close()

    _run(main)


def test_読み込んだ時点ではsyncしない(host_env, monkeypatch):
    # ホスト bot の同期運用を尊重する（勝手に sync を叩かない）
    async def main():
        bot = _host_bot()
        synced = []

        async def spy(*args, **kwargs):
            synced.append((args, kwargs))
            return []

        monkeypatch.setattr(bot.tree, "sync", spy)
        await bot.load_extension(EXTENSION)
        try:
            assert synced == []
        finally:
            await bot.close()

    _run(main)


def test_5分ワーカーが動き出す(host_env):
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        try:
            assert bot.get_cog(COG_NAME).post_worker.is_running()
        finally:
            await bot.close()

    _run(main)


def test_ワーカーの1周目は起動処理の後まで待つ(host_env):
    # cog_load はログイン前に走るので、そのまま回すとチャンネルを引けない状態で1周目が動く
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        cog = bot.get_cog(COG_NAME)
        calls: list[int] = []

        async def spy() -> None:
            calls.append(1)

        # 差し替えはインスタンス側で行う（unload でモジュールごと捨てられるため、
        # テストモジュールが import したクラスへの patch では届かない）
        cog._post_worker_body = spy
        try:
            await _settle()
            assert calls == []
            cog._startup_ready.set()  # on_ready のキュー補完まで終わった状態
            await _settle()
            assert calls == [1]
        finally:
            await bot.close()

    _run(main)


# --- 撤収 -----------------------------------------------------------------


def test_unloadでワーカーが止まる(host_env):
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        cog = bot.get_cog(COG_NAME)
        await bot.unload_extension(EXTENSION)
        await _settle()
        assert not cog.post_worker.is_running()
        await bot.close()

    _run(main)


def test_unloadでtopicコマンドとリスナーが消える(host_env):
    async def main():
        bot = _host_bot()
        await bot.load_extension(EXTENSION)
        await bot.unload_extension(EXTENSION)
        await _settle()
        assert bot.tree.get_commands() == []
        assert bot.extra_events.get("on_message", []) == []
        assert bot.get_cog(COG_NAME) is None
        await bot.close()

    _run(main)
