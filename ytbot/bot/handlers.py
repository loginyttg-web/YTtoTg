"""
Message and callback-query handlers for the bot.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import Config, quality_label
from core.scraper import scan, sort_items, generate_txt
from core.state import StateManager, PENDING, COMPLETED, FAILED, CANCELLED, SKIPPED
from core.system import disk_report as sys_disk_report, server_report as sys_server_report
from core.auth import auth_status, bot_detection_help
from core.downloader import get_bot_detection_alerted, reset_bot_alert, trigger_cancel
from utils.helpers import (
    human_bytes, human_time, human_time_short, short, parse_video_id, classify_url,
    sanitize_filename, progress_bar, speed_str, SEP,
)
from utils.logger import tail_log
from bot.keyboards import kb_sort, kb_quality, kb_processing, kb_confirm, kb_start, kb_tasks_page, kb_video, kb_channels

logger = logging.getLogger("handlers")

state: Optional[StateManager] = None
stop_event: Optional[asyncio.Event] = None


def setup(state_mgr: StateManager, evt: asyncio.Event) -> None:
    global state, stop_event
    state = state_mgr
    stop_event = evt


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def owner_only(_, __, update) -> bool:
    """
    Accept updates from the owner in any chat context:
      1. Direct user message / callback — from_user.id == OWNER_ID
      2. Channel post — owner posts *as the channel* (from_user is None,
         sender_chat is the dest channel) — trust DEST_CHAT_ID posts.
    """
    # Normal user message or callback query (from_user always set for CBQ)
    fu = getattr(update, "from_user", None)
    if fu is not None:
        return fu.id == Config.OWNER_ID
    # Channel post: from_user is None, sender_chat carries the channel identity
    sender = getattr(update, "sender_chat", None)
    if sender is not None:
        # Compare absolute values — Pyrogram sometimes strips the -100 prefix
        if abs(sender.id) == abs(Config.DEST_CHAT_ID):
            return True
    return False

owner_filter = filters.create(owner_only)

YT_PATTERN = r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|[a-zA-Z0-9_-]{11}|@[a-zA-Z0-9_.-]+|playlist\?list=|channel/|c/|shorts/)"

def yt_url(_, __, msg: Message) -> bool:
    import re
    return bool(re.search(YT_PATTERN, msg.text or ""))

yt_filter = filters.create(yt_url)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("start") & owner_filter)
async def cmd_start(client: Client, message: Message):
    await message.reply(
        f"❖ **𝗬𝗧 𝗕𝗮𝗰𝗸𝘂𝗽 𝗕𝗼𝘁**\n"
        f"{SEP}\n"
        f"Send a YouTube link to begin:\n"
        f"⋄ Single video  ⋄ Playlist  ⋄ Channel\n\n"
        f"**📋 Queue**\n"
        f"`/status` — queue overview\n"
        f"`/tasks` — list tasks\n"
        f"`/cancel <id>` — cancel a task\n"
        f"`/pause` · `/resume` — pause / resume\n"
        f"`/clear` — remove finished tasks\n"
        f"`/resetqueue` — cancel all active\n"
        f"`/setparallel <1-5>` — parallel workers\n\n"
        f"**🖥 System**\n"
        f"`/diskspace` — disk usage\n"
        f"`/serverinfo` — CPU, RAM, uptime\n"
        f"`/logs` — last 40 log lines\n"
        f"`/purge <n>` — delete last N messages\n\n"
        f"**📍 Destination**\n"
        f"`/setchannel [chat_id]` — set upload destination\n"
        f"`/channels` — saved destinations, tap to switch\n"
        f"`/destinfo` — current destination\n\n"
        f"**🔐 Auth**\n"
        f"`/cookies` — upload cookies.txt\n"
        f"`/authstatus` — auth state\n"
        f"`/ytdlpupdate` — update yt-dlp",
        reply_markup=kb_start(),
    )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("status") & owner_filter)
async def cmd_status(client: Client, message: Message):
    c      = state.counts()
    paused = state.settings.get("paused", False)
    pq     = state.settings["parallel_downloads"]
    total  = c["total"]
    done   = c["completed"] + c["failed"] + c["skipped"] + c["cancelled"]

    dot    = "⏸" if paused else "🟢"
    stat   = "𝗣𝗔𝗨𝗦𝗘𝗗" if paused else "𝗥𝗨𝗡𝗡𝗜𝗡𝗚"

    if total > 0:
        from utils.helpers import styled_progress_bar
        pct  = int(done * 100 / total)
        bar  = styled_progress_bar(done, total, 14)
        summary = f"`{bar}` **{pct}%**  `{done}/{total}`"
    else:
        summary = "_Queue is empty_"

    text = (
        f"❖ **𝗤𝘂𝗲𝘂𝗲 𝗦𝘁𝗮𝘁𝘂𝘀**\n"
        f"{SEP}\n"
        f"{dot} {stat}  |  ⚡ {pq} workers\n\n"
        f"{summary}\n\n"
        f"✅ `{c['completed']}`  ❌ `{c['failed']}`  ⏳ `{c['pending']}`\n"
        f"⬇ `{c['downloading']}`  📤 `{c['uploading']}`  🚫 `{c['cancelled']}`\n"
        f"{SEP}"
    )
    await message.reply(text, reply_markup=kb_processing())


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("tasks") & owner_filter)
async def cmd_tasks(client: Client, message: Message):
    tasks = state.all_tasks()
    active_tasks = [t for t in tasks if t.status in ("pending", "downloading", "downloaded", "uploading")]
    other_tasks  = [t for t in tasks if t.status not in ("pending", "downloading", "downloaded", "uploading")]
    ordered      = active_tasks + other_tasks

    if not ordered:
        await message.reply("_Queue is empty._")
        return

    total = len(ordered)
    text  = (
        f"❖ **𝗧𝗮𝘀𝗸 𝗟𝗶𝘀𝘁**\n"
        f"{SEP}\n"
        f"⋄ Active: `{len(active_tasks)}`  |  Total: `{total}`\n"
        f"_Tap ❌ to cancel a task_"
    )
    await message.reply(text, reply_markup=kb_tasks_page(ordered, page=0))


# ---------------------------------------------------------------------------
# /diskspace
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("diskspace") & owner_filter)
async def cmd_diskspace(client: Client, message: Message):
    await message.reply(sys_disk_report())


# ---------------------------------------------------------------------------
# /serverinfo
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("serverinfo") & owner_filter)
async def cmd_serverinfo(client: Client, message: Message):
    await message.reply(sys_server_report())


# ---------------------------------------------------------------------------
# /logs
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("logs") & owner_filter)
async def cmd_logs(client: Client, message: Message):
    args         = message.text.split(maxsplit=1)
    level_filter = None
    if len(args) > 1:
        lvl = args[1].strip().upper()
        if lvl in ("ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
            level_filter = lvl if lvl != "WARN" else "WARNING"

    log_text = tail_log(Config.DATA_DIR, lines=40, level=level_filter)
    label    = f" (filter: {level_filter})" if level_filter else ""

    if len(log_text) <= 3800:
        await message.reply(f"**Logs**{label}\n```\n{log_text}\n```")
    else:
        buf      = io.BytesIO(log_text.encode("utf-8"))
        buf.name = "logs.txt"
        await message.reply_document(buf, caption=f"Logs{label}")


# ---------------------------------------------------------------------------
# /cookies
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("cookies") & owner_filter)
async def cmd_cookies(client: Client, message: Message):
    await message.reply(
        f"❖ **𝗨𝗽𝗹𝗼𝗮𝗱 𝗖𝗼𝗼𝗸𝗶𝗲𝘀**\n"
        f"{SEP}\n"
        f"1. Install _Get cookies.txt LOCALLY_ extension\n"
        f"2. Open YouTube while logged in\n"
        f"3. Export `cookies.txt` from the extension\n"
        f"4. **Reply to this message** with the file\n\n"
        f"⚠️ File must be named `cookies.txt`"
    )


@Client.on_message(filters.document & owner_filter & filters.reply)
async def on_cookies_upload(client: Client, message: Message):
    if not message.reply_to_message:
        return

    reply_text = message.reply_to_message.text or ""
    if "Upload" not in reply_text and "cookies" not in reply_text and "𝗖𝗼𝗼𝗸𝗶𝗲𝘀" not in reply_text:
        return

    doc = message.document
    if not doc or not doc.file_name:
        return
    if not doc.file_name.endswith(".txt"):
        await message.reply("❌ Please upload a `.txt` file.")
        return

    cookies_path = Config.DATA_DIR / "cookies.txt"
    try:
        await client.download_media(message, file_name=str(cookies_path))
    except Exception as exc:
        await message.reply(f"❌ Failed to save file: `{exc}`")
        return

    first_line = ""
    try:
        first_line = cookies_path.read_text(encoding="utf-8").split("\n")[0].strip()
    except Exception:
        pass

    if not first_line.startswith("# Netscape HTTP Cookie File"):
        await message.reply("⚠️ File doesn't look like a valid cookies.txt.")

    Config.COOKIES_PATH = str(cookies_path)
    reset_bot_alert()

    await message.reply(
        f"✅ **𝗖𝗼𝗼𝗸𝗶𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱**\n"
        f"{SEP}\n"
        f"⋄ Size: `{cookies_path.stat().st_size:,}` bytes\n"
        f"⋄ Run `/authstatus` to verify"
    )
    logger.info("Cookies uploaded, saved to %s", cookies_path)


# ---------------------------------------------------------------------------
# /authstatus
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("authstatus") & owner_filter)
async def cmd_authstatus(client: Client, message: Message):
    s         = auth_status()
    help_text = bot_detection_help() if "No auth" in s else ""
    await message.reply(f"**Auth Status**\n\n{s}\n\n{help_text}".strip())


# ---------------------------------------------------------------------------
# /ytdlpupdate
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("ytdlpupdate") & owner_filter)
async def cmd_ytdlpupdate(client: Client, message: Message):
    import subprocess
    import sys
    wait_msg = await message.reply("⏳ Updating yt-dlp…")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            import importlib
            import yt_dlp
            importlib.reload(yt_dlp)
            version = getattr(yt_dlp, "__version__", "unknown")
            await wait_msg.edit(f"✅ **yt-dlp updated** → `{version}`")
            logger.info("yt-dlp updated to %s", version)
        else:
            await wait_msg.edit(f"❌ Update failed:\n```\n{result.stderr[-300:]}\n```")
    except Exception as exc:
        await wait_msg.edit(f"❌ Error: `{exc}`")


# ---------------------------------------------------------------------------
# /purge
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("purge") & owner_filter)
async def cmd_purge(client: Client, message: Message):
    args = message.text.split()
    n    = 100
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            await message.reply("❌ Usage: `/purge 100`")
            return

    n = min(max(n, 1), 500)
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    await message.reply(
        f"🗑 Delete last **{n}** messages?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Yes", callback_data=f"purge_confirm_{n}"),
            InlineKeyboardButton("✕ Cancel", callback_data="confirm_no"),
        ]]),
    )


# ---------------------------------------------------------------------------
# /resetqueue
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("resetqueue") & owner_filter)
async def cmd_resetqueue(client: Client, message: Message):
    counts = state.counts()
    total  = counts["total"]
    if total == 0:
        await message.reply("_Queue is already empty._")
        return
    active = counts["pending"] + counts["downloading"] + counts["downloaded"] + counts["uploading"]
    await message.reply(
        f"⚠️ Cancel all **{active}** active tasks?\n_(failed/completed stay in log)_",
        reply_markup=kb_confirm("resetqueue"),
    )


# ---------------------------------------------------------------------------
# /pause / /resume
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("pause") & owner_filter)
async def cmd_pause(client: Client, message: Message):
    state.settings["paused"] = True
    state.mark_dirty()
    await message.reply("⏸ **Paused** — `/resume` to continue.")


@Client.on_message(filters.command("resume") & owner_filter)
async def cmd_resume(client: Client, message: Message):
    state.settings["paused"] = False
    state.mark_dirty()
    await message.reply("▶️ **Resumed.**")


# ---------------------------------------------------------------------------
# /cancel <video_id | url>
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("cancel") & owner_filter)
async def cmd_cancel(client: Client, message: Message):
    args   = message.text.split(maxsplit=1)
    target = args[1].strip() if len(args) > 1 else ""

    if not target and message.reply_to_message:
        target = message.reply_to_message.text or ""

    vid = parse_video_id(target) or (target.strip() if len(target) <= 16 else "")
    if not vid:
        await message.reply("❌ Provide a video URL or ID.\nExample: `/cancel dQw4w9WgXcQ`")
        return

    task = state.get(vid)
    if not task:
        await message.reply(f"❌ No task found for `{vid}`.")
        return

    trigger_cancel(vid)
    removed = state.cancel_and_remove(vid)
    await message.reply(f"🚫 Cancelled: `{short((removed or task).title, 40)}`")


# ---------------------------------------------------------------------------
# /setparallel
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("setparallel") & owner_filter)
async def cmd_setparallel(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply(f"Usage: `/setparallel 3` (current: `{state.settings['parallel_downloads']}`)")
        return
    try:
        n = int(args[1])
    except ValueError:
        await message.reply("❌ Provide a number 1–5.")
        return
    if n < 1 or n > 5:
        await message.reply("❌ Must be 1–5.")
        return
    state.settings["parallel_downloads"] = n
    state.mark_dirty()
    await message.reply(f"⚡ Parallel downloads → `{n}`")


# ---------------------------------------------------------------------------
# /setchannel  /setgroup
# ---------------------------------------------------------------------------

# chat_id (where the command was issued) -> pending destination awaiting confirm
_pending_dest: dict = {}


def _apply_destination(chat_id: int, title: str, ctype: str = "") -> None:
    """Switch the live upload destination and remember it in history."""
    Config.DEST_CHAT_ID = chat_id

    state.settings["dest_chat_id"]    = chat_id
    state.settings["dest_chat_title"] = title

    history: list = state.settings.setdefault("dest_history", [])
    history[:] = [h for h in history if h.get("id") != chat_id]
    history.insert(0, {"id": chat_id, "title": title, "type": ctype})
    del history[10:]  # keep last 10

    state.mark_dirty()
    logger.info("Dest chat set to %d (%s)", chat_id, title)


@Client.on_message(filters.command(["setchannel", "setgroup"]) & owner_filter)
async def cmd_setchannel(client: Client, message: Message):
    """Set the upload destination — current chat, or a chat_id passed as argument."""
    args = message.text.split(maxsplit=1)
    target_id = message.chat.id
    if len(args) > 1:
        raw = args[1].strip()
        try:
            target_id = int(raw)
        except ValueError:
            await message.reply(
                "❌ Provide a numeric chat ID, e.g. `/setchannel -1001234567890`\n"
                "_Or run `/setchannel` with no arguments inside the target chat._"
            )
            return

    wait_msg = await message.reply("⏳ _Checking access…_")

    try:
        chat = await client.get_chat(target_id)
    except Exception as exc:
        await wait_msg.edit(
            f"❌ Can't access `{target_id}`: `{exc}`\n"
            f"Make sure the bot is a member/admin of that chat."
        )
        return

    try:
        member = await client.get_chat_member(target_id, "me")
        status = str(member.status).replace("ChatMemberStatus.", "")
    except Exception:
        status = "unknown"

    if status in ("BANNED", "LEFT"):
        await wait_msg.edit(f"❌ Bot is not a member of **{chat.title or target_id}** (status: `{status}`).")
        return

    title = getattr(chat, "title", None) or "Private Chat"
    ctype = str(getattr(chat, "type", "")).replace("ChatType.", "")

    _pending_dest[message.chat.id] = {"id": target_id, "title": title, "type": ctype}

    await wait_msg.edit(
        f"📍 **𝗦𝘄𝗶𝘁𝗰𝗵 𝗨𝗽𝗹𝗼𝗮𝗱 𝗗𝗲𝘀𝘁𝗶𝗻𝗮𝘁𝗶𝗼𝗻?**\n"
        f"{SEP}\n"
        f"⋄ 𝗡𝗮𝗺𝗲: **{title}**\n"
        f"⋄ 𝗜𝗗: `{target_id}`\n"
        f"⋄ 𝗧𝘆𝗽𝗲: `{ctype}`\n"
        f"⋄ 𝗕𝗼𝘁 𝘀𝘁𝗮𝘁𝘂𝘀: `{status}`\n"
        f"{SEP}\n"
        f"All future uploads will go here.",
        reply_markup=kb_confirm("setdest"),
    )


# ---------------------------------------------------------------------------
# /channels — saved destinations, tap to switch instantly
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("channels") & owner_filter)
async def cmd_channels(client: Client, message: Message):
    history = state.settings.get("dest_history", [])
    if not history:
        await message.reply(
            "_No saved destinations yet._\n"
            "Use `/setchannel` in a chat (or `/setchannel <chat_id>`) to add one."
        )
        return
    await message.reply(
        f"❖ **𝗦𝗮𝘃𝗲𝗱 𝗗𝗲𝘀𝘁𝗶𝗻𝗮𝘁𝗶𝗼𝗻𝘀**\n"
        f"{SEP}\n"
        f"_Tap a destination to switch instantly_",
        reply_markup=kb_channels(history, Config.DEST_CHAT_ID),
    )


# ---------------------------------------------------------------------------
# /destinfo — show current destination
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("destinfo") & owner_filter)
async def cmd_destinfo(client: Client, message: Message):
    dest_id    = Config.DEST_CHAT_ID
    dest_title = state.settings.get("dest_chat_title", "—")
    await message.reply(
        f"📍 **Current Destination**\n"
        f"{SEP}\n"
        f"⋄ 𝗜𝗗: `{dest_id}`\n"
        f"⋄ 𝗡𝗮𝗺𝗲: {dest_title}\n"
        f"Use `/setchannel` to change it, or `/channels` to pick from saved ones."
    )


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("clear") & owner_filter)
async def cmd_clear(client: Client, message: Message):
    c        = state.counts()
    finished = c.get("completed", 0) + c.get("failed", 0) + c.get("cancelled", 0) + c.get("skipped", 0)
    if finished == 0:
        await message.reply("_No finished tasks to clear._")
        return
    await message.reply(
        f"🗑 Clear `{finished}` finished tasks?",
        reply_markup=kb_confirm("clear"),
    )


# ---------------------------------------------------------------------------
# YouTube URL handler
# ---------------------------------------------------------------------------

_scanned_items_cache: dict = {}

# Per-chat scan lock — prevents double-trigger when user sends URL rapidly
_scan_locks:     dict = {}   # chat_id -> asyncio.Lock
_scan_in_flight: dict = {}   # chat_id -> bool


def _get_scan_lock(chat_id: int):
    if chat_id not in _scan_locks:
        _scan_locks[chat_id] = asyncio.Lock()
    return _scan_locks[chat_id]


def _scan_metadata_caption(
    meta: dict,
    kind: str,
    filename: str,
    item_count: int,
    channel: str = "",
    total_secs: int = 0,
) -> str:
    """Rich Telegram caption for the downloadable scan TXT."""
    meta = meta or {}
    icon = "📋" if kind == "playlist" else ("📺" if kind == "channel" else "📹")
    ch_name  = channel or meta.get("channel_url", "") or "Unknown"
    verified = " ✔" if meta.get("verified") else ""
    subs     = meta.get("subscribers") or "—"
    pl_title = meta.get("playlist_title", "") or ch_name
    ch_url   = meta.get("channel_url", "")

    h, r = divmod(total_secs, 3600); m, s = divmod(r, 60)
    dur_s = f"{h}h {m}m" if h else (f"{m}m {s}s" if total_secs else "—")

    lines = [
        f"{icon} **{pl_title}**{verified}",
        SEP,
        f"⋄ 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: **{ch_name}**",
        f"⋄ 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗯𝗲𝗿𝘀: `{subs}`",
        f"⋄ 𝗩𝗶𝗱𝗲𝗼𝘀: `{item_count}`",
        f"⋄ 𝗧𝗼𝘁𝗮𝗹 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: `{dur_s}`",
    ]
    if ch_url:
        lines.append(f"⋄ 𝗨𝗥𝗟: {ch_url}")
    lines += [SEP, f"📄 `{filename}`"]
    return "\n".join(lines)


@Client.on_message(filters.regex(YT_PATTERN) & owner_filter)
async def on_youtube_url(client: Client, message: Message):
    url     = message.text.strip().split()[0]
    kind    = classify_url(url)
    chat_id = message.chat.id

    # ── Deduplication: skip if this chat is already scanning ───────────────
    lock = _get_scan_lock(chat_id)
    if _scan_in_flight.get(chat_id):
        await message.reply("⏳ _Already scanning, please wait…_")
        return
    _scan_in_flight[chat_id] = True
    try:
        async with lock:
            logger.info("Received %s URL: %s", kind, url)
            if kind == "video":
                await _handle_video(client, message, url)
            else:
                await _handle_scan(client, message, url, kind)
    finally:
        _scan_in_flight[chat_id] = False


async def _handle_video(client: Client, message: Message, url: str):
    """Single video: fetch info, show quality selector, then add to queue on Start."""
    vid = parse_video_id(url)
    if not vid:
        await message.reply("❌ Could not extract video ID.")
        return

    status_msg = await message.reply("⏳ _Fetching video info…_")

    try:
        result = await scan(url)
    except Exception as exc:
        await status_msg.edit(f"❌ Scan failed: `{exc}`")
        return

    if not result["items"]:
        await status_msg.edit("❌ Could not fetch video info.")
        return

    item    = result["items"][0]
    title   = item.get("title", "Unknown")
    dur     = item.get("duration", "—")
    channel = result.get("channel") or "—"

    # Send a downloadable TXT with full metadata header.
    try:
        meta     = result.get("meta", {})
        itms     = result["items"]
        txt_path = generate_txt(itms, channel, "video", meta)
        await client.send_document(
            chat_id=message.chat.id,
            document=str(txt_path),
            caption=_scan_metadata_caption(
                meta, "video", txt_path.name, len(itms), channel=channel
            ),
        )
    except Exception as exc:
        logger.warning("Could not send video TXT listing: %s", exc)

    # Cache for action_start
    _scanned_items_cache[message.chat.id] = {
        "items":   result["items"],
        "channel": channel,
        "kind":    "video",
        "meta":    result.get("meta", {}),
    }

    current_q = state.settings.get("quality", "best")
    q_label   = quality_label(current_q)

    await status_msg.edit(
        f"📹 **{title}**\n"
        f"{SEP}\n"
        f"⋄ 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: {channel}\n"
        f"⋄ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: `{dur}`\n"
        f"⋄ 𝗤𝘂𝗮𝗹𝗶𝘁𝘆: `{q_label}`\n"
        f"{SEP}\n"
        f"_Select quality or tap Start_",
        reply_markup=kb_video(),
    )


async def _handle_scan(client: Client, message: Message, url: str, kind: str):
    """Channel/playlist: scan and present sort/quality options."""
    status_msg = await message.reply(f"⏳ _Scanning {kind}…_")

    try:
        result = await scan(url)
    except Exception as exc:
        await status_msg.edit(f"❌ Scan failed: `{exc}`")
        return

    items   = result["items"]
    channel = result["channel"]
    meta    = result.get("meta", {})

    if not items:
        await status_msg.edit("❌ No videos found.")
        return

    # Send TXT immediately with full metadata in caption.
    try:
        txt_path = generate_txt(items, channel, kind, meta)
        # Compute total duration for caption
        _secs = 0
        for _itm in items:
            _p = str(_itm.get("duration", "")).split(":")
            try:
                if len(_p) == 3:   _secs += int(_p[0])*3600+int(_p[1])*60+int(_p[2])
                elif len(_p) == 2: _secs += int(_p[0])*60+int(_p[1])
            except ValueError: pass
        await client.send_document(
            chat_id=message.chat.id,
            document=str(txt_path),
            caption=_scan_metadata_caption(
                meta, kind, txt_path.name, len(items),
                channel=channel, total_secs=_secs
            ),
        )
    except Exception as exc:
        logger.warning("Could not send scan TXT listing: %s", exc)

    _scanned_items_cache[message.chat.id] = {
        "items":   items,
        "channel": channel,
        "kind":    kind,
        "meta":    meta,
    }

    icon      = "📋" if kind == "playlist" else "📺"
    current_q = state.settings.get("quality", "best")
    q_label   = quality_label(current_q)

    await status_msg.edit(
        f"{icon} **{channel or kind.title()}**\n"
        f"{SEP}\n"
        f"⋄ Videos: `{len(items)}`\n"
        f"⋄ Quality: `{q_label}`\n"
        f"{SEP}\n"
        f"_Pick sort order & quality, then Start_",
        reply_markup=kb_sort(),
    )


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------

@Client.on_callback_query(owner_filter)
async def on_callback(client: Client, cq: CallbackQuery):
    data    = cq.data
    chat_id = cq.message.chat.id

    # --- noop ---
    if data == "noop":
        await cq.answer()
        return

    # --- Sort ---
    if data.startswith("sort_"):
        order  = data.replace("sort_", "")
        cached = _scanned_items_cache.get(chat_id)
        if not cached:
            await cq.answer("Session expired. Resend the URL.", show_alert=True)
            return
        cached["items"] = sort_items(cached["items"], order)
        state.settings["sort_order"] = order
        state.mark_dirty()
        label = order.replace("_", " → ")
        await cq.answer(f"Sorted: {label}")
        try:
            await cq.message.edit_reply_markup(kb_sort())
        except Exception:
            pass

    # --- Quality menu ---
    elif data == "quality_menu":
        await cq.message.edit_reply_markup(kb_quality(state.settings.get("quality", "best")))

    elif data.startswith("quality_"):
        q = data.replace("quality_", "")
        if q == "back":
            # Go back to correct keyboard depending on cached kind
            cached = _scanned_items_cache.get(chat_id)
            kind   = (cached or {}).get("kind", "playlist")
            if kind == "video":
                await cq.message.edit_reply_markup(kb_video())
            else:
                await cq.message.edit_reply_markup(kb_sort())
            return
        state.settings["quality"] = q
        state.mark_dirty()
        await cq.answer(f"Quality: {quality_label(q)}")
        await cq.message.edit_reply_markup(kb_quality(q))

    # --- Actions ---
    elif data.startswith("action_"):
        action = data.replace("action_", "")

        if action == "start":
            cached = _scanned_items_cache.get(chat_id)
            if not cached:
                await cq.answer("Session expired. Resend the URL.", show_alert=True)
                return
            quality        = state.settings.get("quality", "best")
            added, _ = state.add_tasks(cached["items"], source=cached["kind"], quality=quality)
            _scanned_items_cache.pop(chat_id, None)

            q_label = quality_label(quality)
            kind    = cached.get("kind", "video")
            icon    = "📹" if kind == "video" else ("📋" if kind == "playlist" else "📺")

            try:
                await cq.message.edit_reply_markup(kb_processing())
            except Exception:
                pass
            await cq.answer(
                f"{icon} {added} added to queue",
                show_alert=(added == 0),
            )

        elif action == "pause":
            state.settings["paused"] = True
            state.mark_dirty()
            await cq.answer("⏸ Paused")

        elif action == "resume":
            state.settings["paused"] = False
            state.mark_dirty()
            await cq.answer("▶️ Resumed")

        elif action == "cancel":
            # Discard cached items (used when user taps Discard on scan result)
            _scanned_items_cache.pop(chat_id, None)
            removed = 0
            for t in list(state.all_tasks()):
                if t.status in (PENDING, "downloading", "downloaded", "uploading"):
                    trigger_cancel(t.id)
                    state.cancel_and_remove(t.id)
                    removed += 1
            if removed:
                await cq.answer(f"🚫 {removed} tasks removed", show_alert=True)
            else:
                await cq.answer("✕ Discarded")

        elif action == "status":
            c = state.counts()
            await cq.answer(
                f"✅{c['completed']} ❌{c['failed']} ⏳{c['pending']} ⬇{c['downloading']} 📤{c['uploading']}",
                show_alert=True,
            )

        elif action == "tasks":
            tasks    = state.all_tasks()
            active_t = [t for t in tasks if t.status in ("pending", "downloading", "downloaded", "uploading")]
            other_t  = [t for t in tasks if t.status not in ("pending", "downloading", "downloaded", "uploading")]
            ordered  = active_t + other_t
            if not ordered:
                await cq.answer("Queue is empty", show_alert=True)
                return
            text = (
                f"❖ **𝗧𝗮𝘀𝗸 𝗟𝗶𝘀𝘁**\n"
                f"{SEP}\n"
                f"⋄ Active: `{len(active_t)}`  |  Total: `{len(ordered)}`\n"
                f"_Tap ❌ to cancel_"
            )
            await cq.message.reply(text, reply_markup=kb_tasks_page(ordered, page=0))
            await cq.answer()

        elif action == "queue":
            tasks = state.all_tasks()
            text  = "\n".join(f"`{t.id[:8]}` {short(t.title, 28)} [{t.status}]" for t in tasks[:20])
            await cq.answer(text[:200] or "Empty queue", show_alert=True)

        elif action == "diskspace":
            await cq.answer(sys_disk_report()[:200], show_alert=True)

        elif action == "serverinfo":
            await cq.answer(sys_server_report()[:200], show_alert=True)

        elif action == "channels":
            history = state.settings.get("dest_history", [])
            if not history:
                await cq.answer("No saved destinations yet. Use /setchannel", show_alert=True)
                return
            await cq.message.reply(
                f"❖ **𝗦𝗮𝘃𝗲𝗱 𝗗𝗲𝘀𝘁𝗶𝗻𝗮𝘁𝗶𝗼𝗻𝘀**\n"
                f"{SEP}\n"
                f"_Tap a destination to switch instantly_",
                reply_markup=kb_channels(history, Config.DEST_CHAT_ID),
            )
            await cq.answer()

    # --- Switch destination from saved history ---
    elif data.startswith("switch_dest_"):
        raw = data.replace("switch_dest_", "")
        try:
            target_id = int(raw)
        except ValueError:
            await cq.answer("Invalid entry", show_alert=True)
            return

        entry = next((h for h in state.settings.get("dest_history", []) if h.get("id") == target_id), None)
        if not entry:
            await cq.answer("Not found in history", show_alert=True)
            return

        try:
            await client.get_chat(target_id)
        except Exception as exc:
            await cq.answer(f"Can't access chat: {exc}", show_alert=True)
            return

        _apply_destination(target_id, entry.get("title", str(target_id)), entry.get("type", ""))
        try:
            await cq.message.edit_reply_markup(kb_channels(state.settings.get("dest_history", []), target_id))
        except Exception:
            pass
        await cq.answer(f"Switched to {entry.get('title', target_id)}")

    # --- Per-task cancel ---
    elif data.startswith("cancel_task_"):
        vid   = data.replace("cancel_task_", "")
        task  = state.get(vid)
        title = short((task.title if task else None) or vid, 30)
        trigger_cancel(vid)
        removed = state.cancel_and_remove(vid)
        if removed:
            await cq.answer(f"🚫 Removed: {title}", show_alert=True)
        else:
            await cq.answer("Task not found", show_alert=True)

        tasks    = state.all_tasks()
        active_t = [t for t in tasks if t.status in ("pending", "downloading", "downloaded", "uploading")]
        other_t  = [t for t in tasks if t.status not in ("pending", "downloading", "downloaded", "uploading")]
        ordered  = active_t + other_t
        if ordered:
            try:
                await cq.message.edit_reply_markup(kb_tasks_page(ordered, page=0))
            except Exception:
                pass
        else:
            try:
                await cq.message.edit("_Queue is empty._")
            except Exception:
                pass

    # --- Task info ---
    elif data.startswith("task_info_"):
        vid  = data.replace("task_info_", "")
        task = state.get(vid)
        if task:
            dur = f" · {task.duration}" if task.duration and task.duration != "?" else ""
            err = f"\n⚠️ `{task.error[:100]}`" if task.error else ""
            await cq.answer(
                f"{short(task.title, 40)}{dur}\nStatus: {task.status}{err}",
                show_alert=True,
            )
        else:
            await cq.answer("Task not found", show_alert=True)

    # --- Tasks pagination ---
    elif data.startswith("tasks_page_"):
        page     = int(data.replace("tasks_page_", ""))
        tasks    = state.all_tasks()
        active_t = [t for t in tasks if t.status in ("pending", "downloading", "downloaded", "uploading")]
        other_t  = [t for t in tasks if t.status not in ("pending", "downloading", "downloaded", "uploading")]
        ordered  = active_t + other_t
        try:
            await cq.message.edit_reply_markup(kb_tasks_page(ordered, page=page))
        except Exception:
            pass
        await cq.answer()

    # --- Tasks close ---
    elif data == "tasks_close":
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.answer()

    # --- Purge confirm ---
    elif data.startswith("purge_confirm_"):
        n = int(data.replace("purge_confirm_", ""))
        await cq.message.delete()

        target      = Config.DEST_CHAT_ID
        report_chat = chat_id

        # Use tracked message IDs (bots cannot call get_chat_history)
        msg_ids = state.get_dest_msgs(n)
        if not msg_ids:
            await client.send_message(
                report_chat,
                "⚠️ No tracked messages found.\n"
                "_Bot tracks message IDs as it uploads — run a few uploads first._"
            )
            await cq.answer()
            return

        status = await client.send_message(
            report_chat, f"🗑 Deleting **{len(msg_ids)}** tracked messages from dest…"
        )

        deleted = 0
        for i in range(0, len(msg_ids), 100):
            batch = msg_ids[i:i + 100]
            try:
                await client.delete_messages(target, batch)
                deleted += len(batch)
            except Exception as exc:
                logger.warning("Purge batch %d failed: %s", i // 100, exc)

        try:
            confirm = await status.edit(f"✅ Purged **{deleted}** messages from dest chat.")
            await asyncio.sleep(4)
            await confirm.delete()
        except Exception:
            pass
        await cq.answer()

    # --- Confirmations ---
    elif data.startswith("confirm_"):
        action = data.replace("confirm_", "")

        if action == "clear":
            removed = state.reset_finished()
            await cq.message.edit(f"✅ Cleared `{removed}` finished tasks.")
            await cq.answer()

        elif action == "resetqueue":
            removed = 0
            for t in list(state.all_tasks()):
                if t.status in (PENDING, "downloading", "downloaded", "uploading"):
                    trigger_cancel(t.id)
                    state.cancel_and_remove(t.id)
                    removed += 1
            await cq.message.edit(f"🚫 `{removed}` tasks removed from queue.")
            await cq.answer("Done!")

        elif action == "setdest":
            pending = _pending_dest.pop(chat_id, None)
            if not pending:
                await cq.message.edit("⚠️ This confirmation expired. Run `/setchannel` again.")
                await cq.answer()
                return
            _apply_destination(pending["id"], pending["title"], pending.get("type", ""))
            await cq.message.edit(
                f"✅ **𝗗𝗲𝘀𝘁𝗶𝗻𝗮𝘁𝗶𝗼𝗻 𝗦𝗲𝘁!**\n"
                f"{SEP}\n"
                f"⋄ 𝗡𝗮𝗺𝗲: **{pending['title']}**\n"
                f"⋄ 𝗜𝗗: `{pending['id']}`\n"
                f"{SEP}\n"
                f"All uploads will now go to this chat.\n"
                f"_Setting saved — survives restarts._"
            )
            await cq.answer("Destination updated")

        elif action == "no":
            _pending_dest.pop(chat_id, None)
            await cq.message.delete()
            await cq.answer()

    else:
        await cq.answer()
