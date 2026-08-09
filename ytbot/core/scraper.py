"""
YouTube channel / playlist scanner using yt-dlp with auth support.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yt_dlp

from config import Config
from core.auth import build_base_opts, apply_request_throttle
from utils.helpers import sanitize_filename, classify_url, normalize_channel_url, parse_video_id

logger = logging.getLogger("scraper")


def _parse_items(info: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Parse yt-dlp info into (channel_name, kind, items)."""
    entries = info.get("entries") or []
    channel = info.get("channel", "") or info.get("uploader", "") or "Unknown"

    if info.get("_type") == "playlist":
        kind = "playlist"
    elif info.get("_type") == "url":
        kind = "video"
    elif entries and info.get("extractor_key") in ("YoutubeTab", "YoutubeChannel"):
        kind = "channel"
    elif entries:
        kind = "playlist"
    else:
        kind = "video"

    items: List[Dict[str, Any]] = []
    for entry in entries:
        vid = entry.get("id") or entry.get("url", "").split("v=")[-1].split("&")[0]
        if not vid or len(vid) != 11:
            continue
        title = entry.get("title", "Untitled")
        if title in ("[Private video]", "[Deleted video]"):
            continue
        duration = entry.get("duration") or entry.get("duration_string") or "?"
        if isinstance(duration, (int, float)):
            duration = _fmt_duration(int(duration))
        # Try to get upload time from raw Unix timestamp
        raw_ts = entry.get("timestamp") or entry.get("release_timestamp")
        upload_time = ""
        if raw_ts:
            try:
                from datetime import datetime as _dtx
                upload_time = _dtx.fromtimestamp(float(raw_ts)).strftime("%H:%M")
            except (TypeError, ValueError, OSError, OverflowError):
                pass

        items.append({
            "id": vid,
            "url": f"https://youtube.com/watch?v={vid}",
            "title": title,
            "duration": str(duration),
            "channel": channel,
            # Flat channel entries do not always expose upload_date. Keep all
            # known date fields so the UI can sort reliably when available.
            "upload_date": (
                entry.get("upload_date")
                or entry.get("release_date")
                or _timestamp_to_date(entry.get("timestamp"))
                or _timestamp_to_date(entry.get("release_timestamp"))
                or ""
            ),
            "upload_time": upload_time,
            "playlist_index": entry.get("playlist_index") or 0,
        })

    return channel, kind, items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fmt_subscribers(n) -> str:
    """Format subscriber count like 1.2M, 340K, 5K etc."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n//1_000}K"
    return str(n)


async def scan(url: str) -> Dict[str, Any]:
    """
    Scan a YouTube URL and return structured result.

    Returns:
        {
          "type": "channel"|"playlist"|"video",
          "channel": str,
          "meta": { subscribers, channel_url, verified, description, ... },
          "items": [{id, url, title, duration, channel, upload_date}, ...]
        }
    """
    logger.info("Scanning: %s", url)

    kind = classify_url(url)

    if kind == "video":
        vid = parse_video_id(url)
        if not vid:
            return {"type": "video", "channel": "", "meta": {}, "items": []}
        try:
            apply_request_throttle()
            opts = build_base_opts({"skip_download": True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            dur = info.get("duration") or 0
            meta = {
                "subscribers": _fmt_subscribers(info.get("channel_follower_count")),
                "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
                "verified":    info.get("channel_is_verified", False),
                "upload_date": info.get("upload_date", ""),
                "view_count":  info.get("view_count") or 0,
                "like_count":  info.get("like_count") or 0,
                "source_url":  url,
            }
            # Try to extract upload time from raw timestamp
            _raw_ts = info.get("timestamp") or info.get("release_timestamp")
            _upload_time = ""
            if _raw_ts:
                try:
                    from datetime import datetime as _dtx
                    _upload_time = _dtx.fromtimestamp(float(_raw_ts)).strftime("%H:%M")
                except (TypeError, ValueError, OSError, OverflowError):
                    pass

            return {
                "type":    "video",
                "channel": info.get("uploader") or info.get("channel") or "",
                "meta":    meta,
                "items": [{
                    "id":          vid,
                    "url":         f"https://youtube.com/watch?v={vid}",
                    "title":       info.get("title", "Untitled"),
                    "duration":    _fmt_duration(int(dur)) if dur else "?",
                    "channel":     info.get("uploader", ""),
                    "upload_date": info.get("upload_date", ""),
                    "upload_time": _upload_time,
                    "width":       info.get("width", 0) or 0,
                    "height":      info.get("height", 0) or 0,
                }],
            }
        except Exception as exc:
            logger.error("Failed to extract single video %s: %s", url, exc)
            return {"type": "video", "channel": "", "meta": {}, "items": []}

    # Channel / playlist
    try:
        if kind == "channel":
            url = normalize_channel_url(url)

        apply_request_throttle()
        opts = build_base_opts({
            "extract_flat": True,
            "skip_download": True,
            "ignoreerrors": True,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        channel, kind_out, items = _parse_items(info)

        meta = {
            "subscribers": _fmt_subscribers(
                info.get("channel_follower_count")
                or info.get("uploader_follower_count")
            ),
            "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
            "verified":    info.get("channel_is_verified", False),
            "description": (info.get("description") or "")[:200],
            "playlist_title": info.get("title") or "",
            "source_url":  url,
        }
        return {"type": kind_out, "channel": channel, "meta": meta, "items": items}

    except Exception as exc:
        logger.error("Scan failed for %s: %s", url, exc)
        raise


def sort_items(items: List[Dict[str, Any]], order: str = "new_old") -> List[Dict[str, Any]]:
    """Sort by upload_date + upload_time for same-day precision.
    'new_old' = newest first, 'old_new' = oldest first."""

    def _dt_key(item: Dict) -> str:
        """Return YYYYMMDDHHMM string so full datetime sorts correctly."""
        d  = item.get("upload_date", "") or ""
        tm = (item.get("upload_time", "") or "").replace(":", "").ljust(4, "0")
        return d + tm if d else ""

    # YouTube's flat channel extractor frequently omits dates. In that case
    # the source order is normally newest-first, so reverse it for Old → New.
    dated = [item for item in items if _dt_key(item)]
    if not dated:
        return list(items) if order == "new_old" else list(reversed(items))

    if order == "old_new":
        return sorted(
            items,
            key=lambda item: (
                _dt_key(item) or "999999999999",
                item.get("playlist_index") or 99999999,
            ),
        )
    return sorted(
        items,
        key=lambda item: (
            _dt_key(item) or "000000000000",
            -(item.get("playlist_index") or 0),
        ),
        reverse=True,
    )


def generate_txt(
    items: List[Dict[str, Any]],
    channel: str,
    kind: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Path:
    """Generate a rich .txt listing file. Returns path to saved file."""
    out_dir = Config.DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_channel = sanitize_filename(channel) or "backup"
    filename = f"{safe_channel}_{kind}.txt"
    filepath = out_dir / filename

    meta = meta or {}
    subs        = meta.get("subscribers", "—")
    ch_url      = meta.get("channel_url", "")
    verified    = "✔ Verified" if meta.get("verified") else ""
    source_url  = meta.get("source_url", "")
    description = (meta.get("description") or "").strip()

    # Total duration sum
    total_secs = 0
    for item in items:
        d = item.get("duration", "")
        parts = str(d).split(":")
        try:
            if len(parts) == 3:
                total_secs += int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                total_secs += int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            pass
    def _fmt_total(secs: int) -> str:
        h, r = divmod(secs, 3600); m, s = divmod(r, 60)
        return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  YT BACKUP LIST  —  {kind.upper()}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Channel     : {channel} {verified}",
        f"  Subscribers : {subs}",
    ]
    if ch_url:
        lines.append(f"  Channel URL : {ch_url}")
    if source_url:
        lines.append(f"  Source URL  : {source_url}")
    lines += [
        f"  Total Videos: {len(items)}",
        f"  Total Time  : {_fmt_total(total_secs)}",
        f"  Generated   : {_now()}",
    ]
    if description:
        lines += ["", f"  About: {description}"]
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for i, item in enumerate(items, 1):
        title = item.get("title", "Untitled")
        dur   = item.get("duration", "?")
        date  = item.get("upload_date", "")
        time_ = item.get("upload_time", "")
        if date and len(date) == 8:
            try:
                from datetime import datetime as _dt
                date = _dt.strptime(date, "%Y%m%d").strftime("%d %b %Y")
            except ValueError:
                pass
        url   = item.get("url", "")
        if date and time_:
            date_part = f" | 📅 {date}  🕐 {time_}"
        elif date:
            date_part = f" | 📅 {date}"
        else:
            date_part = ""
        lines.append(f"{i:>4}. {title} | ⏱ {dur}{date_part}")
        lines.append(f"       🔗 {url}")
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Generated txt: %s (%d entries)", filepath.name, len(items))
    return filepath


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_to_date(value: Any) -> str:
    """Convert yt-dlp timestamp values to YYYYMMDD for sorting."""
    if not value:
        return ""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(value)).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
