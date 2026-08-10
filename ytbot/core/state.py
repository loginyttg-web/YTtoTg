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
# User roles
# ---------------------------------------------------------------------------
ROLE_OWNER = "owner"    # full control (only the configured OWNER_ID)
ROLE_ADMIN = "admin"    # manage watches + queue + settings like quality
ROLE_USER = "user"      # submit links, view status
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_USER)

ROLE_ICON = {ROLE_OWNER: "👑", ROLE_ADMIN: "🛡", ROLE_USER: "👤"}


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
    source: str = ""        # 'channel', 'playlist', 'video', 'watch'
    dest_chat_id: int = 0   # 0 → global DEST_CHAT_ID; else channel-specific
    added_by: int = 0       # Telegram user id that added this task

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
# Watch dataclass — a YouTube channel/playlist being auto-monitored
# ---------------------------------------------------------------------------
@dataclass
class Watch:
    id: str                 # short id: "w1", "w2", … (stable, for buttons)
    url: str                # URL that was watched (normalised)
    key: str = ""           # canonical identity (channel_url if known)
    title: str = ""         # channel / playlist name
    dest_chat_id: int = 0   # 0 → global destination
    dest_chat_title: str = ""
    quality: str = ""       # "" → global default quality
    enabled: bool = True
    interval_min: int = 0   # 0 → global WATCH_INTERVAL_MIN
    daily_at: str = ""      # "HH:MM" → check once a day at this time instead
    known_ids: List[str] = field(default_factory=list)  # already-seen videos
    last_check: float = 0.0
    last_new: int = 0       # new videos found on the last check
    checks: int = 0
    added_by: int = 0
    added_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Watch":
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
                kwargs[k] = None
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------
class StateManager:
    """Thread-safe persistent state for all tasks and settings."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.tasks: Dict[str, Task] = {}      # key = video_id
        self.watches: Dict[str, Watch] = {}   # key = watch.id ("w1", …)
        self.users: Dict[int, Dict[str, Any]] = {}  # key = telegram user id
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
            "watch_counter": 0,
            "watcher_paused": False,
            "watch_interval_min": 0,   # 0 → Config.WATCH_INTERVAL_MIN
            "upload_queue_limit": Config.UPLOAD_QUEUE_LIMIT,
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

            # Restore watches
            self.watches = {}
            for wid, wd in raw.get("watches", {}).items():
                self.watches[wid] = Watch.from_dict(wd)

            # Restore users (JSON keys are strings → back to int)
            self.users = {}
            for uid, ud in raw.get("users", {}).items():
                try:
                    self.users[int(uid)] = ud
                except (TypeError, ValueError):
                    continue

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
            logger.info("State loaded: %d tasks, %d watches, %d users",
                        len(self.tasks), len(self.watches), len(self.users))

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
            "watches": {wid: w.to_dict() for wid, w in self.watches.items()},
            "users": {str(uid): u for uid, u in self.users.items()},
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
        self, items: List[Dict[str, Any]], source: str, quality: Optional[str] = None,
        dest_chat_id: int = 0, added_by: int = 0,
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
                    dest_chat_id=dest_chat_id,
                    added_by=added_by,
                )
                self._order_counter += 1
                self.tasks[vid] = task  # always overwrite — no duplicate blocking
                added += 1

            if added:
                self._dirty = True

        logger.info("Added %d tasks, skipped %d (source=%s q=%s dest=%s)",
                    added, skipped, source, q, dest_chat_id or "global")
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

    def claim_next_pending(self, skip_ids: Optional[set] = None) -> Optional[Task]:
        """Atomically claim the next pending task (marks it DOWNLOADING).

        This prevents two workers from grabbing the same task — the claim and
        the status flip happen under one lock. `skip_ids` lets workers defer
        tasks that are in a backoff window (disk full / rate limit).
        """
        with self._lock:
            pending = [
                t for t in self.tasks.values()
                if t.status == PENDING and (not skip_ids or t.id not in skip_ids)
            ]
            if not pending:
                return None
            task = min(pending, key=lambda t: t.order)
            task.status = DOWNLOADING
            task.started_at = time.time()
            self._dirty = True
            return task

    def next_ready_to_upload(self) -> Optional[Task]:
        """Get the next downloaded task ordered by `order`."""
        with self._lock:
            ready = sorted(
                [t for t in self.tasks.values() if t.status == DOWNLOADED],
                key=lambda t: t.order,
            )
            return ready[0] if ready else None

    def claim_next_upload(self) -> Optional[Task]:
        """Atomically claim the next downloaded task (marks it UPLOADING)."""
        with self._lock:
            ready = [t for t in self.tasks.values() if t.status == DOWNLOADED]
            if not ready:
                return None
            task = min(ready, key=lambda t: t.order)
            task.status = UPLOADING
            self._dirty = True
            return task

    def bump_attempts(self, video_id: str) -> int:
        """Increment and return a task's attempt counter (thread-safe)."""
        with self._lock:
            t = self.tasks.get(video_id)
            if not t:
                return 0
            t.attempts += 1
            self._dirty = True
            return t.attempts

    def upload_queue_size(self) -> int:
        """Downloaded-waiting + currently uploading = upload pipeline load."""
        with self._lock:
            return sum(1 for t in self.tasks.values()
                       if t.status in (DOWNLOADED, UPLOADING))

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

    def reserve_channel_number(self, task: Task) -> int:
        """Atomically reserve the next caption number (parallel-upload safe).

        With multiple upload workers two tasks could otherwise grab the same
        number; this reserves it under the state lock and never reuses one.
        """
        channel = (task.channel or "").strip().casefold()
        with self._lock:
            nums = self.settings.setdefault("channel_numbers", {})
            completed = sum(
                1 for t in self.tasks.values()
                if t.status == COMPLETED
                and (t.channel or "").strip().casefold() == channel
            )
            reserved = max(int(nums.get(channel, 0)), completed) + 1
            nums[channel] = reserved
            self._dirty = True
            return reserved

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

    def retry_failed(self) -> int:
        """Re-queue all FAILED tasks at the end of the queue. Returns count."""
        return self.retry_failed_matching()

    def retry_failed_matching(self, markers: Optional[tuple[str, ...]] = None) -> int:
        """Re-queue failed tasks whose error contains one of *markers*.

        With no markers this preserves `/retryfailed`'s existing all-failures
        behaviour. Cookie installation uses this targeted form to recover old
        429/auth failures without retrying unrelated deleted/private videos.
        """
        folded_markers = tuple(marker.casefold() for marker in (markers or ()))
        with self._lock:
            n = 0
            for t in self.tasks.values():
                if t.status != FAILED:
                    continue
                if folded_markers and not any(
                    marker in (t.error or "").casefold() for marker in folded_markers
                ):
                    continue
                t.status = PENDING
                t.error = ""
                t.attempts = 0
                t.filepath = ""
                t.thumb_path = ""
                t.finished_at = 0
                t.order = self._order_counter
                self._order_counter += 1
                n += 1
            if n:
                self._dirty = True
            return n

    # ------------------------------------------------------------------
    # Watches — auto-monitored YouTube channels/playlists
    # ------------------------------------------------------------------
    def next_watch_id(self) -> str:
        """Return a fresh short watch id like 'w3'."""
        with self._lock:
            n = int(self.settings.get("watch_counter", 0)) + 1
            self.settings["watch_counter"] = n
            self._dirty = True
            return f"w{n}"

    def add_watch(
        self, watch_id: str, url: str, key: str, title: str,
        known_ids: List[str], dest_chat_id: int = 0, dest_chat_title: str = "",
        quality: str = "", added_by: int = 0,
    ) -> Watch:
        """Create (or overwrite) a watch subscription."""
        w = Watch(
            id=watch_id, url=url, key=key or url, title=title,
            dest_chat_id=dest_chat_id, dest_chat_title=dest_chat_title,
            quality=quality, enabled=True,
            known_ids=list(known_ids),
            added_by=added_by, added_at=time.time(),
        )
        with self._lock:
            self.watches[watch_id] = w
            self._dirty = True
        logger.info("Watch added: %s → %s (dest=%s)", title, url, dest_chat_id or "global")
        return w

    def get_watch(self, watch_id: str) -> Optional[Watch]:
        with self._lock:
            return self.watches.get(watch_id)

    def watch_by_key(self, *candidates: str) -> Optional[Watch]:
        """Find a watch whose key/url matches any of the candidate URLs."""
        norm = {c.rstrip("/").lower() for c in candidates if c}
        if not norm:
            return None
        with self._lock:
            for w in self.watches.values():
                if (w.key or "").rstrip("/").lower() in norm:
                    return w
                if (w.url or "").rstrip("/").lower() in norm:
                    return w
        return None

    def remove_watch(self, watch_id: str) -> Optional[Watch]:
        with self._lock:
            w = self.watches.pop(watch_id, None)
            if w:
                self._dirty = True
        if w:
            logger.info("Watch removed: %s (%s)", w.title, watch_id)
        return w

    def all_watches(self) -> List[Watch]:
        with self._lock:
            return sorted(self.watches.values(), key=lambda w: w.added_at)

    def watch_interval(self, watch: Watch) -> int:
        """Effective check interval (minutes) for a watch."""
        if watch.interval_min > 0:
            return watch.interval_min
        s = int(self.settings.get("watch_interval_min", 0))
        return s if s > 0 else Config.WATCH_INTERVAL_MIN

    def watch_due(self, watch: Watch, now: Optional[float] = None) -> bool:
        """Is it time to check this watch?

        Two schedule modes:
        • daily_at = "HH:MM" → due once per day, at/after that wall-clock
          time (if the bot was off at 6:00 and starts at 9:00, the check
          still runs once).
        • otherwise → interval mode: due every `watch_interval()` minutes.
        """
        from datetime import datetime, timedelta

        if not watch.enabled:
            return False
        now = now or time.time()

        if watch.daily_at:
            try:
                hh, mm = (int(x) for x in watch.daily_at.split(":"))
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    return False
            except (ValueError, AttributeError):
                return False
            local  = datetime.fromtimestamp(now)
            target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if local < target:
                target -= timedelta(days=1)   # most recent occurrence
            return watch.last_check < target.timestamp()

        return (now - watch.last_check) >= self.watch_interval(watch) * 60

    def watch_schedule_label(self, watch: Watch) -> str:
        """Human schedule: '⏰ daily 06:00' or '⏱ every 30m'."""
        if watch.daily_at:
            return f"⏰ daily at {watch.daily_at}"
        return f"⏱ every {self.watch_interval(watch)}m"

    # ------------------------------------------------------------------
    # Users & roles
    # ------------------------------------------------------------------
    def role_of(self, user_id: int) -> Optional[str]:
        """Return 'owner' | 'admin' | 'user' | None for a Telegram user id."""
        if user_id == Config.OWNER_ID:
            return ROLE_OWNER
        u = self.users.get(user_id)
        return u.get("role") if u else None

    def add_user(self, user_id: int, role: str, name: str = "", added_by: int = 0) -> None:
        if role not in (ROLE_ADMIN, ROLE_USER):
            role = ROLE_USER
        with self._lock:
            existing = self.users.get(user_id, {})
            self.users[user_id] = {
                "role": role,
                "name": name or existing.get("name", ""),
                "added_by": added_by or existing.get("added_by", 0),
                "added_at": existing.get("added_at") or time.time(),
            }
            self._dirty = True
        logger.info("User added: %d (%s) role=%s", user_id, name, role)

    def remove_user(self, user_id: int) -> bool:
        with self._lock:
            removed = self.users.pop(user_id, None) is not None
            if removed:
                self._dirty = True
        return removed

    def set_role(self, user_id: int, role: str) -> bool:
        if role not in (ROLE_ADMIN, ROLE_USER):
            return False
        with self._lock:
            u = self.users.get(user_id)
            if not u:
                return False
            u["role"] = role
            self._dirty = True
        return True

    def all_users(self) -> List[Tuple[int, Dict[str, Any]]]:
        """All registered users as (user_id, info) sorted by role then name."""
        order = {ROLE_ADMIN: 0, ROLE_USER: 1}
        with self._lock:
            return sorted(self.users.items(),
                          key=lambda kv: (order.get(kv[1].get("role"), 9),
                                          kv[1].get("name", "")))

    def ids_with_role(self, *roles: str) -> List[int]:
        """User ids holding any of the given roles (owner always included)."""
        out = [Config.OWNER_ID] if ROLE_OWNER in roles else []
        for uid, u in self.users.items():
            if u.get("role") in roles:
                out.append(uid)
        return out

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def reset_daily_stats(self) -> None:
        """Reset the rolling daily counters (called after the daily report)."""
        with self._lock:
            self.stats.update({
                "completed": 0,
                "failed": 0,
                "skipped": 0,
                "bytes_uploaded": 0,
                "total_time": 0.0,
                "failed_list": [],
                "date": time.strftime("%Y-%m-%d"),
            })
            self._dirty = True
        logger.info("Daily stats reset")

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
