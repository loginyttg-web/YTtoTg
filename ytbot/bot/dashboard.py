"""
Live dashboard — compact box-style UI.
╭── 🟢 ACTIVE │ ⬇3 ⬆1 │ ↓31 MB/s ↑9 MB/s │ 💾 253 GB ──╮
│ 📊 55.1% (125/227) │ ✓ 124  ✗ 1  ⏳ 102 Pending        │
├────────────────────────────────────────────────────────────┤
│ ⬇ [■■■■■■■■□□  82%] Math Mock Test   612/742MB 18MB/s 7s  │
│ ⬆ [■■■■■■■□□□  67%] Physics Marathon 498/742MB  9MB/s 19s │
╰────────────────────────────────────────────────────────────╯
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from pyrogram import Client
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import Message

from config import Config
from core.downloader import progress_registry, _registry_lock
from core.uploader import upload_progress, _uplock
from core.state import StateManager, DOWNLOADED
from core.system import disk_usage, is_disk_alert
from utils.helpers import human_bytes, human_time_short, short, speed_str
from bot.keyboards import kb_processing

logger = logging.getLogger("dashboard")

dashboard_msg_id:   Optional[int] = None
_dashboard_chat_id: Optional[int] = None
_session_start: float = time.time()
_session_bytes: int   = 0
_last_text: str       = ""

# Inner width between │ chars — 62 chars fits most Telegram mobile screens
W = 62


def set_session_bytes(n: int) -> None:
    global _session_bytes
    _session_bytes = n


# ── Box helpers ────────────────────────────────────────────────────────────────

def _row(content: str) -> str:
    """Wrap content in │…│, padding/truncating to W chars."""
    if len(content) < W:
        content += " " * (W - len(content))
    elif len(content) > W:
        content = content[: W - 1] + "…"
    return f"│{content}│"


def _sep() -> str:
    return "├" + "─" * W + "┤"


def _top(inner: str) -> str:
    """╭── <inner> ──╮ with dashes filling W total."""
    pad   = max(0, W - len(inner))
    left  = pad // 2
    right = pad - left
    return "╭" + "─" * left + inner + "─" * right + "╮"


def _bot() -> str:
    return "╰" + "─" * W + "╯"


def _bar(done: float, total: float) -> str:
    """[■■■■■□□□□□  50%] — always 17 chars wide."""
    if total <= 0:
        return "[□□□□□□□□□□   0%]"
    p = int(min(done / total, 1.0) * 100)
    f = min(p // 10, 10)
    return f"[{'■' * f}{'□' * (10 - f)} {p:3d}%]"


def _spd(bps: float) -> str:
    """Compact speed without space: 18MB/s."""
    if bps <= 0:
        return "—"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f}MB/s"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f}KB/s"
    return f"{bps:.0f}B/s"


def _eta(secs: float) -> str:
    return human_time_short(secs) if secs > 0 else "—"


def _sz(done: float, total: float) -> str:
    """'612/742MB' or '1.2/2.0GB'."""
    d = human_bytes(done, True).replace(" ", "")
    t = human_bytes(total, True).replace(" ", "")
    return f"{d}/{t}"


# ── Main formatter ─────────────────────────────────────────────────────────────

def format_dashboard(state: StateManager) -> str:
    counts    = state.counts()
    paused    = state.settings.get("paused", False)
    completed = counts["completed"]
    failed    = counts["failed"]
    skipped   = counts["skipped"]
    pending   = counts["pending"]
    dl_count  = counts["downloading"]
    up_count  = counts["uploading"]
    total     = counts["total"]
    done      = completed + failed + skipped

    with _registry_lock:
        active_dl = dict(progress_registry)
    with _uplock:
        up_active = dict(upload_progress)

    dl_speed = sum(d.get("speed", 0) for d in active_dl.values())
    ul_speed = sum(d.get("speed", 0) for d in up_active.values())

    du      = disk_usage()
    free_gb = du["free"] / 1_073_741_824  # bytes → GB

    # ── Status ──────────────────────────────────────────────────────────────
    if paused:
        status = "⏸ PAUSED"
    elif dl_count + up_count > 0:
        status = "🟢 ACTIVE"
    else:
        status = "💤 IDLE"

    # ── Header row ──────────────────────────────────────────────────────────
    # Keep it compact; dashes fill the remaining width
    hdr = (
        f" {status} │ ⬇{dl_count} ⬆{up_count}"
        f" │ ↓{_spd(dl_speed)} ↑{_spd(ul_speed)}"
        f" │ 💾 {free_gb:.1f}GB "
    )
    lines = [_top(hdr)]

    # ── Stats row ───────────────────────────────────────────────────────────
    if total > 0:
        pct_val = done * 100 / total
        stats = (
            f" 📊 {pct_val:.1f}% ({done}/{total})"
            f" │ ✓ {completed}  ✗ {failed}  ⏳ {pending} Pending"
        )
    else:
        stats = " 📊 Idle — send a YouTube link to start"
    lines.append(_row(stats))

    # ── Per-task rows ────────────────────────────────────────────────────────
    task_rows: list[str] = []

    # Active downloads (from downloader progress registry)
    for tid, d in list(active_dl.items())[:4]:
        task  = state.get(tid)
        name  = short(task.title, 18) if task else tid[:12]
        stage = d.get("stage", "downloading")

        if stage == "postprocessing":
            task_rows.append(f" ⬇ [■■■■■■■■■■ 100%] {name}  Merging…")
            continue

        dtot  = d.get("total", 0)
        ddone = d.get("downloaded", 0)
        spd   = d.get("speed", 0)
        eta   = d.get("eta", 0)

        bar = _bar(ddone, dtot)
        if dtot > 0:
            info = f"{_sz(ddone, dtot)} {_spd(spd)} {_eta(eta)}"
            task_rows.append(f" ⬇ {bar} {name}  {info}")
        else:
            task_rows.append(f" ⬇ {bar} {name}  Starting…")

    # Downloaded (waiting to upload) — not actively uploading yet
    uploading_ids = set(up_active.keys())
    queued = [t for t in state.by_status(DOWNLOADED) if t.id not in uploading_ids]
    for t in queued[:3]:
        name = short(t.title, 18)
        sz   = human_bytes(t.filesize, True).replace(" ", "") if t.filesize else "?"
        task_rows.append(f" ⬇ [■■■■■■■■■■ 100%] {name}  {sz} ✓ Queued")

    # Active upload(s)
    for tid, d in list(up_active.items())[:2]:
        task  = state.get(tid)
        name  = short(task.title, 18) if task else tid[:12]
        utot  = d.get("total", 0)
        ucur  = d.get("current", 0)
        spd   = d.get("speed", 0)
        eta   = d.get("eta", 0)
        part  = d.get("part", "1/1")

        bar = _bar(ucur, utot)
        if utot > 0:
            pt   = f" P{part}" if part != "1/1" else ""
            info = f"{_sz(ucur, utot)} {_spd(spd)} {_eta(eta)}{pt}"
            task_rows.append(f" ⬆ {bar} {name}  {info}")
        else:
            task_rows.append(f" ⬆ {bar} {name}  Starting…")

    if task_rows:
        lines.append(_sep())
        for row in task_rows:
            lines.append(_row(row))

    # ── Bottom ──────────────────────────────────────────────────────────────
    lines.append(_bot())

    # ── Alerts outside the box (plain text) ─────────────────────────────────
    alerts: list[str] = []
    if is_disk_alert():
        alerts.append("⚠️ Low disk — run /clear")
    if failed > 0 and dl_count == 0 and up_count == 0 and pending == 0:
        alerts.append(f"❌ {failed} failed — /resetqueue to clear")

    box = "```\n" + "\n".join(lines) + "\n```"
    if alerts:
        box += "\n" + "  ".join(alerts)
    return box


# ── Dashboard loop ─────────────────────────────────────────────────────────────

async def dashboard_loop(app: Client, stop_event: asyncio.Event, state: StateManager) -> None:
    global dashboard_msg_id, _dashboard_chat_id, _last_text, _session_bytes

    interval   = Config.PROGRESS_INTERVAL
    last_bytes = state.stats.get("bytes_uploaded", 0)
    logger.info("Dashboard loop started (interval=%ds)", interval)

    while not stop_event.is_set():
        await asyncio.sleep(interval)

        cur = state.stats.get("bytes_uploaded", 0)
        if cur > last_bytes:
            _session_bytes = cur
            last_bytes     = cur

        counts = state.counts()
        if (not state.settings.get("paused") and
                counts.get("downloading", 0) + counts.get("uploading", 0) +
                counts.get("downloaded",  0) + counts.get("pending",   0) == 0):
            continue

        text   = format_dashboard(state)
        markup = kb_processing()

        if text == _last_text and dashboard_msg_id:
            continue

        try:
            if dashboard_msg_id and _dashboard_chat_id:
                await app.edit_message_text(
                    chat_id=_dashboard_chat_id,
                    message_id=dashboard_msg_id,
                    text=text,
                    reply_markup=markup,
                )
            else:
                msg: Message = await app.send_message(
                    chat_id=Config.OWNER_ID,
                    text=text,
                    reply_markup=markup,
                )
                dashboard_msg_id   = msg.id
                _dashboard_chat_id = Config.OWNER_ID
                logger.info("Dashboard created (msg_id=%d)", msg.id)
            _last_text = text

        except MessageNotModified:
            _last_text = text
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
        except Exception as exc:
            err = str(exc)
            if "MESSAGE_ID_INVALID" in err:
                dashboard_msg_id   = None
                _dashboard_chat_id = None
                _last_text         = ""
            else:
                logger.error("Dashboard error: %s", exc)

    dashboard_msg_id   = None
    _dashboard_chat_id = None
    logger.info("Dashboard stopped")
