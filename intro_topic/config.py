"""環境変数（と `.env`）を読み込み、検証済みの Settings を提供する。

優先順位は「すでに設定されている環境変数 > `.env` の記述」。ホスト bot が環境変数を
自前で管理していても、単体起動でリポジトリ直下の `.env` を使っても、Docker のように
環境変数だけを渡しても、同じ関数がそのまま使える形にしている。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# 状態ファイルの既定の置き場所。カレントディレクトリ基準にするのは、単体起動
# （リポジトリ直下で `uv run python bot.py`）でも既存 bot へ組み込んだ場合でも
# 「動かしている場所の直下」に落ちるのが最も予想を裏切らないため。
# Docker・PaaS では STATE_PATH で永続ボリュームのパスへ向ける
DEFAULT_STATE_PATH = Path("state.json")
DEFAULT_OPTOUT_EMOJI = "🚫"
DEFAULT_TOPIC_TITLE = "💭 今日のお題"
FALSE_VALUES = ("false", "0", "no")


@dataclass(frozen=True)
class Settings:
    # 単体起動（bot.py）でログインするときだけ必要。既存 bot へ組み込む場合はログインを
    # ホスト bot が行うので不要（使わないシークレットを二重管理させない）。必須判定は bot.py 側
    discord_token: str
    gemini_api_key: str  # 空なら定型お題モード（Gemini を呼ばずに内蔵のお題を投稿する）
    gemini_model: str
    intro_channel_id: int
    chat_channel_id: int
    news_channel_id: int  # 0 ならニュースを素材に使わない
    min_delay_minutes: int
    quiet_minutes: int
    quiet_busy_minutes: int
    activity_window_minutes: int
    busy_threshold: int
    post_interval_hours: int
    post_window_start: int
    post_window_end: int
    min_intro_length: int
    pool_max_age_days: int
    optout_emoji: str
    use_poll: bool
    topic_title: str  # お題投稿の1行目。毎回同じ固定の見出し
    topic_footer: str  # 空なら表示しない
    topic_ping_role_id: int  # 0 ならお題投稿で通知しない
    measure_after_hours: int
    measure_final_after_hours: int  # 0 なら1点計測のみ
    measure_giveup_hours: int
    backfill_scan_limit: int  # /topic backfill で limit 未指定のときに遡る件数
    dry_run: bool
    log_channel_id: int
    owner_user_id: int
    owner_role_id: int  # 0 ならロールによる許可なし（OWNER_USER_ID との OR 判定）
    state_path: Path


def _load_env_file() -> None:
    """`.env` を環境変数へ流し込む。既に設定済みの環境変数は上書きしない。

    探索はカレントディレクトリ基準（見つからなければ親へ遡り、無ければ何もしない）。
    単体起動ならリポジトリ直下の `.env`、既存 bot へ組み込んだ場合はそのホスト bot の
    `.env` を拾い、Docker・PaaS のように環境変数だけを渡す環境ではファイル無しで動く。
    `override=False` なのでホスト側が先に設定した環境変数のほうが常に優先される。
    """
    load_dotenv(find_dotenv(usecwd=True), override=False)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"環境変数（または .env）に {name} が設定されていません")
    return value


def _require_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is None:
            raise RuntimeError(f"環境変数（または .env）に {name} が設定されていません")
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} は整数で指定してください: {raw!r}") from None


def _require_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in FALSE_VALUES


def load_settings() -> Settings:
    _load_env_file()
    measure_after_hours = _require_int("MEASURE_AFTER_HOURS", 6)
    measure_final_after_hours = _require_int("MEASURE_FINAL_AFTER_HOURS", 24)
    # 2点目が1点目以前だと計測が進まないため、起動時に弾く
    if 0 < measure_final_after_hours <= measure_after_hours:
        raise RuntimeError(
            "MEASURE_FINAL_AFTER_HOURS は MEASURE_AFTER_HOURS より大きい値にしてください "
            f"(現在: {measure_final_after_hours} <= {measure_after_hours})"
        )
    return Settings(
        # 未設定でも組み込みモードでは困らないので、ここでは必須にしない
        discord_token=os.environ.get("DISCORD_TOKEN", "").strip(),
        # 未設定でも起動する。Gemini が使えないだけで定型お題の投稿は続けられるため
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip(),
        intro_channel_id=_require_int("INTRO_CHANNEL_ID"),
        chat_channel_id=_require_int("CHAT_CHANNEL_ID"),
        news_channel_id=_require_int("NEWS_CHANNEL_ID", 0),
        min_delay_minutes=_require_int("MIN_DELAY_MINUTES", 30),
        quiet_minutes=_require_int("QUIET_MINUTES", 15),
        quiet_busy_minutes=_require_int("QUIET_BUSY_MINUTES", 45),
        activity_window_minutes=_require_int("ACTIVITY_WINDOW_MINUTES", 60),
        busy_threshold=_require_int("BUSY_THRESHOLD", 3),
        post_interval_hours=_require_int("POST_INTERVAL_HOURS", 48),
        post_window_start=_require_int("POST_WINDOW_START", 19),
        post_window_end=_require_int("POST_WINDOW_END", 22),
        min_intro_length=_require_int("MIN_INTRO_LENGTH", 50),
        pool_max_age_days=_require_int("POOL_MAX_AGE_DAYS", 90),
        optout_emoji=os.environ.get("OPTOUT_EMOJI", "").strip() or DEFAULT_OPTOUT_EMOJI,
        use_poll=_require_bool("USE_POLL", True),
        topic_title=os.environ.get("TOPIC_TITLE", "").strip() or DEFAULT_TOPIC_TITLE,
        topic_footer=os.environ.get("TOPIC_FOOTER", "").strip(),
        topic_ping_role_id=_require_int("TOPIC_PING_ROLE_ID", 0),
        measure_after_hours=measure_after_hours,
        measure_final_after_hours=measure_final_after_hours,
        measure_giveup_hours=_require_int("MEASURE_GIVEUP_HOURS", 72),
        backfill_scan_limit=_require_int("BACKFILL_SCAN_LIMIT", 200),
        dry_run=_require_bool("DRY_RUN", False),
        log_channel_id=_require_int("LOG_CHANNEL_ID", 0),
        owner_user_id=_require_int("OWNER_USER_ID", 0),
        owner_role_id=_require_int("OWNER_ROLE_ID", 0),
        state_path=Path(os.environ.get("STATE_PATH", "").strip() or DEFAULT_STATE_PATH),
    )


def load_gemini_only_settings() -> tuple[str, str]:
    """try_gemini.py 用: Discord 設定なしで API キーとモデル名だけ読む。"""
    _load_env_file()
    return _require("GEMINI_API_KEY"), os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()
