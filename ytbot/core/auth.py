"""
YouTube authentication & anti-bot-detection layer for yt-dlp.

Every yt-dlp call in the bot flows through `build_base_opts()` which
injects cookies / OAuth, browser-like headers, rate-limit jitter, and
retry logic to avoid the "Sign in to confirm you're not a robot" wall.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import threading
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

import shutil

from config import Config

logger = logging.getLogger("auth")

# Node.js path for yt-dlp JS challenge solving
_NODE_PATH: str = shutil.which("node") or ""

# ---------------------------------------------------------------------------
# OAuth2 Telegram relay
# ---------------------------------------------------------------------------
_oauth_tg_fn: Optional[Callable[[str], Coroutine]] = None
_oauth_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_oauth_telegram_relay(
    send_fn: Callable[[str], Coroutine],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Register an async callable that will be called (thread-safely) whenever
    yt-dlp emits an OAuth2 device-auth message.

    send_fn must be an async function: async def send_fn(text: str) -> None
    """
    global _oauth_tg_fn, _oauth_event_loop
    _oauth_tg_fn = send_fn
    _oauth_event_loop = loop
    logger.info("OAuth2 Telegram relay registered")


def _relay_to_telegram(msg: str) -> None:
    """Fire-and-forget: schedule the async send on the event loop thread."""
    if _oauth_tg_fn and _oauth_event_loop:
        asyncio.run_coroutine_threadsafe(_oauth_tg_fn(msg), _oauth_event_loop)


# ---------------------------------------------------------------------------
# Custom yt-dlp logger — intercepts OAuth2 device-auth URL
# ---------------------------------------------------------------------------

_OAUTH_KEYWORDS = (
    "please open",
    "enter code",
    "device",
    "google.com/device",
    "authorization",
    "oauth",
)


class OAuthTelegramLogger:
    """
    Drop-in yt-dlp logger that forwards OAuth2 device-auth messages to the
    bot owner on Telegram while keeping everything else in the normal log.
    """

    def _is_oauth_msg(self, msg: str) -> bool:
        low = msg.lower()
        return any(kw in low for kw in _OAUTH_KEYWORDS)

    def debug(self, msg: str) -> None:
        if self._is_oauth_msg(msg):
            logger.info("OAuth2 prompt captured: %s", msg.strip())
            _relay_to_telegram(
                f"🔐 **YouTube OAuth2 — Action Required**\n\n"
                f"`{msg.strip()}`\n\n"
                "Open the link above, log in with your Google account, "
                "enter the code shown, then return here.\n"
                "Downloads will resume automatically once authorised."
            )
        else:
            logger.debug("[yt-dlp] %s", msg.strip())

    def info(self, msg: str) -> None:
        if self._is_oauth_msg(msg):
            logger.info("OAuth2 info: %s", msg.strip())
            _relay_to_telegram(
                f"🔐 **YouTube OAuth2**\n\n`{msg.strip()}`"
            )
        else:
            logger.info("[yt-dlp] %s", msg.strip())

    def warning(self, msg: str) -> None:
        logger.warning("[yt-dlp] %s", msg.strip())

    def error(self, msg: str) -> None:
        logger.error("[yt-dlp] %s", msg.strip())


# ---------------------------------------------------------------------------
# Rate-limit throttle (global across all workers)
# ---------------------------------------------------------------------------
_throttle_lock = threading.Lock()
_last_request: float = 0.0
_download_count_this_hour: int = 0
_hour_start: float = time.time()


def _apply_throttle() -> None:
    """Sleep if needed to respect SLEEP_INTERVAL + random jitter."""
    global _last_request

    with _throttle_lock:
        now = time.time()
        min_gap = Config.SLEEP_INTERVAL
        jitter = random.uniform(0, max(0, Config.MAX_SLEEP_INTERVAL - Config.SLEEP_INTERVAL))
        target_wait = min_gap + jitter
        elapsed = now - _last_request

        if elapsed < target_wait:
            sleep_for = target_wait - elapsed
            logger.debug("Throttling: sleeping %.1fs", sleep_for)
            time.sleep(sleep_for)

        _last_request = time.time()


def _check_rate_limit() -> bool:
    """Return False if rate limit exceeded this hour."""
    global _download_count_this_hour, _hour_start

    if Config.RATE_LIMIT <= 0:
        return True

    with _throttle_lock:
        now = time.time()
        if now - _hour_start > 3600:
            _download_count_this_hour = 0
            _hour_start = now

        if _download_count_this_hour >= Config.RATE_LIMIT:
            remaining = 3600 - (now - _hour_start)
            logger.warning(
                "Hourly rate limit reached (%d/%d). Reset in %.0f min",
                _download_count_this_hour, Config.RATE_LIMIT, remaining / 60,
            )
            return False

        _download_count_this_hour += 1
        return True


# ---------------------------------------------------------------------------
# Cookie detection — which auth method is active?
# ---------------------------------------------------------------------------

def _cookie_source() -> Optional[str]:
    """
    Determine the active cookie source in priority order:
      1. COOKIES_PATH (Netscape cookies.txt file)
      2. COOKIES_FROM_BROWSER (extract directly from browser)
      3. OAUTH_CACHE (OAuth2 token cache file)
    Returns None if nothing is configured.
    """
    if Config.COOKIES_PATH and Path(Config.COOKIES_PATH).exists():
        return "cookiefile"
    if Config.COOKIES_FROM_BROWSER:
        return "browser"
    if Config.OAUTH_CACHE:
        return "oauth"
    return None


def auth_status() -> str:
    """Human-readable auth status for /authstatus display."""
    source = _cookie_source()
    po = getattr(Config, "PO_TOKEN", "")

    lines = []
    if source == "cookiefile":
        lines.append(f"✅ **Cookies file:** `{Config.COOKIES_PATH}`")
    elif source == "browser":
        lines.append(f"✅ **Browser cookies:** `{Config.COOKIES_FROM_BROWSER}`")
    else:
        lines.append("⚠️  **No cookies configured**\nRun `/cookies` to upload cookies.txt")

    if po:
        lines.append(f"✅ **PO-Token:** set ({len(po)} chars)")
    else:
        lines.append("ℹ️  **PO-Token:** not set (optional)")

    lines.append("\nℹ️  OAuth2 is no longer supported by YouTube.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core: build yt-dlp options merged with auth + anti-detection
# ---------------------------------------------------------------------------

def build_base_opts(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return a base dict of yt-dlp options with auth, throttling, and
    browser-emulation headers injected.  Merge *extra* on top.

    Call this every time you build a YoutubeDL instance.
    """
    # Use our custom logger so OAuth2 prompts reach Telegram
    # Build node.js runtime config for n-challenge solving
    _node_runtime: Dict[str, Any] = {"path": _NODE_PATH} if _NODE_PATH else {}

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "logger": OAuthTelegramLogger(),
        "retries": Config.MAX_RETRIES,
        "fragment_retries": Config.MAX_RETRIES,
        "extractor_retries": Config.MAX_RETRIES,
        "file_access_retries": Config.MAX_RETRIES,
        # ── Use web client (works with cookies) + node for n-challenge ──
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
            }
        },
        # ── JS runtime: use node.js to solve YouTube's n-challenge ──
        "js_runtimes": {"node": _node_runtime},
        # ── Allow downloading EJS challenge solver script from GitHub ──
        "remote_components": {"ejs:github"},
        # Avoid DASH manifest fetches that trigger extra auth checks
        "youtube_include_dash_manifest": False,
        # Do NOT let yt-dlp overwrite our cookies file with anonymous cookies
        "cookiesfrombrowser": None,
    }

    # ── PO-Token (Proof of Origin) — bypasses tighter bot-detection ──
    po_token = getattr(Config, "PO_TOKEN", "")
    if po_token:
        opts["extractor_args"]["youtube"]["po_token"] = [f"web.gvs+{po_token}"]
        opts["extractor_args"]["youtube"]["player_client"] = ["web"]
        logger.debug("PO-Token active")

    # ── User-Agent override ──
    if Config.USER_AGENT:
        opts["http_headers"] = {
            "User-Agent": Config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }

    # ── Auth priority: cookies.txt → browser → OAuth2 ──
    if Config.COOKIES_PATH and Path(Config.COOKIES_PATH).exists():
        opts["cookiefile"] = Config.COOKIES_PATH
        logger.debug("Using cookies from: %s", Config.COOKIES_PATH)

    elif Config.COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (Config.COOKIES_FROM_BROWSER,)
        logger.debug("Extracting cookies from browser: %s", Config.COOKIES_FROM_BROWSER)

    # OAuth2 via username='oauth2' is no longer supported by YouTube (removed 2024).
    # Only cookies.txt works for authenticated access.

    # ── Rate limiting ──
    if Config.SLEEP_INTERVAL > 0:
        opts["sleep_interval"] = Config.SLEEP_INTERVAL
        opts["max_sleep_interval"] = Config.MAX_SLEEP_INTERVAL

    if Config.RATE_LIMIT > 0:
        opts["ratelimit"] = Config.RATE_LIMIT * 1024 * 1024  # bytes/sec

    # ── Merge caller extras ──
    if extra:
        # Deep-merge extractor_args so caller can add without overwriting
        if "extractor_args" in extra:
            caller_ea = extra.pop("extractor_args", {})
            for extractor, args in caller_ea.items():
                opts["extractor_args"].setdefault(extractor, {}).update(args)
        opts.update(extra)

    return opts


def apply_request_throttle() -> bool:
    """
    Call before every yt-dlp extract_info / download call.
    Applies sleep-jitter and checks hourly rate limit.
    Returns True if the request can proceed.
    """
    if not _check_rate_limit():
        return False
    _apply_throttle()
    return True


# ---------------------------------------------------------------------------
# Handle the specific bot-detection error
# ---------------------------------------------------------------------------

BOT_DETECTION_MARKERS = [
    "Sign in to confirm you",
    "not a robot",
    "Sign in to prove",
    "confirm you're not a bot",
    "HTTP Error 429",
    "Too Many Requests",
    "This video is unavailable",
]


def is_bot_detection_error(error_msg: str) -> bool:
    """Return True if the error is specifically a bot-detection block."""
    lower = error_msg.lower()
    return any(marker.lower() in lower for marker in BOT_DETECTION_MARKERS)


def bot_detection_help() -> str:
    """Return a help message the bot can send the owner."""
    source = _cookie_source()
    if source:
        return (
            "🛡️ **Bot detection triggered despite auth.**\n\n"
            "Possible fixes:\n"
            "1. Your cookies may have expired — re-export `cookies.txt` and upload with `/cookies`\n"
            "2. Try reducing speed: `/setparallel 1` + increase `SLEEP_INTERVAL` env var\n"
            "3. Your account may be flagged — try a different Google account\n"
            "4. Wait 30-60 min before running `/resume`"
        )
    return (
        "🛡️ **YouTube bot detection triggered.**\n\n"
        "You have **no authentication configured**. Fix this:\n\n"
        "**Option A — Cookies file (Recommended):**\n"
        "1. Install 'Get cookies.txt LOCALLY' browser extension\n"
        "2. Log into YouTube → export cookies.txt\n"
        "3. Upload with `/cookies`\n\n"
        "**Option B — PO-Token (advanced):**\n"
        "Set the `PO_TOKEN` environment variable with your token from browser devtools"
    )
