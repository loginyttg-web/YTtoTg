"""
StateManager — JSON persistence, queue ops, crash recovery.
All state lives in one JSON file, written atomically (temp → rename).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import Config, quality_format

logger = logging.getLogger("state")

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
PENDING = "pending"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
UPLOADING = "uploading"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"

ACTIVE_STATUSES = {PENDING, DOWNLOADING, DOWNLOADED, UPLOADING}
TERMINAL_STATUSES = {COMPLETED, FAILED, SKIPPED, CANCELLED}


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------
@dataclass
class Task:
    id: str  # YouTube video ID
    url: str
    title: str = ""
    status: str = PENDING
    quality: str = "best"
    order: int = 0
    attempts: int = 0
    error: str = ""
    filepath: str = ""
    filesize: int = 0
    duration: str = ""
    channel: str = ""
    upload_date: str = ""   # YouTube publish date YYYYMMDD
    upload_time: str = ""   # YouTube publish time HH:MM (when available)
    thumb_path: str = ""    # Local thumbnail file path
    width: int = 0          # Video width in pixels
    height: int = 0         # Video height in pixels
    parts: List[str] = field(default_factory=list)
    added_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    source: str = ""  # 'channel', 'playlist', 'video'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        import dataclasses
        kwargs = {}
        for k, f in cls.__dataclass_fields__.items():
            if k in d:
                kwargs[k] = d[k]
            elif f.default is not dataclasses.MISSING:
                kwargs[k] = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                kwargs[k] = f.default_factory()
            else:
                kwargs[k] = None  # required field missing — state is corrupt
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------
class StateManager:
    """Thread-safe persistent state for all tasks and settings."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.tasks: Dict[str, Task] = {}  # key = video_id
        self.settings: Dict[str, Any] = {
            "parallel_downloads": Config.PARALLEL_DOWNLOADS,
            "quality": Config.DEFAULT_QUALITY,
            "paused": False,
            "sort_order": "new_old",
            "current_source": None,
            "current_channel": "",
            "dest_chat_id": 0,
            "dest_chat_title": "",
            "dest_history": [],
        }
        self.stats: Dict[str, Any] = {
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "bytes_uploaded": 0,
            "total_time": 0.0,
            "failed_list": [],
            "date": time.strftime("%Y-%m-%d"),
        }
        self._dirty = False
        self._lock = threading.Lock()
        self._order_counter: int = 0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Load state from disk; recover interrupted tasks."""
        if not self.filepath.exists():
            logger.info("No existing state.json — starting fresh")
            self.save()
            return

        with self._lock:
            try:
                raw = json.loads(self.filepath.read_text("utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Corrupt state.json, starting fresh: %s", exc)
                self.save()
                return

            # Restore tasks
            self.tasks = {}
            for vid, td in raw.get("tasks", {}).items():
                task = Task.from_dict(td)
                # Recover interrupted states →
                if task.status == DOWNLOADING:
                    logger.info("Recovering interrupted download: %s", task.id)
                    task.status = PENDING
                elif task.status == UPLOADING:
                    logger.info("Recovering interrupted upload: %s", task.id)
                    task.status = DOWNLOADED
                self.tasks[vid] = task

            self.settings.update(raw.get("settings", self.settings))
            self.stats.update(raw.get("stats", self.stats))

            # Restore saved dest_chat_id into Config so /setchannel persists across restarts
            saved_dest = self.settings.get("dest_chat_id", 0)
            if saved_dest and saved_dest != 0:
                Config.DEST_CHAT_ID = int(saved_dest)
                logger.info("Restored DEST_CHAT_ID from state: %d", Config.DEST_CHAT_ID)

            # Restore order counter
            if self.tasks:
                self._order_counter = max(t.order for t in self.tasks.values()) + 1

            # Reset daily stats if date changed
            today = time.strftime("%Y-%m-%d")
            if self.stats.get("date") != today:
                self.stats = {
                    "completed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "bytes_uploaded": 0,
                    "total_time": 0.0,
                    "failed_list": [],
                    "date": today,
                }

            self._dirty = False
            logger.info("State loaded: %d tasks", len(self.tasks))

    def save(self) -> None:
        """Atomic write: temp file → rename."""
        with self._lock:
            data = self._serialize()
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=self.filepath.parent, suffix=".tmp")
                os.close(fd)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.filepath)
                self._dirty = False
            except OSError as exc:
                logger.error("Failed to save state: %s", exc)
                if tmp and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def _serialize(self) -> Dict[str, Any]:
        return {
            "tasks": {vid: t.to_dict() for vid, t in self.tasks.items()},
            "settings": self.settings,
            "stats": self.stats,
        }

    def mark_dirty(self) -> None:
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ------------------------------------------------------------------
    # Autosave loop
    # ------------------------------------------------------------------
    async def autosave_loop(self, interval: int = 15, stop_event=None):
        """Background coroutine: periodically save if dirty."""
        while stop_event is None or not stop_event.is_set():
            await _sleep(interval)
            if self._dirty:
                self.save()

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------
    def add_tasks(
        self, items: List[Dict[str, Any]], source: str, quality: Optional[str] = None
    ) -> Tuple[int, int]:
        """Batch-add tasks without duplicate filtering. Returns (added, 0)."""
        added = 0
        skipped = 0
        q = quality or self.settings["quality"]

        with self._lock:
            for item in items:
                vid = item.get("id") or ""
                if not vid:
                    continue

                task = Task(
                    id=vid,
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    quality=q,
                    order=self._order_counter,
                    duration=item.get("duration", ""),
                    channel=item.get("channel", ""),
                    upload_date=item.get("upload_date", ""),
                    upload_time=item.get("upload_time", ""),
                    width=item.get("width", 0),
                    height=item.get("height", 0),
                    added_at=time.time(),
                    source=source,
                )
                self._order_counter += 1
                self.tasks[vid] = task  # always overwrite — no duplicate blocking
                added += 1

            if added:
                self._dirty = True

        logger.info("Added %d tasks, skipped %d (source=%s q=%s)", added, skipped, source, q)
        return added, skipped

    def remove_task(self, video_id: str) -> bool:
        """Remove a task entirely from the queue."""
        with self._lock:
            if video_id in self.tasks:
                del self.tasks[video_id]
                self._dirty = True
                return True
        return False

    # ------------------------------------------------------------------
    # Queue iterators
    # ------------------------------------------------------------------
    def next_pending(self) -> Optional[Task]:
        """Get the next pending task ordered by `order` field."""
        with self._lock:
            pending = sorted(
                [t for t in self.tasks.values() if t.status == PENDING],
                key=lambda t: t.order,
            )
            return pending[0] if pending else None

    def next_ready_to_upload(self) -> Optional[Task]:
        """Get the next downloaded task ordered by `order`."""
        with self._lock:
            ready = sorted(
                [t for t in self.tasks.values() if t.status == DOWNLOADED],
                key=lambda t: t.order,
            )
            return ready[0] if ready else None

    # ------------------------------------------------------------------
    # Filter / query
    # ------------------------------------------------------------------
    def by_status(self, *statuses: str) -> List[Task]:
        with self._lock:
            return sorted(
                [t for t in self.tasks.values() if t.status in statuses],
                key=lambda t: t.order,
            )

    def get(self, video_id: str) -> Optional[Task]:
        with self._lock:
            return self.tasks.get(video_id)

    def counts(self) -> Dict[str, int]:
        with self._lock:
            c = {s: 0 for s in (PENDING, DOWNLOADING, DOWNLOADED, UPLOADING,
                                COMPLETED, FAILED, SKIPPED, CANCELLED)}
            for t in self.tasks.values():
                c[t.status] = c.get(t.status, 0) + 1
            c["total"] = len(self.tasks)
            return c

    def all_tasks(self) -> List[Task]:
        with self._lock:
            return sorted(self.tasks.values(), key=lambda t: t.order)

    def active_count(self) -> int:
        """Tasks not in terminal state."""
        return sum(1 for t in self.tasks.values() if t.status in ACTIVE_STATUSES)

    def next_channel_number(self, task: Task) -> int:
        """Return the next caption number for this source channel.

        Numbering is intentionally independent for each YouTube channel and
        starts at 1 after history is cleared. Uploads are sequential, so the
        count of already completed tasks is stable while a video is sent.
        """
        channel = (task.channel or "").strip().casefold()
        with self._lock:
            completed = [
                t for t in self.tasks.values()
                if t.status == COMPLETED
                and (t.channel or "").strip().casefold() == channel
            ]
            return len(completed) + 1

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------
    def update_status(self, video_id: str, status: str, **kwargs) -> bool:
        with self._lock:
            t = self.tasks.get(video_id)
            if not t:
                return False
            old_status = t.status
            t.status = status
            for k, v in kwargs.items():
                if hasattr(t, k):
                    setattr(t, k, v)
            if status == DOWNLOADING:
                t.started_at = time.time()
            if status in TERMINAL_STATUSES:
                t.finished_at = time.time()
            self._dirty = True
            return True

    def mark_completed(self, task: Task) -> None:
        """Mark task completed and update stats."""
        with self._lock:
            task.status = COMPLETED
            task.finished_at = time.time()
            self.stats["completed"] += 1
            self.stats["bytes_uploaded"] += task.filesize
            elapsed = task.finished_at - task.started_at if task.started_at else 0
            self.stats["total_time"] += elapsed
            self._dirty = True
        logger.info("✅ Completed: %s (%s)", task.title, human_bytes(task.filesize))

    def mark_failed(self, task: Task, error: str) -> None:
        """Mark task failed and record error."""
        with self._lock:
            task.status = FAILED
            task.error = error[:500]
            task.finished_at = time.time()
            self.stats["failed"] += 1
            self.stats["failed_list"].append({
                "title": task.title,
                "url": task.url,
                "error": task.error,
            })
            self._dirty = True
        logger.error("❌ Failed: %s — %s", task.title, error)

    def cancel_and_remove(self, video_id: str) -> Optional[Task]:
        """Cancel a task and immediately remove it from the queue.
        Returns the removed task (for cleanup), or None if not found."""
        with self._lock:
            task = self.tasks.pop(video_id, None)
            self._dirty = True
        if task:
            logger.info("🚫 Cancelled & removed: %s", task.title or video_id)
        return task

    def mark_skipped(self, task: Task, reason: str = "") -> None:
        with self._lock:
            task.status = SKIPPED
            task.error = reason
            task.finished_at = time.time()
            self.stats["skipped"] += 1
            self._dirty = True

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------
    def reset_finished(self) -> int:
        """Remove completed, failed, cancelled, skipped tasks. Returns count."""
        with self._lock:
            to_remove = [vid for vid, t in self.tasks.items()
                         if t.status in (COMPLETED, FAILED, CANCELLED, SKIPPED)]
            for vid in to_remove:
                del self.tasks[vid]
            self._dirty = True
            return len(to_remove)

    def clear_all(self) -> int:
        """Emergency: remove ALL tasks."""
        with self._lock:
            count = len(self.tasks)
            self.tasks.clear()
            self._dirty = True
            return count

    # ------------------------------------------------------------------
    # Destination message ID tracking (for /purge)
    # Bots cannot call get_chat_history, so we track what we send.
    # ------------------------------------------------------------------
    _DEST_MSG_CAP = 2000   # keep last N message IDs

    def track_dest_msgs(self, msg_ids: List[int]) -> None:
        """Record Telegram message IDs sent to DEST_CHAT_ID."""
        if not msg_ids:
            return
        with self._lock:
            bucket: List[int] = self.settings.setdefault("dest_msg_ids", [])
            bucket.extend(msg_ids)
            # Trim to cap — keep the most recent (tail)
            if len(bucket) > self._DEST_MSG_CAP:
                self.settings["dest_msg_ids"] = bucket[-self._DEST_MSG_CAP:]
            self._dirty = True

    def get_dest_msgs(self, n: int) -> List[int]:
        """Return the last *n* tracked dest message IDs (most-recent first)."""
        with self._lock:
            bucket: List[int] = self.settings.get("dest_msg_ids", [])
            return list(reversed(bucket[-n:])) if bucket else []

    def reorder_tasks(self, sort_order: str) -> None:
        """Re-sort pending tasks by YouTube upload_date (falls back to added_at)."""
        with self._lock:
            pending = [t for t in self.tasks.values() if t.status == PENDING]
            # Sort by YouTube upload_date (YYYYMMDD string sorts correctly)
            def _dt_key(t: "Task") -> str:
                # Combine YYYYMMDD + HHMM so same-day videos sort correctly
                d  = t.upload_date or "0"
                tm = (t.upload_time or "").replace(":", "").ljust(4, "0")
                return d + tm

            if sort_order == "new_old":
                pending.sort(key=_dt_key, reverse=True)
            else:
                pending.sort(key=_dt_key)
            # Reassign order
            base_order = (
                max((t.order for t in self.tasks.values()), default=0) + 100
            )
            for i, t in enumerate(pending):
                t.order = base_order + i
            self.settings["sort_order"] = sort_order
            self._dirty = True
            logger.info("Reordered %d tasks: %s", len(pending), sort_order)


# ---------------------------------------------------------------------------
# Helpers (imported here to avoid circular dependency with utils.helpers)
# ---------------------------------------------------------------------------
from utils.helpers import human_bytes  # noqa: E402


async def _sleep(seconds: float) -> None:
    """Async sleep helper."""
    import asyncio
    await asyncio.sleep(seconds)
