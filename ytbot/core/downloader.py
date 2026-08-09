"""
Parallel download workers — yt-dlp with auth, throttling, progress tracking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yt_dlp

from config import Config, quality_format
from core.auth import (
    build_base_opts,
    apply_request_throttle,
    is_bot_detection_error,
    bot_detection_help,
)
from core.state import Task, DOWNLOADING, DOWNLOADED, PENDING, FAILED, StateManager
from core.system import has_space_for
from utils.helpers import sanitize_filename, human_bytes, eta_from_speed

logger = logging.getLogger("downloader")


# ---------------------------------------------------------------------------
# Node.js path helper — ensures yt-dlp can find node for JS challenge solving
# ---------------------------------------------------------------------------

def _get_enhanced_path() -> str:
    """Return PATH with node.js directories prepended."""
    import shutil
    import os as _os
    base_path = _os.environ.get("PATH", "")
    node_bin  = shutil.which("node")
    if node_bin:
        node_dir = str(Path(node_bin).parent)
        if node_dir not in base_path:
            return node_dir + ":" + base_path
    return base_path


# ---------------------------------------------------------------------------
# Global progress registry (for dashboard)
# ---------------------------------------------------------------------------
progress_registry: Dict[str, Dict[str, Any]] = {}
_registry_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Per-task cancel events (for smart cancel mid-download)
# ---------------------------------------------------------------------------
_cancel_events: Dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def register_cancel(task_id: str) -> threading.Event:
    evt = threading.Event()
    with _cancel_lock:
        _cancel_events[task_id] = evt
    return evt


def trigger_cancel(task_id: str) -> None:
    """Signal a running download to abort."""
    with _cancel_lock:
        evt = _cancel_events.get(task_id)
    if evt:
        evt.set()


def clear_cancel(task_id: str) -> None:
    with _cancel_lock:
        _cancel_events.pop(task_id, None)


# Track bot-detection state so we don't spam the owner
_bot_detection_alerted: bool = False
_alert_lock = threading.Lock()


def _update_progress(task_id: str, data: Dict[str, Any]) -> None:
    with _registry_lock:
        progress_registry[task_id] = data


def _remove_progress(task_id: str) -> None:
    with _registry_lock:
        progress_registry.pop(task_id, None)


def reset_bot_alert() -> None:
    global _bot_detection_alerted
    with _alert_lock:
        _bot_detection_alerted = False


def get_bot_detection_alerted() -> bool:
    with _alert_lock:
        return _bot_detection_alerted


def _mark_bot_alert() -> None:
    global _bot_detection_alerted
    with _alert_lock:
        _bot_detection_alerted = True


# ---------------------------------------------------------------------------
# yt-dlp progress hook
# ---------------------------------------------------------------------------

def _progress_hook(task_id: str):
    """Factory: returns a yt-dlp progress hook bound to *task_id*.
    Also aborts if the task's cancel event is set."""

    def hook(d: Dict[str, Any]) -> None:
        # Smart cancel: abort mid-download
        with _cancel_lock:
            evt = _cancel_events.get(task_id)
        if evt and evt.is_set():
            raise yt_dlp.utils.DownloadError(f"Cancelled: {task_id}")

        status = d.get("status", "")
        if status == "downloading":
            _update_progress(task_id, {
                "downloaded": d.get("downloaded_bytes", 0),
                "total": d.get("total_bytes") or d.get("total_bytes_estimate", 0),
                "speed": d.get("speed") or 0,
                "eta": d.get("eta") or 0,
                "stage": "downloading",
            })
        elif status == "finished":
            _update_progress(task_id, {
                "downloaded": d.get("total_bytes", 0),
                "total": d.get("total_bytes", 0),
                "speed": 0,
                "eta": 0,
                "stage": "postprocessing",
            })

    return hook


# ---------------------------------------------------------------------------
# yt-dlp options builder (wraps auth.build_base_opts)
# ---------------------------------------------------------------------------

def _build_ydl_opts(task: Task, out_dir: Path) -> Dict[str, Any]:
    """Build yt-dlp options for a specific download task."""
    safe_title = sanitize_filename(task.title, max_len=80)
    outtmpl = str(out_dir / f"{task.id}_{safe_title}.%(ext)s")

    fmt = quality_format(task.quality)

    return build_base_opts({
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "continuedl": True,
        "progress_hooks": [_progress_hook(task.id)],
        "postprocessors": [
            {"key": "FFmpegMetadata"},
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ],
    })


def _download_yt_thumbnail(video_id: str, out_dir: Path) -> str:
    """
    Download the actual YouTube thumbnail at highest quality.
    Tries maxresdefault (1280×720) → sddefault (640×480) → hqdefault (480×360).
    Falls back to ffmpeg frame extraction if all URLs fail.
    Returns absolute thumb path, or '' on total failure.
    """
    import urllib.request, subprocess

    thumb_path = out_dir.resolve() / f"{video_id}_thumb.jpg"

    # YouTube CDN thumbnail URLs in quality order
    yt_urls = [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/sddefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    ]

    for url in yt_urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status != 200:
                    continue
                data = resp.read()
                # YouTube returns a small 120×90 "no thumbnail" image (~1.4 KB)
                # for maxresdefault when none exists — skip it
                if len(data) < 5000:
                    continue
                thumb_path.write_bytes(data)
                logger.info("YouTube thumbnail downloaded (%s): %d B", url.split("/")[-1], len(data))
                return str(thumb_path)
        except Exception as exc:
            logger.debug("YT thumb URL %s failed: %s", url, exc)

    # Fallback: extract a frame from the video file
    logger.warning("YouTube thumbnail download failed for %s — falling back to ffmpeg", video_id)
    return ""


def _extract_thumbnail_ffmpeg(video_path: str, video_id: str, out_dir: Path) -> str:
    """
    Fallback: extract a JPEG frame from the downloaded video using ffmpeg.
    Returns absolute thumb path, or '' on failure.
    """
    import subprocess
    thumb_path = out_dir.resolve() / f"{video_id}_thumb.jpg"
    try:
        if thumb_path.exists():
            thumb_path.unlink()
    except OSError:
        pass

    # 720px wide so Telegram shows full-width preview
    vf_scale = "scale='min(720,iw)':-2"

    for seek_sec in ("10", "5", "1", "0"):
        try:
            cmd = [
                "ffmpeg", "-y",
                "-ss", seek_sec,
                "-i", video_path,
                "-vframes", "1",
                "-vf", vf_scale,
                "-q:v", "3",
                "-f", "image2",
                str(thumb_path),
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 1024:
                logger.info("Thumbnail extracted via ffmpeg (%ss): %d B",
                            seek_sec, thumb_path.stat().st_size)
                return str(thumb_path)
        except Exception as exc:
            logger.warning("ffmpeg thumb attempt seek=%s failed: %s", seek_sec, exc)
            break

    logger.warning("Thumbnail extraction (ffmpeg) also failed for %s", video_id)
    return ""


# ---------------------------------------------------------------------------
# Download task
# ---------------------------------------------------------------------------

async def download_task(task: Task, state: StateManager) -> bool:
    """Download a single task. Returns True on success."""
    task_id = task.id
    out_dir = Config.DOWNLOAD_DIR

    logger.info("⬇️  Starting download: %s (%s)", task.title, task.quality)

    # --- Pre-download size estimation ---
    estimated = await _estimate_size(task)
    if estimated > 0 and not has_space_for(estimated):
        logger.warning("Not enough disk for %s (need ~%s)", task.title, human_bytes(estimated))
        state.update_status(task_id, PENDING)
        _remove_progress(task_id)
        return False

    # --- Throttle (respect SLEEP_INTERVAL + hourly cap) ---
    if not apply_request_throttle():
        logger.warning("Rate limit exceeded, deferring %s", task.title)
        state.update_status(task_id, PENDING)
        _remove_progress(task_id)
        return False

    # --- Register cancel event ---
    cancel_evt = register_cancel(task_id)

    # --- Build options & download ---
    opts = _build_ydl_opts(task, out_dir)
    loop = asyncio.get_running_loop()

    try:
        result = await loop.run_in_executor(None, _run_download, task.url, opts)
    except Exception as exc:
        clear_cancel(task_id)
        err_str = str(exc)
        # If cancelled mid-download, silently drop (worker handles cleanup)
        if cancel_evt.is_set() or "Cancelled:" in err_str:
            logger.info("Download cancelled mid-run: %s", task.title)
            _remove_progress(task_id)
            return False
        logger.error("Download exception for %s: %s", task.title, exc)
        await _handle_download_error(task, state, err_str)
        _remove_progress(task_id)
        return False
    finally:
        clear_cancel(task_id)

    # Check if task was cancelled while we were downloading
    current = state.get(task_id)
    if current is None or cancel_evt.is_set():
        logger.info("Task cancelled after download: %s", task_id)
        _remove_progress(task_id)
        return False

    if not result:
        await _handle_download_error(task, state, "Download returned no result")
        _remove_progress(task_id)
        return False

    # --- Find the output file ---
    filepath = _find_output_file(out_dir, task.id)
    if not filepath:
        state.mark_failed(task, "Cannot locate downloaded file")
        _remove_progress(task_id)
        return False

    filesize = os.path.getsize(filepath) if os.path.exists(filepath) else 0

    # --- Download actual YouTube thumbnail (best quality), fallback to ffmpeg ---
    thumb_path_str = await loop.run_in_executor(
        None, _download_yt_thumbnail, task.id, out_dir
    )
    if not thumb_path_str:
        thumb_path_str = await loop.run_in_executor(
            None, _extract_thumbnail_ffmpeg, str(filepath), task.id, out_dir
        )

    # Extract video dimensions for proper Telegram display
    width, height = await loop.run_in_executor(
        None, _get_video_dimensions, str(filepath)
    )

    state.update_status(
        task_id, DOWNLOADED,
        filepath=str(filepath),
        filesize=filesize,
        thumb_path=thumb_path_str,
        width=width,
        height=height,
    )

    _remove_progress(task_id)
    logger.info("✅ Downloaded: %s (%s)", task.title, human_bytes(filesize))
    return True


def _run_download(url: str, opts: Dict[str, Any]) -> bool:
    """Synchronous yt-dlp download (runs in executor).
    Raises on error so the caller receives the real error message."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return True
    except yt_dlp.utils.DownloadError as exc:
        logger.error("yt-dlp DownloadError: %s", exc)
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:
        logger.error("yt-dlp unexpected error: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Error handler — detects bot blocks specifically
# ---------------------------------------------------------------------------

async def _handle_download_error(task: Task, state: StateManager, error: str) -> None:
    """Classify the error and take appropriate action."""

    if is_bot_detection_error(error):
        logger.warning("⚠️  Bot detection triggered for %s", task.title)

        # Pause downloads to avoid hammering YouTube
        state.settings["paused"] = True
        state.mark_dirty()
        state.mark_failed(task, f"Bot detection: {error[:200]}")

        # Alert the owner once
        if not get_bot_detection_alerted():
            _mark_bot_alert()

    else:
        state.mark_failed(task, error)


# ---------------------------------------------------------------------------
# Size estimation (also uses auth)
# ---------------------------------------------------------------------------

async def _estimate_size(task: Task) -> int:
    """Estimate video size without downloading. Returns bytes (0 if unknown)."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _estimate_sync, task)
    except Exception:
        return 0


def _estimate_sync(task: Task) -> int:
    """Synchronous size estimation. Applies throttling."""
    apply_request_throttle()

    fmt = quality_format(task.quality)
    opts = build_base_opts({
        "format": fmt,
        "skip_download": True,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(task.url, download=False)
        return info.get("filesize_approx") or info.get("filesize") or 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Output file finder
# ---------------------------------------------------------------------------

def _get_video_dimensions(video_path: str) -> tuple:
    """Get video width/height using ffprobe. Returns (width, height) or (0, 0)."""
    import subprocess, json as _json
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0", video_path
            ],
            capture_output=True, text=True, timeout=30
        )
        data   = _json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        return stream.get("width", 0) or 0, stream.get("height", 0) or 0
    except Exception:
        return 0, 0


def _find_thumbnail(out_dir: Path, video_id: str) -> Optional[Path]:
    """Find the downloaded thumbnail for a given video ID."""
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return None
    for entry in out_dir.iterdir():
        if entry.is_file() and entry.suffix in (".jpg", ".jpeg") and video_id in entry.name:
            return entry
    # fallback: webp
    for entry in out_dir.iterdir():
        if entry.is_file() and entry.suffix == ".webp" and video_id in entry.name:
            return entry
    return None


def _find_output_file(out_dir: Path, video_id: str) -> Optional[Path]:
    """Find the downloaded MP4 file for a given video ID."""
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return None

    candidates = []
    for entry in out_dir.iterdir():
        if entry.is_file() and entry.suffix in (".mp4", ".mkv", ".webm"):
            if video_id in entry.name:
                candidates.append((entry.stat().st_size, entry))

    mp4s = [(s, p) for s, p in candidates if p.suffix == ".mp4"]
    best = mp4s or candidates
    if best:
        best.sort(key=lambda x: x[0], reverse=True)
        return best[0][1]
    return None


# ---------------------------------------------------------------------------
# Download worker loop
# ---------------------------------------------------------------------------

async def download_worker(
    worker_id: int,
    semaphore: asyncio.Semaphore,
    stop_event: asyncio.Event,
    state: StateManager,
) -> None:
    """Async worker loop. Respects paused flag and semaphore for concurrency."""
    logger.info("Download worker %d started", worker_id)

    while not stop_event.is_set():
        if state.settings.get("paused", False):
            await asyncio.sleep(2)
            continue

        task = state.next_pending()
        if task is None:
            await asyncio.sleep(2)
            continue

        async with semaphore:
            if stop_event.is_set():
                break

            state.update_status(task.id, DOWNLOADING)
            success = await download_task(task, state)

            if not success:
                t = state.get(task.id)
                if t and t.status == DOWNLOADING:
                    t.attempts += 1
                    if t.attempts >= Config.MAX_RETRIES:
                        state.mark_failed(t, f"Failed after {Config.MAX_RETRIES} attempts")
                    else:
                        state.update_status(task.id, PENDING)

    logger.info("Download worker %d stopped", worker_id)
