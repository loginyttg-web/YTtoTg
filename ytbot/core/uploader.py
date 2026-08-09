"""
Sequential upload worker: send files to Telegram, handle FloodWait, auto-delete.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import Config
from core.splitter import needs_split, split_to_zip_parts, cleanup_parts
from core.state import Task, UPLOADING, StateManager
from core.system import safe_delete
from utils.helpers import human_bytes, eta_from_speed, md_escape, short

logger = logging.getLogger("uploader")

# ---------------------------------------------------------------------------
# Global upload progress registry (for dashboard)
# ---------------------------------------------------------------------------
upload_progress: Dict[str, Dict[str, Any]] = {}
_uplock = threading.Lock()


def _set_upload_progress(task_id: str, data: Dict[str, Any]) -> None:
    with _uplock:
        upload_progress[task_id] = data


def _del_upload_progress(task_id: str) -> None:
    with _uplock:
        upload_progress.pop(task_id, None)


# ---------------------------------------------------------------------------
# Global FloodWait gate — when Telegram says "slow down", ALL upload
# workers (and retries) respect the same cooldown window.
# ---------------------------------------------------------------------------
_flood_lock = threading.Lock()
_flood_until: float = 0.0


def _set_global_flood(seconds: float) -> None:
    global _flood_until
    with _flood_lock:
        _flood_until = max(_flood_until, time.time() + seconds)
    logger.warning("🌊 FloodWait — global upload cooldown %ds", seconds)


def _global_flood_remaining() -> float:
    with _flood_lock:
        return max(0.0, _flood_until - time.time())


# ---------------------------------------------------------------------------
# Caption system — settings, signature footer, builder
# ---------------------------------------------------------------------------

def get_caption_settings(state) -> Dict[str, Any]:
    """Caption configuration from state.settings (with safe defaults)."""
    s = state.settings
    return {
        "enabled":   s.get("caption_enabled", True),
        "signature": s.get("caption_signature", True),
        "name":      s.get("caption_name", ""),
        "username":  s.get("caption_username", ""),
        "show_id":   s.get("caption_show_id", False),
    }


def signature_preview(cfg: Dict[str, Any], video_num: int = 1) -> str:
    """Render the signature footer (used by uploads and as a live preview)."""
    bits = []
    if video_num > 0:
        bits.append(f"#{video_num}")
    if cfg.get("signature"):
        who = []
        if cfg.get("name"):
            who.append(f"**{md_escape(cfg['name'])}**")
        uname = (cfg.get("username") or "").lstrip("@")
        if uname:
            who.append(f"@{uname}")
        if cfg.get("show_id"):
            who.append(f"🆔 `{Config.OWNER_ID}`")
        if who:
            bits.append("Uploaded by " + " ".join(who))
    if not bits:
        return ""
    return f"━━━━━━━━━━━━━━━━━━━━\n⚡ " + " · ".join(bits)


def _build_caption(task: Task, video_num: int = 0,
                   cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    Build the Telegram upload caption.
    Returns '' when captions are disabled (→ send file without caption).
    """
    from config import quality_label as _qlabel
    from utils.helpers import fmt_yt_date

    cfg = cfg or {"enabled": True}
    if not cfg.get("enabled", True):
        return ""

    title       = md_escape(task.title or "Untitled")
    channel     = md_escape(task.channel or "—")
    duration    = task.duration or "—"
    upload_time = getattr(task, "upload_time", "") or ""
    yt_date_str = fmt_yt_date(getattr(task, "upload_date", ""), upload_time)
    url         = task.url
    q_label     = _qlabel(getattr(task, "quality", "best"))

    d        = datetime.now()
    tg_date  = f"{d.day} {d.strftime('%b %Y')}"

    body = (
        f"**{title}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 {channel}\n"
        f"{q_label}  ·  ⏱ `{duration}`\n"
        f"📅 `{yt_date_str}`  ·  📤 `{tg_date}`\n"
        f"🔗 {url}"
    )

    foot = signature_preview(cfg, video_num)
    return body + ("\n" + foot if foot else "")


# ---------------------------------------------------------------------------
# Thumbnail helper
# ---------------------------------------------------------------------------

def _parse_duration_secs(duration_str: str) -> int:
    """Convert 'HH:MM:SS' or 'MM:SS' duration string to integer seconds."""
    if not duration_str or duration_str == "—":
        return 0
    parts = duration_str.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        else:
            return int(parts[0])
    except (ValueError, IndexError):
        return 0


def _get_thumb(task: Task) -> Optional[str]:
    """Return absolute thumb path if the file exists."""
    thumb = getattr(task, "thumb_path", "") or ""
    if not thumb:
        logger.debug("No thumb_path on task %s", task.id)
        return None
    # Resolve to absolute so it works regardless of cwd
    from pathlib import Path as _Path
    p = _Path(thumb).resolve()
    if p.exists() and p.stat().st_size > 0:
        logger.debug("Thumb found: %s (%d B)", p, p.stat().st_size)
        return str(p)
    logger.warning("Thumb file missing at upload time: %s", thumb)
    return None


def _cleanup_thumb(task: Task) -> None:
    """Delete local thumbnail file after upload."""
    thumb = getattr(task, "thumb_path", "") or ""
    if thumb:
        safe_delete(Path(thumb))


# ---------------------------------------------------------------------------
# Single file upload
# ---------------------------------------------------------------------------

class _SpeedTracker:
    """Exponentially-smoothed speed tracker → stable ETA instead of jitter."""

    _ALPHA = 0.25  # smoothing factor (lower = smoother)

    def __init__(self) -> None:
        self._last_bytes: int = 0
        self._last_t: float = time.monotonic()
        self.speed: float = 0.0

    def update(self, current: int) -> float:
        now = time.monotonic()
        dt = now - self._last_t
        if dt <= 0.2:          # ignore sub-200ms ticks (noisy)
            return self.speed
        inst = max(current - self._last_bytes, 0) / max(dt, 0.001)
        if self.speed <= 0:
            self.speed = inst
        else:
            self.speed = self._ALPHA * inst + (1 - self._ALPHA) * self.speed
        self._last_bytes = current
        self._last_t = now
        return self.speed


def resolve_dest(task: Task) -> int:
    """Per-task destination: watch-specific chat, else the global one."""
    return task.dest_chat_id or Config.DEST_CHAT_ID


async def _upload_single(app: Client, task: Task, caption: str, dest: int) -> bool:
    """Upload one file as video with progress callback. Returns success."""

    tracker = _SpeedTracker()

    def progress_cb(current: int, total: int) -> None:
        speed = tracker.update(current)
        _set_upload_progress(task.id, {
            "current": current,
            "total":   total,
            "speed":   speed,
            "eta":     eta_from_speed(current, total, speed) if speed > 0 else 0,
            "part":    "1/1",
        })

    thumb    = _get_thumb(task)
    duration = _parse_duration_secs(task.duration or "")
    width    = getattr(task, "width", 0) or 0
    height   = getattr(task, "height", 0) or 0

    video_kwargs = dict(
        chat_id=dest,
        video=task.filepath,
        caption=caption,
        thumb=thumb,
        duration=duration if duration > 0 else None,
        width=width if width > 0 else None,
        height=height if height > 0 else None,
        supports_streaming=True,
    )

    sent_ids: list = []

    async def _do_send(**extra):
        msg = await app.send_video(progress=progress_cb, **video_kwargs, **extra)
        if msg and msg.id:
            sent_ids.append(msg.id)

    try:
        await asyncio.wait_for(_do_send(), timeout=3600)  # 1h timeout
        _del_upload_progress(task.id)
        return sent_ids or True
    except asyncio.TimeoutError:
        logger.error("Upload timed out for %s", task.title)
        raise Exception("Upload timed out after 1 hour")
    except FloodWait as fw:
        logger.warning("FloodWait: sleeping %ds", fw.value)
        _set_global_flood(fw.value + 1)
        await asyncio.sleep(fw.value + 1)
        try:
            await asyncio.wait_for(_do_send(), timeout=3600)
            _del_upload_progress(task.id)
            return sent_ids or True
        except FloodWait as fw2:
            _set_global_flood(fw2.value + 1)
            raise
        except Exception as exc:
            raise exc
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Split upload
# ---------------------------------------------------------------------------

async def _upload_split(app: Client, task: Task, caption: str, dest: int):
    """Split file into parts and upload each as a document.
    Returns list of sent message IDs on success, raises on failure."""
    parts       = split_to_zip_parts(task.filepath)
    total_parts = len(parts)
    task.parts  = parts
    sent_ids: list = []

    try:
        for i, part_path in enumerate(parts, 1):
            part_label = f"{i}/{total_parts}"
            tracker = _SpeedTracker()

            def progress_cb(current: int, total: int, _lbl=part_label, _tr=tracker) -> None:
                speed = _tr.update(current)
                _set_upload_progress(task.id, {
                    "current": current,
                    "total":   total,
                    "speed":   speed,
                    "eta":     eta_from_speed(current, total, speed) if speed > 0 else 0,
                    "part":    _lbl,
                })

            part_caption = (
                f"{caption}\n\n📦 Part {part_label}"
                if caption else f"📦 Part {part_label}"
            )
            filename     = Path(part_path).name

            logger.info("Uploading %s (%d/%d)", filename, i, total_parts)

            async def _do_send_doc(_pp=part_path, _cap=part_caption, _fn=filename, _cb=progress_cb):
                return await app.send_document(
                    chat_id=dest,
                    document=_pp,
                    caption=_cap,
                    file_name=_fn,
                    progress=_cb,
                )

            try:
                msg = await asyncio.wait_for(_do_send_doc(), timeout=3600)
                if msg and msg.id:
                    sent_ids.append(msg.id)
            except FloodWait as fw:
                logger.warning("FloodWait: sleeping %ds", fw.value)
                _set_global_flood(fw.value + 1)
                await asyncio.sleep(fw.value + 1)
                msg = await app.send_document(
                    chat_id=dest,
                    document=part_path,
                    caption=part_caption,
                    file_name=filename,
                )
                if msg and msg.id:
                    sent_ids.append(msg.id)

        _del_upload_progress(task.id)
        return sent_ids

    except Exception as exc:
        logger.error("Split upload failed: %s", exc)
        raise
    finally:
        cleanup_parts(parts)


# ---------------------------------------------------------------------------
# Main upload task
# ---------------------------------------------------------------------------

async def upload_task(app: Client, task: Task, state: StateManager) -> bool:
    """
    Upload a single task to Telegram:
      1. Send thumbnail as a photo (so channel shows it before the video)
      2. Send the video (with thumb attached for the player preview)
    Returns True on success.
    """
    # ── Resolve filepath to absolute ────────────────────────────────────────
    from pathlib import Path as _Path
    fp = _Path(task.filepath)
    if not fp.is_absolute() or not fp.exists():
        # Try resolving relative to DOWNLOAD_DIR (handles migrated state.json)
        candidate = Config.DOWNLOAD_DIR / fp.name
        if candidate.exists():
            task.filepath = str(candidate)
        else:
            # Last resort: scan DOWNLOAD_DIR by video ID
            from core.downloader import _find_output_file
            found = _find_output_file(Config.DOWNLOAD_DIR, task.id)
            if found:
                task.filepath = str(found)
            else:
                logger.warning("File not found for %s — re-queueing for download", task.id)
                # Reset to PENDING so it gets re-downloaded
                state.update_status(task.id, "pending", filepath="")
                return False
    # ── Resolve thumb_path to absolute if needed ─────────────────────────
    if task.thumb_path:
        tp = _Path(task.thumb_path)
        if not tp.is_absolute() or not tp.exists():
            candidate_t = Config.DOWNLOAD_DIR / tp.name
            task.thumb_path = str(candidate_t) if candidate_t.exists() else ""

    video_num = state.reserve_channel_number(task)
    cap_cfg   = get_caption_settings(state)
    caption   = _build_caption(task, video_num, cap_cfg) or None
    dest      = resolve_dest(task)

    logger.info("📤 Uploading #%d → %d: %s (%s)",
                video_num, dest, task.title, human_bytes(task.filesize))

    sent_msg_ids: list = []

    # --- 1. Send thumbnail as standalone photo first ---
    thumb = _get_thumb(task)
    if thumb:
        try:
            photo_cap = (
                f"🖼️ **{md_escape(task.title or 'Untitled')}**"
                if cap_cfg.get("enabled", True) else None
            )
            photo_msg = await app.send_photo(
                chat_id=dest,
                photo=thumb,
                caption=photo_cap,
            )
            if photo_msg and photo_msg.id:
                sent_msg_ids.append(photo_msg.id)
            logger.info("Thumbnail photo sent for %s", task.id)
        except FloodWait as fw:
            logger.warning("FloodWait on thumb photo: sleeping %ds", fw.value)
            _set_global_flood(fw.value + 1)
            await asyncio.sleep(fw.value + 1)
            try:
                photo_msg = await app.send_photo(
                    chat_id=dest, photo=thumb, caption=photo_cap,
                )
                if photo_msg and photo_msg.id:
                    sent_msg_ids.append(photo_msg.id)
            except Exception as exc:
                logger.warning("Thumb photo retry failed: %s", exc)
        except Exception as exc:
            logger.warning("Thumbnail photo send failed (continuing): %s", exc)

    # --- 2. Send video (with thumb for in-player preview) ---
    try:
        if needs_split(task.filepath):
            result = await _upload_split(app, task, caption, dest)
        else:
            result = await _upload_single(app, task, caption, dest)
    except Exception as exc:
        logger.error("Upload failed for %s: %s", task.title, exc)
        state.mark_failed(task, f"Upload: {exc}")
        _del_upload_progress(task.id)
        return False

    # result is a list of message IDs on success (or True for legacy compat)
    if result:
        if isinstance(result, list):
            sent_msg_ids.extend(result)
        # Track all sent message IDs so /purge can find them
        if sent_msg_ids:
            state.track_dest_msgs(sent_msg_ids)
        if os.path.exists(task.filepath):
            safe_delete(Path(task.filepath))
        _cleanup_thumb(task)
        state.mark_completed(task)
        return True

    return False


# ---------------------------------------------------------------------------
# Upload worker loop
# ---------------------------------------------------------------------------

async def upload_worker(
    app: Client,
    stop_event: asyncio.Event,
    state: StateManager,
) -> None:
    """
    Single sequential upload worker: pick next downloaded task, upload it.
    """
    logger.info("Upload worker started")

    while not stop_event.is_set():
        if state.settings.get("paused", False):
            await asyncio.sleep(2)
            continue

        # FloodWait safety — if any upload hit a flood cooldown, everyone waits
        rem = _global_flood_remaining()
        if rem > 0:
            await asyncio.sleep(min(rem, 30))
            continue

        # Atomic claim — status flips to UPLOADING under the state lock.
        task = state.claim_next_upload()
        if task is None:
            await asyncio.sleep(2)
            continue

        await upload_task(app, task, state)

    logger.info("Upload worker stopped")
