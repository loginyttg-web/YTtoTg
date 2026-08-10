"""
YouTube authentication & anti-bot-detection layer for yt-dlp.

Every yt-dlp call in the bot flows through `build_base_opts()` which
injects cookies / OAuth, browser-like headers, rate-limit jitter, and
retry logic to avoid the "Sign in to confirm you're not a robot" wall.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

from config import Config

logger = logging.getLogger("auth")

# JS runtimes for yt-dlp n/sig challenge solving (node + deno)
# Computed lazily inside build_base_opts so PATH changes at startup are seen.
def _node_path() -> str:
    return shutil.which("node") or ""

def _deno_path() -> str:
    return shutil.which("deno") or ""

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


MAX_COOKIE_FILE_BYTES = 5 * 1024 * 1024
_AUTH_COOKIE_NAMES = {
    "APISID", "HSID", "LOGIN_INFO", "SAPISID", "SID", "SSID",
    "__SECURE-1PAPISID", "__SECURE-1PSID", "__SECURE-3PAPISID",
    "__SECURE-3PSID",
}


@dataclass(frozen=True)
class CookieFileInfo:
    """Non-sensitive metadata collected while validating a Netscape file."""

    path: Path
    cookie_count: int
    youtube_cookie_count: int
    auth_cookie_count: int
    expired_cookie_count: int
    domain_count: int
    size_bytes: int

    @property
    def has_login_cookies(self) -> bool:
        return self.auth_cookie_count > 0


def _absolute_cookie_path(raw_path: str) -> Path:
    """Resolve cookie settings consistently against the ytbot directory."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Config.BASE_DIR / path
    return path.resolve()


def configured_cookie_path() -> Path:
    """Return the exact path where `/cookies` installs an uploaded file.

    ``COOKIES_PATH`` can be an absolute filename, a relative filename (resolved
    from ``ytbot/``), or an existing directory. Without it, cookies always live
    at ``DATA_DIR/cookies.txt``. The path is returned even before it exists.
    """
    configured = (Config.COOKIES_PATH or "").strip()
    if configured:
        path = _absolute_cookie_path(configured)
        # Be forgiving when an operator supplies an existing storage folder or
        # explicitly ends the setting in a path separator.
        is_directory_hint = configured.endswith(("/", "\\"))
        return path / "cookies.txt" if path.is_dir() or is_directory_hint else path
    return (Config.DATA_DIR / "cookies.txt").resolve()


def inspect_cookies_file(path: Path) -> tuple[Optional[CookieFileInfo], str]:
    """Parse and validate a Netscape cookies file without exposing values.

    This catches empty exports, HTML/JSON renamed to .txt, malformed rows,
    unrelated browser exports and oversized uploads before they can replace a
    known-good file. yt-dlp remains responsible for server-side expiry/auth.
    """
    try:
        size = path.stat().st_size
        if size <= 0:
            return None, "The cookie file is empty. Export it again from YouTube."
        if size > MAX_COOKIE_FILE_BYTES:
            return None, (
                f"Cookie file is too large ({size:,} bytes). Maximum allowed is "
                f"{MAX_COOKIE_FILE_BYTES // (1024 * 1024)} MB."
            )
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return None, "The file is not UTF-8 text. Export it again as `cookies.txt`."
    except OSError as exc:
        return None, f"Could not read the uploaded file: `{exc}`"

    if "\x00" in text:
        return None, "The uploaded file contains binary data, not browser cookies."

    lines = text.splitlines()
    first_content_line = next((line.strip() for line in lines if line.strip()), "")
    valid_headers = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
    if not first_content_line.startswith(valid_headers):
        return (
            None,
            "This is not a Netscape-format `cookies.txt` file. "
            "Export it with **Get cookies.txt LOCALLY** while YouTube is open.",
        )

    now = int(time.time())
    cookie_count = 0
    youtube_count = 0
    auth_count = 0
    expired_count = 0
    domains: set[str] = set()

    for line_number, original_line in enumerate(lines, start=1):
        line = original_line.strip("\r")
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue

        row = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
        columns = row.split("\t", 6)
        if len(columns) != 7:
            return None, f"Malformed Netscape cookie row at line `{line_number}`."

        domain, include_subdomains, cookie_path, secure, expires, name, _value = columns
        if not domain or not cookie_path or include_subdomains.upper() not in {"TRUE", "FALSE"}:
            return None, f"Invalid cookie fields at line `{line_number}`."
        if secure.upper() not in {"TRUE", "FALSE"}:
            return None, f"Invalid secure flag at line `{line_number}`."
        try:
            expires_at = int(expires)
        except ValueError:
            return None, f"Invalid expiry timestamp at line `{line_number}`."

        clean_domain = domain.lstrip(".").casefold()
        is_youtube = clean_domain == "youtube.com" or clean_domain.endswith(".youtube.com")
        cookie_count += 1
        domains.add(clean_domain)
        if is_youtube:
            youtube_count += 1
            if name.upper() in _AUTH_COOKIE_NAMES:
                auth_count += 1
        if expires_at > 0 and expires_at <= now:
            expired_count += 1

    if cookie_count == 0:
        return None, "The file has no cookie rows. Export it again from YouTube."
    if youtube_count == 0:
        return None, (
            "No `youtube.com` cookies were found. Open YouTube, stay logged in, "
            "then export cookies for the current site."
        )

    return CookieFileInfo(
        path=path,
        cookie_count=cookie_count,
        youtube_cookie_count=youtube_count,
        auth_cookie_count=auth_count,
        expired_cookie_count=expired_count,
        domain_count=len(domains),
        size_bytes=size,
    ), ""


def validate_cookies_file(path: Path) -> tuple[bool, str]:
    """Compatibility wrapper used by upload and startup checks."""
    info, error = inspect_cookies_file(path)
    return info is not None, error


def install_cookies_file(source: Path, destination: Optional[Path] = None) -> CookieFileInfo:
    """Validate and atomically install *source* with owner-only permissions.

    Raises ``ValueError`` for invalid content and leaves any existing active
    cookie file untouched. The caller should place the temporary source in the
    destination directory so ``os.replace`` is atomic on all deployments.
    """
    info, error = inspect_cookies_file(source)
    if info is None:
        raise ValueError(error)

    target = (destination or configured_cookie_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.chmod(0o600)
    except OSError:
        # Windows and some mounted filesystems may not support POSIX modes.
        pass
    os.replace(source, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass

    return CookieFileInfo(
        path=target,
        cookie_count=info.cookie_count,
        youtube_cookie_count=info.youtube_cookie_count,
        auth_cookie_count=info.auth_cookie_count,
        expired_cookie_count=info.expired_cookie_count,
        domain_count=info.domain_count,
        size_bytes=info.size_bytes,
    )


def active_cookie_path() -> Optional[Path]:
    """Return the first existing *valid* cookie file, checked on every call.

    Manual file copies are detected without a restart. Invalid files are never
    passed to yt-dlp, and a missing/invalid custom path can safely fall back to
    the managed ``DATA_DIR/cookies.txt`` location.
    """
    configured = (Config.COOKIES_PATH or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(configured_cookie_path())

    default_path = (Config.DATA_DIR / "cookies.txt").resolve()
    if not candidates or candidates[0] != default_path:
        candidates.append(default_path)

    for path in candidates:
        if path.is_file() and inspect_cookies_file(path)[0] is not None:
            return path
    return None


def _cookie_source() -> Optional[str]:
    """
    Determine the active cookie source in priority order:
      1. COOKIES_PATH or DATA_DIR/cookies.txt (Netscape cookie file)
      2. COOKIES_FROM_BROWSER (extract directly from browser)
      3. OAUTH_CACHE (legacy reference only)
    Returns None if nothing is configured.
    """
    if active_cookie_path():
        return "cookiefile"
    if Config.COOKIES_FROM_BROWSER:
        return "browser"
    if Config.OAUTH_CACHE:
        return "oauth"
    return None


def auth_status() -> str:
    """Human-readable, content-aware status for the Telegram auth panel."""
    source = _cookie_source()
    cookie_path = active_cookie_path()
    target_path = configured_cookie_path()
    po = getattr(Config, "PO_TOKEN", "")

    lines: list[str] = []
    if source == "cookiefile" and cookie_path:
        info, _ = inspect_cookies_file(cookie_path)
        assert info is not None  # active_cookie_path only returns valid files
        login_state = "login cookies found" if info.has_login_cookies else "no login marker found"
        lines.extend([
            "✅ **YouTube cookies active**",
            f"├ `{info.youtube_cookie_count}` YouTube rows · `{login_state}`",
            f"├ `{info.size_bytes:,}` bytes · `{info.expired_cookie_count}` expired rows",
            f"└ `{cookie_path}`",
        ])
        if not info.has_login_cookies:
            lines.append("⚠️ Re-export while signed in; this file may only contain guest cookies.")
    elif source == "browser":
        lines.append(f"✅ **Browser cookies:** `{Config.COOKIES_FROM_BROWSER}`")
    else:
        invalid_error = ""
        if target_path.is_file():
            _info, invalid_error = inspect_cookies_file(target_path)
        if invalid_error:
            lines.extend([
                "❌ **Cookie file found but invalid**",
                invalid_error,
                f"📁 Replace: `{target_path}`",
            ])
        else:
            lines.extend([
                "⚠️ **No YouTube cookies configured**",
                f"📁 Upload target: `{target_path}`",
            ])

    lines.append(
        f"{'✅' if po else 'ℹ️'} **PO-Token:** "
        + (f"set (`{len(po)}` chars)" if po else "not set (optional)")
    )
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
    # Build JS runtime config for n-challenge solving (node + deno)
    _runtimes: Dict[str, Any] = {}
    _np = _node_path()
    if _np:
        _runtimes["node"] = {"path": _np}
    _dp = _deno_path()
    if _dp:
        _runtimes["deno"] = {"path": _dp}
    # If neither found, pass empty dict — lets yt-dlp auto-discover

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "logger": OAuthTelegramLogger(),
        "retries": Config.MAX_RETRIES,
        "fragment_retries": Config.MAX_RETRIES,
        "extractor_retries": Config.MAX_RETRIES,
        "file_access_retries": Config.MAX_RETRIES,
        # ── YouTube clients: web (needs valid cookies) + fallback clients ──
        # web = best quality with cookies; ios/android/mweb can return formats
        # even when web is challenged. yt-dlp tries them in order.
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "ios", "android", "mweb"],
                "player_skip": ["webpage"],
            }
        },
        # ── JS runtime: node/deno to solve YouTube's n-challenge ──
        "js_runtimes": _runtimes,
        # ── Allow downloading EJS challenge solver script from GitHub ──
        "remote_components": {"ejs:github"},
        "compat_opts": set(),
        # Avoid DASH manifest fetches that trigger extra auth checks
        "youtube_include_dash_manifest": False,
        # Do NOT let yt-dlp overwrite our cookies file with anonymous cookies
        "cookiesfrombrowser": None,
    }

    # ── PO-Token (Proof of Origin) — bypasses tighter bot-detection ──
    po_token = getattr(Config, "PO_TOKEN", "")
    if po_token:
        opts["extractor_args"]["youtube"]["po_token"] = [f"web.gvs+{po_token}"]
        logger.debug("PO-Token active")

    # ── User-Agent override ──
    if Config.USER_AGENT:
        opts["http_headers"] = {
            "User-Agent": Config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }

    # ── Auth priority: cookies.txt → browser → OAuth2 ──
    cookie_path = active_cookie_path()
    if cookie_path:
        opts["cookiefile"] = str(cookie_path)
        logger.debug("Using cookies from: %s", cookie_path)

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
    "Only images are available",
    "Requested format is not available",
    "n challenge solving failed",
    "Some formats may be missing",
    "challenge solving failed",
    "use --list-formats",
    "have a supported JavaScript runtime",
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
