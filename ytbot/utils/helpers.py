"""
Utility helpers: formatting, progress bars, speed indicators, URL parsing.
"""

from __future__ import annotations

import re
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Byte / time formatting
# ---------------------------------------------------------------------------

def human_bytes(n: float, compact: bool = False) -> str:
    """Convert bytes to human-readable string, e.g. '1.23 GB'."""
    if n < 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}" if compact else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_time(seconds: float) -> str:
    """Convert seconds to '2h 15m 30s'."""
    if seconds <= 0:
        return "0s"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def human_time_short(seconds: float) -> str:
    """Compact: '2h15m' or '15m30s' or '45s'."""
    if seconds <= 0:
        return "0s"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Progress bars — multiple styles
# ---------------------------------------------------------------------------

BLOCKS = "█▉▊▋▌▍▎▏ "
DOTS = "●○"
SEP = "━━━━━━━━━━━━━━━━━━━━"


def progress_bar(current: float, total: float, width: int = 16) -> str:
    """Unicode progress bar with ⅛-block granularity: `████▌░░░░░░░░░`."""
    if total <= 0:
        return "░" * width
    ratio = min(max(current / total, 0), 1)
    filled = int(ratio * width)
    partial = int((ratio * width - filled) * 8)
    bar = BLOCKS[0] * filled
    if filled < width:
        bar += BLOCKS[8 - partial]  # 8-0=8→space, 8-7=1→▉, correct gradient
        bar += BLOCKS[-1] * (width - filled - 1)
    return bar


def styled_progress_bar(current: float, total: float, width: int = 14) -> str:
    """Styled bar with dashes for empty: [████████──────]"""
    if total <= 0:
        return "[" + "─" * width + "]"
    ratio = min(max(current / total, 0), 1)
    filled = int(ratio * width)
    bar = "█" * filled + "─" * (width - filled)
    return f"[{bar}]"


def progress_bar_compact(current: float, total: float, width: int = 10) -> str:
    """Compact bar using dot fill — better in tight spaces: `●●●●●○○○○○`."""
    if total <= 0:
        return DOTS[1] * width
    ratio = min(max(current / total, 0), 1)
    filled = int(ratio * width)
    return DOTS[0] * filled + DOTS[1] * (width - filled)


def progress_bar_dual(
    current: float, total: float, width: int = 22
) -> str:
    """
    Dual-colour bar with percentage baked in:
    `████████████░░░░  64%`
    """
    bar = progress_bar(current, total, width)
    pct_str = f"{percent(current, total):.0f}%".rjust(4)
    return f"{bar} {pct_str}"


def percent(current: float, total: float) -> float:
    """Percentage 0-100."""
    if total <= 0:
        return 0.0
    return round(min(current / total, 1.0) * 100, 1)


def eta_from_speed(done: float, total: float, speed: float) -> float:
    """Estimated seconds remaining based on current speed."""
    if speed <= 0:
        return 0.0
    remaining = total - done
    return remaining / speed


# ---------------------------------------------------------------------------
# Speed tier indicator
# ---------------------------------------------------------------------------

def speed_tier(bytes_per_sec: float) -> str:
    if bytes_per_sec > 10_000_000:
        return "🚀"
    if bytes_per_sec > 2_000_000:
        return "🐇"
    if bytes_per_sec > 500_000:
        return "🐢"
    return "🐌"


def speed_str(bytes_per_sec: float) -> str:
    """Human-friendly speed: `4.2 MB/s` or `850 KB/s`."""
    if bytes_per_sec >= 1_000_000:
        return f"{bytes_per_sec / 1_000_000:.1f} MB/s"
    if bytes_per_sec >= 1_000:
        return f"{bytes_per_sec / 1_000:.0f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


# ---------------------------------------------------------------------------
# Queue-level stats
# ---------------------------------------------------------------------------

def queue_progress_summary(
    completed: int, failed: int, pending: int, active: int, total: int,
) -> str:
    done = completed + failed
    bar = progress_bar_compact(done, total, 10) if total > 0 else DOTS[1] * 10
    pct_str = f"{percent(done, total):.0f}%" if total > 0 else "0%"
    return (
        f"`{bar}`  `{done}/{total} ({pct_str})`  "
        f"✅{completed}  ❌{failed}  ⏳{pending}  ⬇{active}"
    )


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Strip invalid filename chars, keep unicode letters/numbers, truncate."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Keep unicode letters, numbers, spaces and safe punctuation
    name = re.sub(r"[^\w\s\-.()\[\]{}]", "_", name, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip()
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    if len(name) > max_len:
        name = name[: max_len - 3].rstrip() + "..."
    return name or "untitled"


def short(text: str, limit: int = 38) -> str:
    """Truncate with ellipsis if longer than limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def pad_right(text: str, width: int) -> str:
    """Pad to exact width (approximate, since emoji are wide)."""
    visible = len(text)
    if visible >= width:
        return text
    return text + " " * (width - visible)


# ---------------------------------------------------------------------------
# URL / ID helpers
# ---------------------------------------------------------------------------

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|"
    r"youtube\.com/v/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def parse_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID (11 chars) from URL."""
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def classify_url(url: str) -> str:
    """Classify a YouTube URL as 'video', 'playlist', or 'channel'."""
    if "playlist" in url and "list=" in url:
        return "playlist"
    if parse_video_id(url):
        return "video"
    if re.search(r"youtube\.com/(@|channel/|c/)", url):
        return "channel"
    if "youtu.be" in url:
        return "video"
    return "unknown"


def normalize_channel_url(url: str) -> str:
    """Append /videos to channel URL for full scan."""
    url = url.rstrip("/")
    if "/videos" not in url:
        return url + "/videos"
    return url
