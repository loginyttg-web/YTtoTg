"""
Central configuration loader and validator.
Loads from .env, validates required fields, exposes helpers.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("config")

# Resolve runtime paths relative to the application directory, not the shell's
# current working directory. This keeps ``python ytbot/main.py``, Railway's
# ``cd ytbot && python main.py`` and test runners consistent.
APP_DIR = Path(__file__).resolve().parent


def _runtime_path(env_name: str, default: str) -> Path:
    raw = Path(os.getenv(env_name, default)).expanduser()
    return raw.resolve() if raw.is_absolute() else (APP_DIR / raw).resolve()


# ---------------------------------------------------------------------------
# Quality → yt-dlp format string map
# ---------------------------------------------------------------------------
# NOTE on fallbacks: the `<=?` filter is *optional* in yt-dlp — if no format
# matches at that height it is dropped, so "2160" gracefully falls back to
# 1080p → 720p → … automatically (and "1080" falls back to 720 → 480 …).
QUALITY_MAP = {
    "best": (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    ),
    "2160": (
        "bestvideo[height<=?2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=?2160]+bestaudio/best[height<=?2160]"
    ),
    "1440": (
        "bestvideo[height<=?1440][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=?1440]+bestaudio/best[height<=?1440]"
    ),
    "1080": (
        "bestvideo[height<=?1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=?1080]+bestaudio/best[height<=?1080]"
    ),
    "720": (
        "bestvideo[height<=?720][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=?720]+bestaudio/best[height<=?720]"
    ),
    "480": (
        "bestvideo[height<=?480][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=?480]"
    ),
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

QUALITY_LABELS = {
    "best": "⭐ Best",
    "2160": "🎞 4K",
    "1440": "🎥 2K",
    "1080": "🎬 1080p",
    "720": "📺 720p",
    "480": "📱 480p",
    "audio": "🎵 Audio",
}


class Config:
    """Singleton-ish config loaded from environment."""

    # --- Telegram ---
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))
    DEST_CHAT_ID: int = int(os.getenv("DEST_CHAT_ID", "0"))

    # --- Download ---
    PARALLEL_DOWNLOADS: int = int(os.getenv("PARALLEL_DOWNLOADS", "3"))
    DEFAULT_QUALITY: str = os.getenv("DEFAULT_QUALITY", "best")
    PROGRESS_INTERVAL: int = int(os.getenv("PROGRESS_INTERVAL", "3"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))

    # --- Upload pipeline ---
    # Upload workers (1 = strictly sequential, 2 = two files in parallel)
    UPLOAD_WORKERS: int = int(os.getenv("UPLOAD_WORKERS", "1"))
    # Max downloaded-but-unuploaded videos allowed (backpressure on downloads)
    UPLOAD_QUEUE_LIMIT: int = int(os.getenv("UPLOAD_QUEUE_LIMIT", "3"))

    # --- Paths ---
    # Relative values are always based at the ytbot/ application directory.
    # This avoids accidentally creating a second ./data directory when the bot
    # is launched from the repository root.
    BASE_DIR: Path = APP_DIR
    DOWNLOAD_DIR: Path = _runtime_path("DOWNLOAD_DIR", "./downloads")
    DATA_DIR: Path = _runtime_path("DATA_DIR", "./data")

    # --- YouTube Anti-Bot / Authentication ---
    # Path to cookies.txt (Netscape format) — export from browser
    COOKIES_PATH: str = os.getenv("COOKIES_PATH", "")
    # Path to browser executable for auto-cookie extraction (e.g. "chrome", "firefox")
    COOKIES_FROM_BROWSER: str = os.getenv("COOKIES_FROM_BROWSER", "")
    # OAuth2 is no longer supported by YouTube (removed 2024). Left for reference only.
    OAUTH_CACHE: str = os.getenv("OAUTH_CACHE", "")
    # PO-Token (Proof of Origin) — copy from browser devtools if needed
    PO_TOKEN: str = os.getenv("PO_TOKEN", "")
    # Sleep between requests to avoid rate-limiting (seconds)
    SLEEP_INTERVAL: float = float(os.getenv("SLEEP_INTERVAL", "3"))
    # Maximum sleep for random jitter (seconds)
    MAX_SLEEP_INTERVAL: float = float(os.getenv("MAX_SLEEP_INTERVAL", "8"))
    # Rate limit: max downloads per hour (0 = unlimited)
    RATE_LIMIT: int = int(os.getenv("RATE_LIMIT", "0"))
    # Custom User-Agent override (leave empty for yt-dlp default)
    USER_AGENT: str = os.getenv("USER_AGENT", "")

    # --- Storage ---
    MAX_DISK_GB: float = float(os.getenv("MAX_DISK_GB", "10"))
    DISK_SAFETY_MARGIN_GB: float = float(os.getenv("DISK_SAFETY_MARGIN_GB", "1.5"))
    DISK_ALERT_PERCENT: int = int(os.getenv("DISK_ALERT_PERCENT", "85"))

    # --- Upload/Split ---
    SPLIT_SIZE_MB: int = int(os.getenv("SPLIT_SIZE_MB", "1900"))
    TG_MAX_UPLOAD_MB: int = int(os.getenv("TG_MAX_UPLOAD_MB", "1990"))

    # --- Features ---
    DAILY_REPORT: bool = os.getenv("DAILY_REPORT", "true").lower() == "true"
    DAILY_REPORT_HOUR: int = int(os.getenv("DAILY_REPORT_HOUR", "22"))

    # --- Auto-watch (new-upload detection) ---
    # How often watched channels are checked for new videos (minutes)
    WATCH_INTERVAL_MIN: int = int(os.getenv("WATCH_INTERVAL_MIN", "30"))

    # --- Caption ---
    CAPTION_TEMPLATE: str = os.getenv(
        "CAPTION_TEMPLATE",
        "**{title}**\n\n📺 {channel}\n⏱ `{duration}`  ·  📅 `{upload_date}`\n🔗 {url}",
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @classmethod
    def split_size_bytes(cls) -> int:
        return cls.SPLIT_SIZE_MB * 1024 * 1024

    @classmethod
    def tg_max_bytes(cls) -> int:
        return cls.TG_MAX_UPLOAD_MB * 1024 * 1024

    @classmethod
    def safety_margin_bytes(cls) -> int:
        return int(cls.DISK_SAFETY_MARGIN_GB * 1024 * 1024 * 1024)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @classmethod
    def validate(cls) -> list[str]:
        """Return list of missing/wrong config keys (empty = all good)."""
        errors: list[str] = []

        if not cls.API_ID or cls.API_ID == 0:
            errors.append("API_ID is missing or zero")
        if not cls.API_HASH:
            errors.append("API_HASH is missing")
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN is missing")
        if not cls.OWNER_ID or cls.OWNER_ID == 0:
            errors.append("OWNER_ID is missing or zero")
        if not cls.DEST_CHAT_ID or cls.DEST_CHAT_ID == 0:
            errors.append("DEST_CHAT_ID is missing or zero")

        if cls.PARALLEL_DOWNLOADS < 1:
            errors.append("PARALLEL_DOWNLOADS must be >= 1")
        if cls.PARALLEL_DOWNLOADS > 5:
            errors.append("PARALLEL_DOWNLOADS must be <= 5")
        if cls.UPLOAD_WORKERS < 1 or cls.UPLOAD_WORKERS > 2:
            errors.append("UPLOAD_WORKERS must be 1 or 2 (Telegram flood safety)")
        if cls.UPLOAD_QUEUE_LIMIT < 1 or cls.UPLOAD_QUEUE_LIMIT > 20:
            errors.append("UPLOAD_QUEUE_LIMIT must be 1–20")
        if cls.DEFAULT_QUALITY not in QUALITY_MAP:
            errors.append(f"DEFAULT_QUALITY must be one of {list(QUALITY_MAP)}")
        if cls.PROGRESS_INTERVAL < 3:
            errors.append("PROGRESS_INTERVAL must be >= 3")
        if cls.PROGRESS_INTERVAL > 15:
            errors.append("PROGRESS_INTERVAL must be <= 15")
        if cls.MAX_RETRIES < 0:
            errors.append("MAX_RETRIES must be >= 0")
        if cls.WATCH_INTERVAL_MIN < 5 or cls.WATCH_INTERVAL_MIN > 1440:
            errors.append("WATCH_INTERVAL_MIN must be between 5 and 1440")

        # Ensure directories exist
        cls.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)

        for err in errors:
            logger.error("Config error: %s", err)

        if not errors:
            logger.info("Configuration validated successfully")

        return errors


def quality_format(quality: str) -> str:
    """Return yt-dlp format string for given quality key."""
    return QUALITY_MAP.get(quality, QUALITY_MAP["best"])


def quality_label(quality: str) -> str:
    """Return display label for quality key."""
    return QUALITY_LABELS.get(quality, quality)
