"""
Channel Watcher — auto-detects new uploads on watched YouTube channels.

How it works
------------
1. `/watch <channel_url>` scans the channel once and stores every current
   video ID in `watch.known_ids` (a snapshot).
2. A background loop re-scans each enabled watch every WATCH_INTERVAL_MIN
   minutes (flat extraction — fast and cheap).
3. Any video ID NOT in `known_ids` is brand new → it gets queued for
   download/upload to the watch's own destination chat.
4. Owners/admins are notified in Telegram whenever new videos are found.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from pyrogram import Client

from config import Config
from core.auth import is_bot_detection_error
from core.scraper import scan, sort_items
from core.state import StateManager, Watch

logger = logging.getLogger("watcher")

KNOWN_IDS_CAP = 5000  # per watch — more than enough for any channel


# ---------------------------------------------------------------------------
# Single-watch check
# ---------------------------------------------------------------------------

async def check_watch(state: StateManager, watch: Watch) -> List[dict]:
    """
    Scan one watched channel/playlist, queue any new videos.
    Returns the list of newly queued items (empty if none).
    Raises on bot-detection errors so the loop can pause & alert.
    """
    try:
        # scan() owns request throttling; doing it here as well doubled every
        # watcher delay/request budget and contributed to rate limiting.
        result = await scan(watch.url)
    except Exception as exc:
        watch.last_check = time.time()
        state.mark_dirty()
        if is_bot_detection_error(str(exc)):
            raise
        logger.error("Watch check failed for %s: %s", watch.title or watch.url, exc)
        return []

    items = result.get("items", [])
    watch.title   = result.get("channel") or watch.title
    watch.checks += 1
    watch.last_check = time.time()

    known     = set(watch.known_ids)
    new_items = [it for it in items if it.get("id") and it["id"] not in known]

    # Always refresh the known snapshot (also picks up IDs we may have missed)
    all_ids = list(dict.fromkeys(watch.known_ids + [it["id"] for it in items]))
    watch.known_ids = all_ids[-KNOWN_IDS_CAP:]

    if not new_items:
        watch.last_new = 0
        state.mark_dirty()
        logger.info("Watch %s: no new videos (%d checked)", watch.title, watch.checks)
        return []

    # Oldest first so uploads happen in publish order
    new_items = sort_items(new_items, "old_new")

    quality = watch.quality or state.settings.get("quality", "best")
    added, _ = state.add_tasks(
        new_items,
        source="watch",
        quality=quality,
        dest_chat_id=watch.dest_chat_id,
        added_by=watch.added_by,
    )

    watch.last_new = added
    state.mark_dirty()
    logger.info("🔔 Watch %s: %d new videos queued", watch.title, added)
    return new_items[:added] if added else new_items


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

async def notify_new_videos(
    app: Client, state: StateManager, watch: Watch, new_items: List[dict]
) -> None:
    """Tell the owner (and whoever added the watch) that new videos arrived."""
    dest_title = watch.dest_chat_title or (
        str(watch.dest_chat_id) if watch.dest_chat_id else "global destination"
    )

    lines = [
        "🔔 **𝗡𝗲𝘄 𝗩𝗶𝗱𝗲𝗼𝘀 𝗗𝗲𝘁𝗲𝗰𝘁𝗲𝗱!**",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⋄ 📺 Channel: **{watch.title or 'Unknown'}**",
        f"⋄ 🎬 New videos: `{len(new_items)}`",
        f"⋄ 📍 Destination: `{dest_title}`",
        "",
    ]
    for i, it in enumerate(new_items[:5], 1):
        lines.append(f"{i}. {it.get('title', 'Untitled')[:60]}")
    if len(new_items) > 5:
        lines.append(f"…and {len(new_items) - 5} more")
    lines.append("")
    lines.append("_Auto-queued for download & upload._")

    targets = list(dict.fromkeys([Config.OWNER_ID, watch.added_by]))
    for chat_id in targets:
        if not chat_id:
            continue
        try:
            await app.send_message(chat_id=chat_id, text="\n".join(lines))
        except Exception as exc:
            logger.warning("New-video notification failed for %d: %s", chat_id, exc)


async def alert_watcher_paused(app: Client, watch: Watch, error: Exception) -> None:
    """Owner alert when YouTube bot-detection stops the watcher."""
    try:
        await app.send_message(
            chat_id=Config.OWNER_ID,
            text=(
                "🛑 **Watcher paused — YouTube bot detection!**\n\n"
                f"Scanning `{watch.title or watch.url}` was blocked:\n"
                f"`{str(error)[:200]}`\n\n"
                "Fix auth (new `/cookies`) then run `/watchresume`."
            ),
        )
    except Exception as exc:
        logger.error("Watcher alert failed: %s", exc)


# ---------------------------------------------------------------------------
# Watcher background loop
# ---------------------------------------------------------------------------

async def watcher_loop(app: Client, stop_event: asyncio.Event, state: StateManager) -> None:
    """Tick every 30s; check every watch whose interval has elapsed."""
    logger.info("Watcher loop started (default interval=%dm)", Config.WATCH_INTERVAL_MIN)

    while not stop_event.is_set():
        # 30-second tick that can be interrupted instantly by shutdown
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass

        if state.settings.get("watcher_paused") or state.settings.get("paused"):
            continue

        now = time.time()
        due: List[Watch] = [w for w in state.all_watches() if state.watch_due(w, now)]
        if not due:
            continue

        # Check the most stale watch first
        for watch in sorted(due, key=lambda w: w.last_check):
            if stop_event.is_set():
                break
            if state.settings.get("watcher_paused") or state.settings.get("paused"):
                break

            logger.info("Checking watch %s (%s)…", watch.id, watch.title or watch.url)
            try:
                new_items = await check_watch(state, watch)
            except Exception as exc:
                if is_bot_detection_error(str(exc)):
                    state.settings["watcher_paused"] = True
                    state.settings["watcher_pause_reason"] = "youtube_auth"
                    state.mark_dirty()
                    logger.warning("Watcher paused due to bot detection")
                    await alert_watcher_paused(app, watch, exc)
                    break
                logger.error("Watcher error on %s: %s", watch.title, exc)
                continue

            if new_items:
                await notify_new_videos(app, state, watch, new_items)
                state.save()  # persist new tasks + snapshot immediately

    logger.info("Watcher loop stopped")
