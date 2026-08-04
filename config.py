"""環境変数 (.env) を読み込み、検証済みの Settings を提供する。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = PROJECT_DIR / "state.json"
DEFAULT_OPTOUT_EMOJI = "🚫"
DEFAULT_TOPIC_TITLE = "💭 今日のお題"
FALSE_VALUES = ("false", "0", "no")


@dataclass(frozen=True)
class Settings:
    discord_token: str
    gemini_api_key: str
    gemini_model: str
    intro_channel_id: int
    chat_channel_id: int
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
    measure_after_hours: int
    measure_final_after_hours: int  # 0 なら1点計測のみ
    measure_giveup_hours: int
    backfill_scan_limit: int  # /topic backfill で limit 未指定のときに遡る件数
    dry_run: bool
    log_channel_id: int
    owner_user_id: int
    state_path: Path


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f".env に {name} が設定されていません")
    return value


def _require_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is None:
            raise RuntimeError(f".env に {name} が設定されていません")
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
    load_dotenv(PROJECT_DIR / ".env")
    measure_after_hours = _require_int("MEASURE_AFTER_HOURS", 6)
    measure_final_after_hours = _require_int("MEASURE_FINAL_AFTER_HOURS", 24)
    # 2点目が1点目以前だと計測が進まないため、起動時に弾く
    if 0 < measure_final_after_hours <= measure_after_hours:
        raise RuntimeError(
            "MEASURE_FINAL_AFTER_HOURS は MEASURE_AFTER_HOURS より大きい値にしてください "
            f"(現在: {measure_final_after_hours} <= {measure_after_hours})"
        )
    return Settings(
        discord_token=_require("DISCORD_TOKEN"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip(),
        intro_channel_id=_require_int("INTRO_CHANNEL_ID"),
        chat_channel_id=_require_int("CHAT_CHANNEL_ID"),
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
        measure_after_hours=measure_after_hours,
        measure_final_after_hours=measure_final_after_hours,
        measure_giveup_hours=_require_int("MEASURE_GIVEUP_HOURS", 72),
        backfill_scan_limit=_require_int("BACKFILL_SCAN_LIMIT", 200),
        dry_run=_require_bool("DRY_RUN", False),
        log_channel_id=_require_int("LOG_CHANNEL_ID", 0),
        owner_user_id=_require_int("OWNER_USER_ID", 0),
        state_path=Path(os.environ.get("STATE_PATH", "").strip() or DEFAULT_STATE_PATH),
    )


def load_gemini_only_settings() -> tuple[str, str]:
    """try_gemini.py 用: Discord 設定なしで API キーとモデル名だけ読む。"""
    load_dotenv(PROJECT_DIR / ".env")
    return _require("GEMINI_API_KEY"), os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()
