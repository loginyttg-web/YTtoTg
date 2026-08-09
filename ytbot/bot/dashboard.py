"""
Live dashboard — clean box UI with smooth gradient progress bars.

╭─────────────────── 🟢 ACTIVE ───────────────────╮
│ ⬇2  ⬆1   ↓31.2MB/s 🚀  ↑9.1MB/s 🐇   💾 253GB  │
│ Queue ██████████▏─────────  51%  ·  125/227     │
│ ✓ 124   ✗ 1   ⏳ 101 pending                    │
├─────────────────────────────────────────────────┤
│ ⬇ Math Mock Test #4                             │
│   ████████▏───  68%  504/742MB · 18MB/s · 13s   │
│ ⬆ Physics Marathon                              │
│   ██████▎─────  53%  392/742MB · 9MB/s · 39s    │
╰───────────── ↑ 1.2GB sent · 12m run ────────────╯
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
from utils.helpers import (
    human_bytes, human_time_short, short, speed_str_compact, speed_tier,
    bar_smooth, strip_md,
)
from bot.keyboards import kb_processing

logger = logging.getLogger("dashboard")

dashboard_msg_id:   Optional[int] = None
_dashboard_chat_id: Optional[int] = None
_session_start: float = time.time()
_session_bytes: int   = 0
_last_text: str       = ""

# Inner width between │ chars — fits Telegram mobile monospace rendering
W = 54

BAR_W = 12  # progress-bar width (chars)


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


def _bot(inner: str = "") -> str:
    """╰── <inner> ──╯ bottom edge, optionally with centred text."""
    if not inner:
        return "╰" + "─" * W + "╯"
    pad   = max(0, W - len(inner))
    left  = pad // 2
    right = pad - left
    return "╰" + "─" * left + inner + "─" * right + "╯"


def _spd(bps: float) -> str:
    return speed_str_compact(bps)


def _eta(secs: float) -> str:
    return human_time_short(secs) if secs and secs > 0 else "—"


def _sz(done: float, total: float) -> str:
    """'612/742MB' or '1.2/2.0GB'."""
    d = human_bytes(done, True).replace(" ", "")
    t = human_bytes(total, True).replace(" ", "")
    return f"{d}/{t}"


def _task_bar_row(done: float, total: float, speed: float, eta: float,
                  extra: str = "") -> str:
    """`  ████████▏───  68%  504/742MB · 18MB/s · 13s`"""
    if total > 0:
        pct = int(min(done / total, 1.0) * 100)
        seg = f"{_sz(done, total)} · {_spd(speed)} · {_eta(eta)}"
    else:
        pct = 0
        seg = "Starting…"
    if extra:
        seg = f"{seg} {extra}"
    bar = bar_smooth(done, total, BAR_W)
    return f"  {bar} {pct:>3d}%  {seg}"


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
    done      = completed + failed + skipped + counts.get("cancelled", 0)

    with _registry_lock:
        active_dl = dict(progress_registry)
    with _uplock:
        up_active = dict(upload_progress)

    dl_speed = sum(d.get("speed", 0) for d in active_dl.values())
    ul_speed = sum(d.get("speed", 0) for d in up_active.values())

    du      = disk_usage()
    free_gb = du["free"] / 1_073_741_824  # bytes → GB

    # ── Status pill ─────────────────────────────────────────────────────────
    if paused:
        status = "⏸ PAUSED"
    elif dl_count + up_count > 0:
        status = "🟢 ACTIVE"
    elif pending > 0:
        status = "🟡 STARTING"
    else:
        status = "💤 IDLE"

    # ── Header ──────────────────────────────────────────────────────────────
    lines = [_top(f" {status} ")]

    speed_bits = []
    if dl_speed > 0:
        speed_bits.append(f"↓{_spd(dl_speed)} {speed_tier(dl_speed)}")
    if ul_speed > 0:
        speed_bits.append(f"↑{_spd(ul_speed)} {speed_tier(ul_speed)}")
    speed_txt = "  ".join(speed_bits) if speed_bits else "↓—  ↑—"

    lines.append(_row(f" ⬇{dl_count}  ⬆{up_count}   {speed_txt}   💾 {free_gb:.0f}GB free"))

    # ── Queue overview ──────────────────────────────────────────────────────
    if total > 0:
        pct_val = done * 100 / total
        qbar    = bar_smooth(done, total, 16)
        lines.append(_row(f" Queue {qbar} {pct_val:>3.0f}% · {done}/{total}"))
        lines.append(_row(
            f" ✓ {completed}   ✗ {failed}   ⏳ {pending} pending"
            + (f"   ⏭ {skipped}" if skipped else "")
        ))
    else:
        lines.append(_row(" Queue is empty — send a YouTube link 🎬"))

    # ── Per-task rows ───────────────────────────────────────────────────────
    task_rows: list[str] = []

    # Active downloads (from downloader progress registry)
    for tid, d in list(active_dl.items())[:4]:
        task  = state.get(tid)
        name  = strip_md(short(task.title, 34) if task else tid[:12])

        stage = d.get("stage", "downloading")
        if stage == "postprocessing":
            task_rows.append(f" ⬇ {name}")
            task_rows.append(f"  {'█' * BAR_W} 100%  Merging / converting…")
            continue

        dtot  = d.get("total", 0)
        ddone = d.get("downloaded", 0)
        spd   = d.get("speed", 0)
        eta   = d.get("eta", 0)

        task_rows.append(f" ⬇ {name}")
        task_rows.append(_task_bar_row(ddone, dtot, spd, eta))

    # Downloaded, waiting for upload
    uploading_ids = set(up_active.keys())
    queued = [t for t in state.by_status(DOWNLOADED) if t.id not in uploading_ids]
    for t in queued[:3]:
        name = strip_md(short(t.title, 34))
        sz   = human_bytes(t.filesize, True).replace(" ", "") if t.filesize else "?"
        task_rows.append(f" ✓ {name}")
        task_rows.append(f"  {'█' * BAR_W} 100%  {sz} · waiting to upload")

    # Active upload(s)
    for tid, d in list(up_active.items())[:2]:
        task = state.get(tid)
        name = strip_md(short(task.title, 34) if task else tid[:12])
        utot = d.get("total", 0)
        ucur = d.get("current", 0)
        spd  = d.get("speed", 0)
        eta  = d.get("eta", 0)
        part = d.get("part", "1/1")
        extra = f"· P{part}" if part != "1/1" else ""

        task_rows.append(f" ⬆ {name}")
        task_rows.append(_task_bar_row(ucur, utot, spd, eta, extra))

    if task_rows:
        lines.append(_sep())
        for row in task_rows:
            lines.append(_row(row))

    # ── Footer — session totals ─────────────────────────────────────────────
    sent_bytes = state.stats.get("bytes_uploaded", 0)
    run_secs   = time.time() - _session_start
    foot = f" ↑ {human_bytes(sent_bytes, True)} sent · {human_time_short(run_secs)} run "
    lines.append(_bot(foot))

    # ── Alerts (outside the box) ────────────────────────────────────────────
    alerts: list[str] = []
    if is_disk_alert():
        alerts.append(f"⚠️ Disk {du['percent']:.0f}% full — free space or run /clear")
    if failed > 0 and dl_count == 0 and up_count == 0 and pending == 0:
        alerts.append(f"❌ {failed} failed — /retryfailed to re-queue")

    box = "```\n" + "\n".join(lines) + "\n```"
    if alerts:
        box += "\n" + "\n".join(alerts)
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
        markup = kb_processing(paused=state.settings.get("paused", False))

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
