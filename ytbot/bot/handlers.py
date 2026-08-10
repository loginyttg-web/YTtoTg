"""
Message and callback-query handlers for the bot.

Access model
------------
👑 owner : Config.OWNER_ID — everything (users, settings, watches, queue)
🛡 admin : manage watches + queue (add/remove watch, pause, cancel, quality…)
👤 user  : submit YouTube links, view status/dashboard/stats
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import Config, quality_label
from core.scraper import scan, sort_items, generate_txt, date_range_of, total_duration_secs
from core.state import (
    StateManager, PENDING, ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_ICON,
)
from core.system import (
    disk_report as sys_disk_report,
    server_report as sys_server_report,
    run_speedtest,
)
from core.auth import (
    MAX_COOKIE_FILE_BYTES,
    auth_status,
    configured_cookie_path,
    install_cookies_file,
    is_bot_detection_error,
    probe_youtube_access,
)
from core.downloader import (
    clear_youtube_cooldown,
    register_youtube_block,
    reset_bot_alert,
    trigger_cancel,
    youtube_cooldown_remaining,
)
from core.watcher import check_watch
from utils.helpers import (
    human_bytes, human_time, human_time_short, short, md_escape,
    parse_video_id, classify_url, styled_progress_bar, bar_smooth, SEP,
)
from utils.logger import tail_log
from bot.keyboards import (
    kb_sort, kb_quality, kb_processing, kb_confirm, kb_start,
    kb_tasks_page, kb_video, kb_channels,
    kb_watch_actions, kb_watchlist, kb_users, kb_caption, kb_auth,
)

logger = logging.getLogger("handlers")

state: Optional[StateManager] = None
stop_event: Optional[asyncio.Event] = None


def setup(state_mgr: StateManager, evt: asyncio.Event) -> None:
    global state, stop_event
    state = state_mgr
    stop_event = evt


# ---------------------------------------------------------------------------
# Roles & filters
# ---------------------------------------------------------------------------

def _role_of_user(user_id: int) -> Optional[str]:
    if state is None:
        return None
    return state.role_of(user_id)


def _is_owner_channel_post(update) -> bool:
    """Channel post in the configured destination channel → owner context."""
    if getattr(update, "from_user", None) is not None:
        return False
    sender = getattr(update, "sender_chat", None)
    return sender is not None and abs(sender.id) == abs(Config.DEST_CHAT_ID)


def owner_only(_, __, update) -> bool:
    fu = getattr(update, "from_user", None)
    if fu is not None:
        return fu.id == Config.OWNER_ID
    return _is_owner_channel_post(update)


def admin_only(_, __, update) -> bool:
    fu = getattr(update, "from_user", None)
    if fu is not None:
        return _role_of_user(fu.id) in (ROLE_OWNER, ROLE_ADMIN)
    return _is_owner_channel_post(update)


def user_only(_, __, update) -> bool:
    fu = getattr(update, "from_user", None)
    if fu is not None:
        return _role_of_user(fu.id) is not None
    return _is_owner_channel_post(update)


owner_filter = filters.create(owner_only)
admin_filter = filters.create(admin_only)
user_filter  = filters.create(user_only)

YT_PATTERN = r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|[a-zA-Z0-9_-]{11}|@[a-zA-Z0-9_.-]+|playlist\?list=|channel/|c/|shorts/)"

def yt_url(_, __, msg: Message) -> bool:
    return bool(re.search(YT_PATTERN, msg.text or ""))

def not_a_command(_, __, msg: Message) -> bool:
    """True when the message does NOT start with a slash command.

    Keeps things like `/watch <url>` or `/cancel <url>` from being treated as
    raw YouTube links when the sender lacks permission for that command.
    """
    text = (msg.text or msg.caption) or ""
    return not text.lstrip().startswith("/")

yt_filter = filters.create(yt_url) & filters.create(not_a_command)

ACTIVE_STATUSES = ("pending", "downloading", "downloaded", "uploading")


def _ordered_tasks():
    """All tasks, active first (in queue order)."""
    tasks = state.all_tasks()
    active = [t for t in tasks if t.status in ACTIVE_STATUSES]
    other  = [t for t in tasks if t.status not in ACTIVE_STATUSES]
    return active, other


async def _resolve_user_arg(client: Client, token: str):
    """Resolve a user id / @username token to a pyrogram User, or None."""
    try:
        users = await client.get_users(token)
        return users[0] if isinstance(users, list) else users
    except Exception as exc:
        logger.warning("Could not resolve user %r: %s", token, exc)
        return None


# ---------------------------------------------------------------------------
# /start  &  /help
# ---------------------------------------------------------------------------

START_TEXT = (
    "❖ **𝗬𝗧 ➜ 𝗧𝗲𝗹𝗲𝗴𝗿𝗮𝗺 𝗕𝗮𝗰𝗸𝘂𝗽 𝗕𝗼𝘁**\n"
    f"{SEP}\n"
    "Send any YouTube link and I'll back it up:\n"
    "🎬 Single video · 📋 Playlist · 📺 Channel\n\n"
    "**👀 Auto-Watch** (admins)\n"
    "`/watch <channel> [dest]` auto-backup new uploads\n"
    "`/watchlist` · `/unwatch <id>` · `/checknow [id]`\n"
    "`/backfill <id>` · `/watchdest <id> <chat>`\n"
    "`/watchpause` · `/watchresume`\n\n"
    "**📥 Queue**\n"
    "`/status` · `/tasks` · `/dashboard` · `/stats`\n"
    "`/cancel <id|url>` · `/pause` · `/resume`\n"
    "`/resetqueue` · `/clear` · `/retryfailed`\n"
    "`/syncfrom <last_video_link>` resume after crash\n\n"
    "**👥 Users** (owner)\n"
    "`/adduser` · `/removeuser` · `/setrole` · `/users`\n"
    "`/whoami` — check your role\n\n"
    "**⚙️ Settings**\n"
    "`/setquality <best|2160|1440|1080|720|480|audio>`\n"
    "`/setparallel <1-5>` downloads · `/setuploaders <1-2>` uploads\n"
    "`/setqlimit <n>` upload queue cap · `/watchinterval <min>`\n"
    "`/setchannel [id]` · `/channels` · `/destinfo`\n"
    "`/caption` upload captions + Uploaded-by signature\n\n"
    "**🖥 System**\n"
    "`/serverinfo` · `/diskspace` · `/speedtest`\n"
    "`/logs [n|level]` · `/purge <n>`\n\n"
    "**🔐 Auth**\n"
    "`/cookies` · `/authstatus` · `/authcheck` · `/ytdlpupdate`"
)

def _home_text(role: str) -> str:
    """Compact control-centre summary; /help keeps the full command list."""
    counts = state.counts()
    active = sum(counts.get(key, 0) for key in ACTIVE_STATUSES)
    watches = state.all_watches()
    watch_on = sum(1 for watch in watches if watch.enabled)
    role_label = {
        ROLE_OWNER: "👑 Owner",
        ROLE_ADMIN: "🛡 Admin",
        ROLE_USER: "👤 User",
    }.get(role, role.title())
    auth_icon = "✅" if "YouTube cookies active" in auth_status() else "⚠️"
    paused = bool(state.settings.get("paused", False))
    engine = "⏸ Paused" if paused else "🟢 Ready"
    return (
        "❖ **𝗬𝗧𝘁𝗼𝗧𝗴 · 𝗖𝗼𝗻𝘁𝗿𝗼𝗹 𝗖𝗲𝗻𝘁𝗲𝗿**\n"
        f"{SEP}\n"
        f"{engine}   ·   {role_label}\n\n"
        f"📥 Queue  `{active}` active  ·  `{counts.get('completed', 0)}` done\n"
        f"👀 Watches  `{watch_on}/{len(watches)}` enabled\n"
        f"🔐 YouTube auth  {auth_icon}\n"
        f"🎞 Quality  {quality_label(state.settings.get('quality', 'best'))}\n"
        f"📍 Destination  `{Config.DEST_CHAT_ID}`\n"
        f"{SEP}\n"
        "Send a **YouTube video, playlist or channel link** to begin.\n"
        "Use the buttons below, or `/help` for every command."
    )


@Client.on_message(filters.command(["start", "help"]))
async def cmd_start(client: Client, message: Message):
    uid  = message.from_user.id if message.from_user else 0
    role = _role_of_user(uid) if uid else None
    if not role:
        await message.reply(
            "🔒 **Access Denied**\n"
            f"{SEP}\n"
            "You're not registered with this bot.\n\n"
            "Ask the owner to add you:\n"
            "`/adduser` — reply to one of your messages\n\n"
            f"Your user ID: `{uid}`"
        )
        return

    command = ((message.command or [""])[0]).casefold()
    text = START_TEXT if command == "help" else _home_text(role)
    await message.reply(text, reply_markup=kb_start())


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("status") & user_filter)
async def cmd_status(client: Client, message: Message):
    c      = state.counts()
    paused = state.settings.get("paused", False)
    pq     = state.settings["parallel_downloads"]
    q      = state.settings.get("quality", "best")
    total  = c["total"]
    done   = c["completed"] + c["failed"] + c["skipped"] + c["cancelled"]

    watches = state.all_watches()
    w_on    = sum(1 for w in watches if w.enabled)

    dot  = "⏸" if paused else "🟢"
    stat = "𝗣𝗔𝗨𝗦𝗘𝗗" if paused else "𝗥𝗨𝗡𝗡𝗜𝗡𝗚"

    if total > 0:
        pct = int(done * 100 / total)
        bar = styled_progress_bar(done, total, 14)
        summary = f"`{bar}` **{pct}%**  `{done}/{total}`"
    else:
        summary = "_Queue is empty — send a YouTube link_"

    watch_line = f"👀 `{w_on}/{len(watches)}` watches active\n" if watches else ""
    iv = int(state.settings.get("watch_interval_min", 0) or Config.WATCH_INTERVAL_MIN)

    qlim  = int(state.settings.get("upload_queue_limit", Config.UPLOAD_QUEUE_LIMIT))
    ready = state.upload_queue_size()

    text = (
        f"❖ **𝗤𝘂𝗲𝘂𝗲 𝗦𝘁𝗮𝘁𝘂𝘀**\n"
        f"{SEP}\n"
        f"{dot} {stat}   ⚡ ⬇`{pq}` ⬆`{Config.UPLOAD_WORKERS}` workers\n\n"
        f"{summary}\n\n"
        f"✅ `{c['completed']}`   ❌ `{c['failed']}`   ⏳ `{c['pending']}`\n"
        f"⬇️ `{c['downloading']}`   📤 `{c['uploading']}`   🚫 `{c['cancelled']}`\n\n"
        f"📦 Upload queue: `{ready}/{qlim}` (download backpressure)\n"
        f"{watch_line}🕐 _Watcher auto-checks every {iv}m_\n"
        f"{SEP}"
    )
    await message.reply(text, reply_markup=kb_processing(paused))


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("tasks") & user_filter)
async def cmd_tasks(client: Client, message: Message):
    active_t, other_t = _ordered_tasks()
    ordered = active_t + other_t

    if not ordered:
        await message.reply("_Queue is empty._")
        return

    text = (
        f"❖ **𝗧𝗮𝘀𝗸 𝗟𝗶𝘀𝘁**\n"
        f"{SEP}\n"
        f"⋄ Active: `{len(active_t)}`  ·  Total: `{len(ordered)}`\n"
        f"_Tap ℹ️ for details · ❌ to cancel_"
    )
    await message.reply(text, reply_markup=kb_tasks_page(ordered, page=0))


# ---------------------------------------------------------------------------
# /dashboard — pin the live progress panel in this chat
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("dashboard") & user_filter)
async def cmd_dashboard(client: Client, message: Message):
    from bot import dashboard as dash_mod

    paused = state.settings.get("paused", False)
    text   = dash_mod.format_dashboard(state)
    msg    = await message.reply(text, reply_markup=kb_processing(paused))

    # Retarget the background dashboard loop to this message
    dash_mod.dashboard_msg_id   = msg.id
    dash_mod._dashboard_chat_id = message.chat.id
    dash_mod._last_text         = text


# ---------------------------------------------------------------------------
# /stats — session statistics
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("stats") & user_filter)
async def cmd_stats(client: Client, message: Message):
    await message.reply(_stats_text())


def _stats_text() -> str:
    stats = state.stats
    c     = state.counts()

    up_bytes = stats.get("bytes_uploaded", 0)
    up_time  = stats.get("total_time", 0.0)
    avg_spd  = (up_bytes / up_time) if up_time > 0 else 0

    from bot import dashboard as dash_mod
    run_secs = time.time() - dash_mod._session_start

    bar = bar_smooth(stats.get("completed", 0), max(c["total"], 1), 14)

    watches = state.all_watches()
    w_on    = sum(1 for w in watches if w.enabled)
    known   = sum(len(w.known_ids) for w in watches)

    watch_line = (
        f"⋄ 👀 Watching: `{w_on}/{len(watches)}` channels "
        f"(`{known}` videos tracked)\n"
        if watches else
        "⋄ 👀 Watching: _none — `/watch` a channel_\n"
    )

    return (
        f"❖ **𝗦𝗲𝘀𝘀𝗶𝗼𝗻 𝗦𝘁𝗮𝘁𝘀**\n"
        f"{SEP}\n"
        f"`{bar}`\n\n"
        f"⋄ ✅ Completed: `{stats.get('completed', 0)}`\n"
        f"⋄ ❌ Failed: `{stats.get('failed', 0)}`\n"
        f"⋄ ⏭ Skipped: `{stats.get('skipped', 0)}`\n"
        f"⋄ 📦 Uploaded: `{human_bytes(up_bytes)}`\n"
        f"⋄ ⏱ Upload time: `{human_time(up_time)}`\n"
        f"⋄ ⚡ Avg upload speed: `{human_bytes(avg_spd)}/s`\n"
        f"⋄ 🕐 Bot running: `{human_time(run_secs)}`\n"
        f"{watch_line}"
        f"{SEP}"
    )


# ---------------------------------------------------------------------------
# 👀 WATCH — auto-monitor YouTube channels
# ---------------------------------------------------------------------------

WATCH_USAGE = (
    "❖ **𝗔𝘂𝘁𝗼-𝗪𝗮𝘁𝗰𝗵 𝗮 𝗖𝗵𝗮𝗻𝗻𝗲𝗹**\n"
    f"{SEP}\n"
    "**Usage:**\n"
    "`/watch <channel_url>`\n"
    "`/watch <channel_url> -1001234567890`\n"
    "`/watch <channel_url> all` _(also backup existing)_\n\n"
    "⋄ 📍 No chat ID → uses current global destination\n"
    "⋄ 🆕 After setup, only **new uploads** are auto-backed up\n"
    f"⋄ ⏱ Default check: every `{Config.WATCH_INTERVAL_MIN}` minutes\n\n"
    "**Timing options (after watching):**\n"
    "`/watchtime w1 06:00` — once daily at 6 AM\n"
    "`/watchinterval w1 720` — every 12h · `1440` = 24h\n"
    "`/checknow w1` — one-off instant check"
)


@Client.on_message(filters.command("watch") & admin_filter)
async def cmd_watch(client: Client, message: Message):
    args = message.text.split()[1:]
    if not args:
        await message.reply(WATCH_USAGE)
        return

    url = None
    dest_arg = None
    full = False
    for tok in args:
        tl = tok.lower()
        if tl in ("all", "full"):
            full = True
        elif re.fullmatch(r"-?\d{5,}", tok):
            dest_arg = int(tok)
        elif "youtu" in tl or tl.startswith("http"):
            url = tok

    if not url:
        await message.reply(WATCH_USAGE)
        return

    wait_msg = await message.reply("👀 _Scanning channel… this can take a moment_")

    try:
        result = await scan(url)
    except Exception as exc:
        await wait_msg.edit(f"❌ Scan failed: `{exc}`")
        return

    items = result.get("items", [])
    kind  = result.get("type", "")
    meta  = result.get("meta", {}) or {}
    channel = result.get("channel", "")

    if kind == "video" or len(items) <= 0:
        await wait_msg.edit(
            "❌ That looks like a single video (or nothing was found).\n"
            "_Just paste the video link directly to back it up — "
            "or send the channel/playlist URL._"
        )
        return

    key   = meta.get("channel_url") or url
    title = meta.get("playlist_title") or channel or "Unknown"

    # ── Resolve destination ─────────────────────────────────────────────
    dest_id, dest_title = 0, ""
    if dest_arg:
        try:
            chat = await client.get_chat(dest_arg)
            dest_title = getattr(chat, "title", None) or "Private Chat"
            dest_id = dest_arg
        except Exception as exc:
            await wait_msg.edit(f"❌ Can't access chat `{dest_arg}`: `{exc}`")
            return

    # ── Already watching? ───────────────────────────────────────────────
    existing = state.watch_by_key(key, url)
    if existing:
        note = ""
        if dest_id:
            existing.dest_chat_id = dest_id
            existing.dest_chat_title = dest_title
            state.mark_dirty()
            note = f"\n📍 Destination updated → **{dest_title}**"
        await wait_msg.edit(
            f"ℹ️ **Already watching** `{existing.id}` — **{existing.title}**\n"
            f"Known videos: `{len(existing.known_ids)}`"
            f"{note}\n\n_Use `/watchlist` to manage._"
        )
        return

    # ── Snapshot current videos as "known" ──────────────────────────────
    wid   = state.next_watch_id()
    known = [it["id"] for it in items if it.get("id")]
    added_by = message.from_user.id if message.from_user else 0

    w = state.add_watch(
        wid, url, key, title, known,
        dest_chat_id=dest_id, dest_chat_title=dest_title, added_by=added_by,
    )

    backfill_note = ""
    if full:
        quality = state.settings.get("quality", "best")
        n, _ = state.add_tasks(
            sort_items(items, "old_new"), source="watch", quality=quality,
            dest_chat_id=dest_id, added_by=added_by,
        )
        backfill_note = f"\n📦 Backfill: `{n}` existing videos queued too."

    interval = state.watch_interval(w)
    await wait_msg.edit(
        f"✅ **𝗪𝗮𝘁𝗰𝗵 𝗔𝗰𝘁𝗶𝘃𝗲!**\n"
        f"{SEP}\n"
        f"⋄ 📺 Channel: **{md_escape(title)}**\n"
        f"⋄ 🆔 Watch ID: `{wid}`\n"
        f"⋄ 🎬 Known videos: `{len(known)}` _(won't re-download)_\n"
        f"⋄ 📍 Destination: **{dest_title or 'Global (see /destinfo)'}**\n"
        f"⋄ ⏱ Auto-check: every `{interval}m`\n"
        f"{SEP}\n"
        f"🆕 Only **new uploads** will be auto-backed up.{backfill_note}",
        reply_markup=kb_watch_actions(wid),
    )


@Client.on_message(filters.command("unwatch") & admin_filter)
async def cmd_unwatch(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Usage: `/unwatch w3` or `/unwatch <channel name>`")
        return

    q = args[1].strip()
    watches = state.all_watches()

    match = next((w for w in watches if w.id == q.lower()), None)
    if not match:
        ql = q.lower()
        hits = [w for w in watches if ql in (w.title or "").lower()]
        if len(hits) == 1:
            match = hits[0]
        elif len(hits) > 1:
            lst = "\n".join(f"⋄ `{w.id}` — {short(w.title, 36)}" for w in hits[:10])
            await message.reply(f"Multiple matches — use the exact ID:\n{lst}")
            return

    if not match:
        await message.reply(f"❌ No watch found for `{q}`. See `/watchlist`.")
        return

    state.remove_watch(match.id)
    await message.reply(
        f"🗑 Watch removed: **{md_escape(match.title or match.id)}**\n"
        f"_Already-queued tasks keep running — `/resetqueue` to cancel them._"
    )


def _watchlist_text() -> str:
    watches = state.all_watches()
    if not watches:
        return (
            f"❖ **𝗪𝗮𝘁𝗰𝗵𝗲𝗱 𝗖𝗵𝗮𝗻𝗻𝗲𝗹𝘀**\n"
            f"{SEP}\n"
            "_No watches yet._\nAdd one with `/watch <channel_url>`"
        )

    wp = state.settings.get("watcher_paused", False)
    head = (
        f"❖ **𝗪𝗮𝘁𝗰𝗵𝗲𝗱 𝗖𝗵𝗮𝗻𝗻𝗲𝗹𝘀**\n"
        f"{SEP}\n"
    )
    if wp:
        head += "⏸ **Watcher is PAUSED** — `/watchresume` to enable\n\n"

    rows = []
    now = time.time()
    for w in watches:
        dot   = "🟢" if w.enabled else "⏸"
        sched = state.watch_schedule_label(w)
        ago   = human_time_short(now - w.last_check) + " ago" if w.last_check else "never"
        dest  = w.dest_chat_title or (str(w.dest_chat_id) if w.dest_chat_id else "global")
        q     = f" · 🎞 {quality_label(w.quality)}" if w.quality else ""
        rows.append(
            f"{dot} `{w.id}` **{md_escape(short(w.title or w.url, 26))}**\n"
            f"   📍 {md_escape(short(dest, 20))} · {sched}{q}\n"
            f"   🎬 {len(w.known_ids)} known · last check: {ago}"
        )
    return head + "\n".join(rows) + f"\n{SEP}\n_⏯ toggle · 📺 details · 🗑 remove_"


@Client.on_message(filters.command("watchlist") & admin_filter)
async def cmd_watchlist(client: Client, message: Message):
    watches = state.all_watches()
    markup = kb_watchlist(watches) if watches else None
    await message.reply(_watchlist_text(), reply_markup=markup)


@Client.on_message(filters.command("checknow") & admin_filter)
async def cmd_checknow(client: Client, message: Message):
    args = message.text.split()
    watches = state.all_watches()

    if len(args) > 1:
        w = next((w for w in watches if w.id == args[1].lower()), None)
        if not w:
            await message.reply(f"❌ No watch `{args[1]}`. See `/watchlist`.")
            return
        watches = [w]
    else:
        watches = [w for w in watches if w.enabled]

    if not watches:
        await message.reply("_Nothing to check — add a watch with `/watch`._")
        return

    wait_msg = await message.reply(f"🔍 Checking `{len(watches)}` watch(es)…")

    total_new = 0
    lines = []
    for w in watches:
        if stop_event.is_set():
            break
        try:
            new_items = await check_watch(state, w)
        except Exception as exc:
            lines.append(f"⚠️ `{w.id}` {short(w.title, 24)} — error: {short(str(exc), 40)}")
            continue
        if new_items:
            total_new += len(new_items)
            lines.append(f"🔔 `{w.id}` **{md_escape(short(w.title, 26))}** — {len(new_items)} new ✅")
            from core.watcher import notify_new_videos
            await notify_new_videos(client, state, w, new_items)
        else:
            lines.append(f"✔️ `{w.id}` {md_escape(short(w.title, 26))} — no new videos")
        state.save()

    summary = f"🔍 **Check complete** — `{total_new}` new videos queued"
    await wait_msg.edit(summary + "\n" + "\n".join(lines[:15]))


@Client.on_message(filters.command("backfill") & admin_filter)
async def cmd_backfill(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Usage: `/backfill w3` — queues ALL videos of that watch.")
        return

    w = state.get_watch(args[1].lower())
    if not w:
        await message.reply(f"❌ No watch `{args[1]}`. See `/watchlist`.")
        return

    wait_msg = await message.reply(f"⏳ _Scanning {w.title or w.id} for backfill…_")
    try:
        result = await scan(w.url)
    except Exception as exc:
        await wait_msg.edit(f"❌ Scan failed: `{exc}`")
        return

    items = result.get("items", [])
    if not items:
        await wait_msg.edit("❌ No videos found.")
        return

    quality = w.quality or state.settings.get("quality", "best")
    added_by = message.from_user.id if message.from_user else 0
    n, _ = state.add_tasks(
        sort_items(items, "old_new"), source="watch", quality=quality,
        dest_chat_id=w.dest_chat_id, added_by=added_by,
    )

    # Refresh the known snapshot too
    ids = [it["id"] for it in items if it.get("id")]
    w.known_ids = list(dict.fromkeys(w.known_ids + ids))[-5000:]
    state.mark_dirty()

    dest = w.dest_chat_title or (str(w.dest_chat_id) if w.dest_chat_id else "global destination")
    await wait_msg.edit(
        f"📦 **Backfill queued**\n"
        f"⋄ Channel: **{md_escape(w.title or w.id)}**\n"
        f"⋄ Videos: `{n}`\n"
        f"⋄ Destination: `{dest}`"
    )


# ---------------------------------------------------------------------------
# /syncfrom — resume after crash: "is video ke baad wale sab bhejo"
# ---------------------------------------------------------------------------

@Client.on_message(filters.command(["syncfrom", "resumefrom"]) & admin_filter)
async def cmd_syncfrom(client: Client, message: Message):
    """
    Send the LAST successfully uploaded video link:
      /syncfrom <video_link>          → queue everything NEWER than it
      /syncfrom <video_link> before   → queue everything OLDER than it
    Perfect for recovering after a server crash / fresh deploy.
    """
    args = message.text.split()
    if len(args) < 2:
        await message.reply(
            "❖ **𝗥𝗲𝘀𝘂𝗺𝗲 𝗔𝗳𝘁𝗲𝗿 𝗖𝗿𝗮𝘀𝗵**\n"
            f"{SEP}\n"
            "Send the link of the **last video that was already uploaded**:\n\n"
            "`/syncfrom <video_link>`\n"
            "→ queues all videos uploaded **after** it\n\n"
            "`/syncfrom <video_link> before`\n"
            "→ queues all **older** videos instead\n\n"
            "_The marker video itself is NOT re-uploaded._"
        )
        return

    url = next((a for a in args[1:] if a.lower().startswith("http") or "youtu" in a.lower()), None)
    direction = "before" if any(a.lower() in ("before", "older", "pehle") for a in args[1:]) else "after"

    if not url:
        await message.reply("❌ Video link missing. Usage: `/syncfrom <video_link>`")
        return

    marker_vid = parse_video_id(url)
    if not marker_vid:
        await message.reply("❌ Could not extract a video ID from that link.")
        return

    wait_msg = await message.reply("⏳ _Step 1/2 — finding the video's channel…_")

    try:
        vres = await scan(url)
    except Exception as exc:
        await wait_msg.edit(f"❌ Could not read the video: `{exc}`")
        return

    vmeta = vres.get("meta", {}) or {}
    ch_url = vmeta.get("channel_url", "")
    marker_title = (vres.get("items") or [{}])[0].get("title", marker_vid)
    if not ch_url:
        await wait_msg.edit("❌ Could not resolve the channel of that video.")
        return

    try:
        await wait_msg.edit("⏳ _Step 2/2 — scanning full channel list…_")
        cres = await scan(ch_url)
    except Exception as exc:
        await wait_msg.edit(f"❌ Channel scan failed: `{exc}`")
        return

    items = cres.get("items", [])
    ordered = sort_items(items, "old_new")   # oldest → newest

    idx = next((i for i, it in enumerate(ordered) if it.get("id") == marker_vid), None)
    if idx is None:
        await wait_msg.edit(
            f"❌ Marker video not found in the channel's upload list.\n"
            f"_It may be deleted, a Short, or from another tab._"
        )
        return

    selected = ordered[:idx] if direction == "before" else ordered[idx + 1:]
    if not selected:
        await wait_msg.edit(
            f"✅ Marker found: **{md_escape(short(marker_title, 40))}**\n"
            f"…but there are no videos **{direction}** it. Nothing to queue."
        )
        return

    # ── Route via matching watch (destination + known-snapshot) ─────────
    watch = state.watch_by_key(ch_url, cres.get("meta", {}).get("source_url", ""))
    dest = watch.dest_chat_id if watch else 0
    if watch:
        covered = ordered[:idx + 1] + (selected if direction == "after" else [])
        known = set(watch.known_ids) | {it["id"] for it in covered if it.get("id")}
        watch.known_ids = list(known)[-5000:]
        state.mark_dirty()

    quality = (watch.quality if watch else "") or state.settings.get("quality", "best")
    uid = message.from_user.id if message.from_user else 0
    n, _ = state.add_tasks(
        sort_items(selected, "old_new"), source="sync", quality=quality,
        dest_chat_id=dest, added_by=uid,
    )
    state.save()

    ch_name = cres.get("channel", "") or ch_url
    dest_txt = (
        f"**{watch.dest_chat_title or watch.dest_chat_id}** (watch `{watch.id}`)"
        if watch else "**Global destination** (`/destinfo`)"
    )
    arrow = "⬆️ newer" if direction == "after" else "⬇️ older"

    await wait_msg.edit(
        f"✅ **𝗦𝘆𝗻𝗰 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲!**\n"
        f"{SEP}\n"
        f"⋄ 📺 Channel: **{md_escape(ch_name)}**\n"
        f"⋄ 📌 Marker: **{md_escape(short(marker_title, 36))}**\n"
        f"⋄ {arrow} videos queued: `{n}`\n"
        f"⋄ 🎞 Quality: {quality_label(quality)}\n"
        f"⋄ 📍 Destination: {dest_txt}\n"
        f"{SEP}\n"
        f"_Marker video skipped (already uploaded). /status to watch progress._"
        + ("" if watch else "\n\n💡 `/watch " + ch_url + "` to keep it auto-synced.")
    )


# ---------------------------------------------------------------------------
# /setuploaders  &  /setqlimit — upload pipeline tuning (owner)
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("setuploaders") & owner_filter)
async def cmd_setuploaders(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply(
            f"Upload workers: `{Config.UPLOAD_WORKERS}`\n\n"
            "Usage: `/setuploaders 1` or `/setuploaders 2`\n"
            "_1 = strictly sequential (safest) · 2 = parallel (faster, mild flood risk)_\n\n"
            "⚠️ Takes effect after bot restart."
        )
        return
    try:
        n = int(args[1])
    except ValueError:
        await message.reply("❌ Provide 1 or 2.")
        return
    if n not in (1, 2):
        await message.reply("❌ Only 1 or 2 upload workers allowed (Telegram flood safety).")
        return
    Config.UPLOAD_WORKERS = n
    os.environ["UPLOAD_WORKERS"] = str(n)
    await message.reply(
        f"⬆️ Upload workers → `{n}`\n"
        f"_Fully applied after restart. Uploads within each worker stay sequential "
        f"and share one FloodWait cooldown._"
    )


@Client.on_message(filters.command("setqlimit") & owner_filter)
async def cmd_setqlimit(client: Client, message: Message):
    """Upload-queue watermark: max videos waiting downloaded for upload."""
    args = message.text.split()
    cur = int(state.settings.get("upload_queue_limit", Config.UPLOAD_QUEUE_LIMIT))
    if len(args) < 2:
        await message.reply(
            f"Upload queue cap: `{cur}` videos\n\n"
            "Usage: `/setqlimit 3`\n"
            "_Downloads pause automatically whenever this many videos are "
            "already downloaded-waiting/uploading — protects disk space._"
        )
        return
    try:
        n = int(args[1])
    except ValueError:
        await message.reply("❌ Provide a number 1–20.")
        return
    if n < 1 or n > 20:
        await message.reply("❌ Cap must be 1–20.")
        return
    state.settings["upload_queue_limit"] = n
    state.mark_dirty()
    await message.reply(
        f"📦 Upload queue cap → `{n}` videos\n"
        f"_New downloads start only when the upload pipeline drops below {n}._"
    )


@Client.on_message(filters.command("watchdest") & admin_filter)
async def cmd_watchdest(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.reply("Usage: `/watchdest w3 -1001234567890`")
        return

    w = state.get_watch(args[1].lower())
    if not w:
        await message.reply(f"❌ No watch `{args[1]}`. See `/watchlist`.")
        return

    try:
        chat_id = int(args[2])
    except ValueError:
        await message.reply("❌ Chat ID must be numeric.")
        return

    try:
        chat = await client.get_chat(chat_id)
    except Exception as exc:
        await message.reply(f"❌ Can't access `{chat_id}`: `{exc}`")
        return

    w.dest_chat_id = chat_id
    w.dest_chat_title = getattr(chat, "title", None) or "Private Chat"
    state.mark_dirty()
    await message.reply(
        f"✅ **{md_escape(w.title or w.id)}** → now uploads to "
        f"**{md_escape(w.dest_chat_title)}** (`{chat_id}`)"
    )


@Client.on_message(filters.command("watchinterval") & admin_filter)
async def cmd_watchinterval(client: Client, message: Message):
    """
    /watchinterval 30       → global default (all watches without override)
    /watchinterval w1 720   → one watch every 12h (720m) / 1440m = 24h
    /watchinterval w1 0     → clear per-watch override
    """
    args = message.text.split()

    # Per-watch mode: /watchinterval w1 720
    if len(args) >= 3:
        w = state.get_watch(args[1].lower())
        if w:
            try:
                mins = int(args[2])
            except ValueError:
                await message.reply("❌ Minutes must be a number (0 or 5–1440).")
                return
            if mins != 0 and (mins < 5 or mins > 1440):
                await message.reply("❌ Interval must be 5–1440 minutes (or 0 to clear).")
                return
            w.interval_min = mins
            w.daily_at = ""  # interval mode replaces daily schedule
            state.mark_dirty()
            if mins == 0:
                await message.reply(f"✅ `{w.id}` override cleared → global interval")
            else:
                h = mins / 60
                nice = f" ({h:.0f}h)" if h == int(h) else ""
                await message.reply(f"⏱ `{w.id}` → checked every `{mins}` minutes{nice}")
            return
        # else fall through — maybe user typed it wrong

    if len(args) < 2:
        cur = state.settings.get("watch_interval_min", 0) or Config.WATCH_INTERVAL_MIN
        await message.reply(
            f"Global check interval: `{cur}` minutes.\n\n"
            "Usage:\n"
            "`/watchinterval 30` — global default\n"
            "`/watchinterval w1 720` — one watch, every 12h\n"
            "`/watchtime w1 06:00` — or fixed daily time"
        )
        return
    try:
        mins = int(args[1])
    except ValueError:
        await message.reply("❌ Provide minutes as a number (5–1440).")
        return
    if mins < 5 or mins > 1440:
        await message.reply("❌ Interval must be between 5 and 1440 minutes.")
        return
    state.settings["watch_interval_min"] = mins
    state.mark_dirty()
    await message.reply(f"⏱ Global watch interval → every `{mins}` minutes")


@Client.on_message(filters.command("watchtime") & admin_filter)
async def cmd_watchtime(client: Client, message: Message):
    """
    Schedule a watch:
      /watchtime w1 06:00   → check once daily at 6:00 AM
      /watchtime all 06:00  → same for every watch
      /watchtime w1 off     → back to interval mode
    """
    args = message.text.split()
    if len(args) < 3:
        await message.reply(
            "**Usage:**\n"
            "`/watchtime w1 06:00` — daily at 6 AM\n"
            "`/watchtime all 22:30` — all watches daily 10:30 PM\n"
            "`/watchtime w1 off` — back to interval mode\n\n"
            "_For 12h/24h cycles use `/watchinterval w1 720` / `1440`._"
        )
        return

    target = args[1].lower()
    value  = args[2].strip()

    watches = state.all_watches()
    if target == "all":
        targets = watches
    else:
        w = next((w for w in watches if w.id == target), None)
        if not w:
            await message.reply(f"❌ No watch `{target}`. See `/watchlist`.")
            return
        targets = [w]

    if value.lower() == "off":
        for w in targets:
            w.daily_at = ""
        state.mark_dirty()
        await message.reply(
            f"⏱ Schedule cleared for `{len(targets)}` watch(es) — "
            f"back to interval mode."
        )
        return

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        await message.reply("❌ Time must be 24-hour `HH:MM`, e.g. `06:00` or `22:30`.")
        return

    hh, mm = int(m.group(1)), int(m.group(2))
    daily  = f"{hh:02d}:{mm:02d}"
    for w in targets:
        w.daily_at = daily
    state.mark_dirty()

    names = ", ".join(f"`{w.id}`" for w in targets[:5])
    await message.reply(
        f"⏰ Daily schedule set!\n"
        f"⋄ Watches: {names}{' …' if len(targets) > 5 else ''}\n"
        f"⋄ Check time: **{daily}** (every day)\n\n"
        f"_If the bot is offline at {daily}, it checks right after starting._"
    )


@Client.on_message(filters.command("watchquality") & admin_filter)
async def cmd_watchquality(client: Client, message: Message):
    """/watchquality w1 720 — quality override for one watch (or 'default')."""
    args = message.text.split()
    if len(args) < 3:
        await message.reply(
            "Usage: `/watchquality w1 720`\n"
            f"Qualities: `{'`, `'.join(VALID_QUALITIES)}` · `default` = global"
        )
        return

    w = state.get_watch(args[1].lower())
    if not w:
        await message.reply(f"❌ No watch `{args[1]}`. See `/watchlist`.")
        return

    q = args[2].lower()
    if q == "default":
        w.quality = ""
        state.mark_dirty()
        await message.reply(f"✅ `{w.id}` → global default quality")
        return
    if q not in VALID_QUALITIES:
        await message.reply(f"❌ Unknown quality `{q}`. Choose: `{'`, `'.join(VALID_QUALITIES)}`")
        return

    w.quality = q
    state.mark_dirty()
    await message.reply(
        f"✅ **{md_escape(short(w.title or w.id, 30))}** → {quality_label(q)}\n"
        f"_Applies to future auto-detected videos._"
    )


@Client.on_message(filters.command("watchpause") & admin_filter)
async def cmd_watchpause(client: Client, message: Message):
    state.settings["watcher_paused"] = True
    state.settings["watcher_pause_reason"] = "manual"
    state.mark_dirty()
    await message.reply("⏸ **Watcher paused** — no auto-checks until `/watchresume`.")


@Client.on_message(filters.command("watchresume") & admin_filter)
async def cmd_watchresume(client: Client, message: Message):
    state.settings["watcher_paused"] = False
    state.settings.pop("watcher_pause_reason", None)
    state.mark_dirty()
    await message.reply("▶️ **Watcher resumed** — auto-checks are back on.")


# ---------------------------------------------------------------------------
# 👥 USER MANAGEMENT (owner only)
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("adduser") & owner_filter)
async def cmd_adduser(client: Client, message: Message):
    """
    /adduser                → reply mode, role = user
    /adduser admin          → reply mode, role = admin
    /adduser 123456789      → id mode
    /adduser @username admin
    """
    args = message.text.split()[1:]
    role = ROLE_USER
    token = None

    for tok in args:
        if tok.lower() in (ROLE_ADMIN, ROLE_USER):
            role = tok.lower()
        else:
            token = tok

    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    elif token:
        target = await _resolve_user_arg(client, token)

    if target is None:
        await message.reply(
            "**Usage:** reply to the person's message with `/adduser [admin|user]`\n"
            "or `/adduser <user_id|@username> [admin|user]`"
        )
        return

    if target.id == Config.OWNER_ID:
        await message.reply("👑 That's the owner — already has full access.")
        return

    name = " ".join(filter(None, [target.first_name, target.last_name])) or str(target.id)
    state.add_user(target.id, role, name, message.from_user.id)
    state.save()

    perms = ("manage watches + queue" if role == ROLE_ADMIN
             else "submit links + view status")
    await message.reply(
        f"✅ **User Added**\n"
        f"{SEP}\n"
        f"⋄ {ROLE_ICON[role]} Role: **{role.title()}**\n"
        f"⋄ 👤 Name: **{md_escape(name)}**\n"
        f"⋄ 🆔 ID: `{target.id}`\n"
        f"{SEP}\n"
        f"🔓 Can now: {perms}"
    )


@Client.on_message(filters.command("removeuser") & owner_filter)
async def cmd_removeuser(client: Client, message: Message):
    args = message.text.split()[1:]
    uid = None

    if message.reply_to_message and message.reply_to_message.from_user:
        uid = message.reply_to_message.from_user.id
    elif args:
        try:
            uid = int(args[0])
        except ValueError:
            u = await _resolve_user_arg(client, args[0])
            uid = u.id if u else None

    if uid is None:
        await message.reply("Usage: reply with `/removeuser` or `/removeuser <user_id>`")
        return
    if uid == Config.OWNER_ID:
        await message.reply("👑 Can't remove the owner.")
        return

    if state.remove_user(uid):
        state.save()
        await message.reply(f"🗑 User `{uid}` removed — access revoked.")
    else:
        await message.reply(f"❌ `{uid}` is not a registered user. See `/users`.")


@Client.on_message(filters.command("setrole") & owner_filter)
async def cmd_setrole(client: Client, message: Message):
    args = message.text.split()[1:]
    role = next((a.lower() for a in args if a.lower() in (ROLE_ADMIN, ROLE_USER)), None)

    uid = None
    if message.reply_to_message and message.reply_to_message.from_user:
        uid = message.reply_to_message.from_user.id
    else:
        tok = next((a for a in args if a.lower() not in (ROLE_ADMIN, ROLE_USER)), None)
        if tok:
            try:
                uid = int(tok)
            except ValueError:
                u = await _resolve_user_arg(client, tok)
                uid = u.id if u else None

    if uid is None or role is None:
        await message.reply("Usage: `/setrole <user_id|reply> <admin|user>`")
        return

    if state.set_role(uid, role):
        state.save()
        await message.reply(f"✅ `{uid}` → **{role.title()}** {ROLE_ICON[role]}")
    else:
        await message.reply(f"❌ `{uid}` is not registered. `/adduser` first.")


def _users_text() -> str:
    owner_name = "You"
    lines = [
        f"❖ **𝗔𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝗨𝘀𝗲𝗿𝘀**",
        SEP,
        f"👑 Owner: `{Config.OWNER_ID}`",
    ]
    users = state.all_users()
    for uid, info in users:
        icon = ROLE_ICON.get(info.get("role"), "👤")
        name = md_escape(info.get("name") or str(uid))
        lines.append(f"{icon} {name} — `{uid}` · _{info.get('role')}_")
    lines += [
        SEP,
        f"Total: `{len(users) + 1}` (incl. owner)",
        "_Tap name → toggle admin↔user · 🗑 → remove_",
    ]
    return "\n".join(lines)


@Client.on_message(filters.command("users") & owner_filter)
async def cmd_users(client: Client, message: Message):
    await message.reply(_users_text(), reply_markup=kb_users(state.all_users()))


@Client.on_message(filters.command("whoami") & user_filter)
async def cmd_whoami(client: Client, message: Message):
    uid  = message.from_user.id
    role = _role_of_user(uid)
    perms = {
        ROLE_OWNER: "Full control — users, settings, watches, queue",
        ROLE_ADMIN: "Manage watches + queue + submit links",
        ROLE_USER:  "Submit links + view status",
    }.get(role, "None")
    await message.reply(
        f"🪪 **Your Profile**\n"
        f"{SEP}\n"
        f"⋄ 🆔 ID: `{uid}`\n"
        f"⋄ {ROLE_ICON.get(role, '❔')} Role: **{(role or 'unknown').title()}**\n"
        f"⋄ 🔓 Permissions: {perms}"
    )


# ---------------------------------------------------------------------------
# /diskspace  &  /serverinfo  &  /speedtest
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("diskspace") & user_filter)
async def cmd_diskspace(client: Client, message: Message):
    await message.reply(sys_disk_report())


@Client.on_message(filters.command("serverinfo") & user_filter)
async def cmd_serverinfo(client: Client, message: Message):
    await message.reply(sys_server_report())


@Client.on_message(filters.command("speedtest") & user_filter)
async def cmd_speedtest(client: Client, message: Message):
    wait_msg = await message.reply("🌐 _Running speedtest: 200 MB DL + 200 MB UL + ping (≥10s)…_")
    result   = await run_speedtest()
    await wait_msg.edit(result)


# ---------------------------------------------------------------------------
# /logs [lines|level]
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("logs") & admin_filter)
async def cmd_logs(client: Client, message: Message):
    args         = message.text.split()[1:]
    level_filter = None
    lines        = 40

    for tok in args:
        tok = tok.strip()
        if tok.upper() in ("ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
            level_filter = tok.upper() if tok.upper() != "WARN" else "WARNING"
        elif tok.isdigit():
            lines = min(max(int(tok), 1), 200)

    log_text = tail_log(Config.DATA_DIR, lines=lines, level=level_filter)
    label    = f" (filter: {level_filter})" if level_filter else ""

    if len(log_text) <= 3800:
        await message.reply(f"**Logs**{label}\n```\n{log_text}\n```")
    else:
        buf      = io.BytesIO(log_text.encode("utf-8"))
        buf.name = "logs.txt"
        await message.reply_document(buf, caption=f"Logs{label}")


# ---------------------------------------------------------------------------
# /cookies  (owner only)
# ---------------------------------------------------------------------------

# A direct document upload is allowed for 15 minutes after `/cookies`. A file
# whose name contains "cookie" is also accepted directly at any time, making
# uploads survive a bot restart and avoiding fragile handler/filter ordering.
_COOKIE_UPLOAD_TIMEOUT_SECONDS = 15 * 60
_cookie_upload_requests: dict[int, tuple[int, int, float]] = {}
_cookie_install_lock = asyncio.Lock()


def _pending_cookie_upload(chat_id: int) -> Optional[tuple[int, int, float]]:
    """Return a non-expired cookie-upload request for a chat, if any."""
    pending = _cookie_upload_requests.get(chat_id)
    if pending and pending[2] <= time.monotonic():
        _cookie_upload_requests.pop(chat_id, None)
        return None
    return pending


def _start_cookie_upload(chat_id: int, command_id: int, prompt_id: int) -> None:
    _cookie_upload_requests[chat_id] = (
        command_id,
        prompt_id,
        time.monotonic() + _COOKIE_UPLOAD_TIMEOUT_SECONDS,
    )


def _cookie_request_reply(message: Message) -> bool:
    """Recognise replies to an old /cookies command/prompt after a restart."""
    reply = message.reply_to_message
    if not reply:
        return False
    raw = f"{reply.text or ''}\n{reply.caption or ''}"
    folded = raw.casefold()
    return bool(
        re.search(r"(?m)^/cookies(?:@[a-z0-9_]+)?(?:\s|$)", folded)
        or ("upload" in folded and "cookie" in folded)
        or ("𝗨𝗽𝗹𝗼𝗮𝗱" in raw and "𝗖𝗼𝗼𝗸𝗶𝗲𝘀" in raw)
    )


def _looks_like_cookie_document(message: Message) -> bool:
    """Route requested or clearly named cookie documents to one handler."""
    doc = getattr(message, "document", None)
    if not doc:
        return False
    if _pending_cookie_upload(message.chat.id) or _cookie_request_reply(message):
        return True
    # Owners may send cookies.txt directly without running /cookies first.
    name = (doc.file_name or "").strip().casefold()
    return "cookie" in name


def _cookie_panel_text(chat_id: int) -> str:
    pending = _pending_cookie_upload(chat_id)
    wait_line = ""
    if pending:
        minutes = max(1, int((pending[2] - time.monotonic() + 59) // 60))
        wait_line = f"\n\n🟢 **Upload window open:** `{minutes} min` remaining"
    cooldown = youtube_cooldown_remaining()
    cooldown_line = (
        f"\n🛑 **YouTube cooldown:** `{human_time_short(cooldown)}` remaining"
        if cooldown else ""
    )
    return (
        f"❖ **𝗬𝗼𝘂𝗧𝘂𝗯𝗲 𝗔𝘂𝘁𝗵**\n"
        f"{SEP}\n"
        f"{auth_status()}"
        f"{wait_line}{cooldown_line}\n"
        f"{SEP}\n"
        f"📎 Send the export as a Telegram **File/Document**.\n"
        f"_File Status is structural; use 🌐 Live Check to test Railway → YouTube._"
    )


@Client.on_message(filters.command("cookies") & owner_filter)
async def cmd_cookies(client: Client, message: Message):
    # Open the window before awaiting Telegram so even a very fast upload is
    # recognised. Update it with the prompt id after the message is sent.
    _start_cookie_upload(message.chat.id, message.id, 0)
    prompt = await message.reply(
        _cookie_panel_text(message.chat.id)
        + "\n\n"
        + "**3 quick steps**\n"
        + "1️⃣ Sign in at `youtube.com` in Chrome/Firefox\n"
        + "2️⃣ Export **Netscape** format with `Get cookies.txt LOCALLY`\n"
        + "3️⃣ Send that `.txt` here now — reply is optional",
        reply_markup=kb_auth(waiting=True),
    )
    _start_cookie_upload(message.chat.id, message.id, prompt.id)


@Client.on_message(filters.document & owner_filter)
async def on_owner_document(client: Client, message: Message):
    """Install owner cookie uploads through one deterministic document route.

    Pyrogram only executes the first matching message handler in a handler
    group. Keeping detection and installation in this single handler prevents
    a generic document handler from silently swallowing cookies.txt.
    """
    if not _looks_like_cookie_document(message):
        return

    doc = message.document
    file_name = (doc.file_name if doc and doc.file_name else "").strip()
    if not file_name.casefold().endswith(".txt"):
        await message.reply(
            "❌ **Wrong file type**\n"
            "Send the Netscape export as a `.txt` **File/Document**, not JSON, ZIP, photo or pasted text.",
            reply_markup=kb_auth(waiting=True),
        )
        return

    file_size = int(getattr(doc, "file_size", 0) or 0)
    if file_size > MAX_COOKIE_FILE_BYTES:
        await message.reply(
            f"❌ Cookie file is too large (`{file_size:,}` bytes). "
            f"Maximum: `{MAX_COOKIE_FILE_BYTES // (1024 * 1024)} MB`."
        )
        return

    progress = await message.reply("⏳ **Cookies received** · downloading and validating…")
    cookies_path = configured_cookie_path()
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cookies_path.with_name(
        f".{cookies_path.name}.{message.chat.id}.{message.id}.upload"
    )
    downloaded_path = temp_path

    try:
        async with _cookie_install_lock:
            downloaded = await asyncio.wait_for(
                client.download_media(message, file_name=str(temp_path)),
                timeout=120,
            )
            if not downloaded:
                raise RuntimeError("Telegram returned no downloaded file path")
            downloaded_path = Path(downloaded).resolve()

            # Validation happens before os.replace, so a bad upload can never
            # erase the currently working cookies file.
            info = install_cookies_file(downloaded_path, cookies_path)
    except asyncio.TimeoutError:
        logger.warning("Cookie download timed out for chat %s", message.chat.id)
        await progress.edit(
            "❌ Cookie download timed out. Check the server connection and send the file again.",
            reply_markup=kb_auth(waiting=True),
        )
        return
    except ValueError as exc:
        logger.warning("Rejected invalid cookies upload from %s: %s", message.chat.id, exc)
        await progress.edit(
            f"⚠️ **Cookies not changed**\n{exc}\n\n"
            "Export again while logged into `youtube.com`.",
            reply_markup=kb_auth(waiting=True),
        )
        return
    except Exception as exc:
        logger.exception("Failed to save cookies upload")
        await progress.edit(
            f"❌ **Could not save cookies**\n`{md_escape(short(str(exc), 180))}`\n\n"
            f"Manual path: `{cookies_path}`",
            reply_markup=kb_auth(waiting=True),
        )
        return
    finally:
        # os.replace moves a successful temp file. Never delete the active path.
        for path in {temp_path, downloaded_path}:
            if path != cookies_path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    Config.COOKIES_PATH = str(info.path)
    _cookie_upload_requests.pop(message.chat.id, None)
    reset_bot_alert()

    # Recover tasks failed by older deployments, but do not immediately unleash
    # parallel workers into an IP that may still be returning 429. A successful
    # Live Check below clears the cooldown and resumes auth-paused components.
    recovered = 0
    awaiting_live_check = False
    if state is not None:
        recovered = state.retry_failed_matching((
            "bot detection",
            "http error 429",
            "too many requests",
            "only images are available",
        ))
        if state.settings.get("pause_reason") == "youtube_auth" or recovered > 0:
            state.settings["paused"] = True
            state.settings["pause_reason"] = "youtube_auth"
            awaiting_live_check = True
        if state.settings.get("watcher_pause_reason") == "youtube_auth":
            awaiting_live_check = True
        if recovered or awaiting_live_check:
            state.mark_dirty()

    login_line = (
        f"✅ Login markers: `{info.auth_cookie_count}`"
        if info.has_login_cookies
        else "⚠️ No login markers found — re-export while signed in if downloads fail"
    )
    await progress.edit(
        f"✅ **𝗖𝗼𝗼𝗸𝗶𝗲𝘀 𝗟𝗼𝗮𝗱𝗲𝗱**\n"
        f"{SEP}\n"
        f"🍪 YouTube rows: `{info.youtube_cookie_count}`\n"
        f"{login_line}\n"
        f"📦 Size: `{info.size_bytes:,}` bytes\n"
        f"🔒 Permission: `owner-only (0600)`\n"
        f"📁 `{info.path}`\n"
        + (f"🔁 Re-queued old auth failures: `{recovered}`\n" if recovered else "")
        + ("⏸ Queue is safe—run 🌐 Live Check before resume\n" if awaiting_live_check else "")
        + "\n_The next yt-dlp request uses this file; no restart needed._",
        reply_markup=kb_auth(waiting=False),
    )
    logger.info(
        "Cookies installed at %s (%d YouTube rows, %d login markers)",
        info.path, info.youtube_cookie_count, info.auth_cookie_count,
    )


# ---------------------------------------------------------------------------
# /authstatus  &  /ytdlpupdate
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("authstatus") & admin_filter)
async def cmd_authstatus(client: Client, message: Message):
    await message.reply(
        _cookie_panel_text(message.chat.id),
        reply_markup=kb_auth(waiting=bool(_pending_cookie_upload(message.chat.id))),
    )


async def _run_auth_live_check(message: Message) -> None:
    wait = await message.reply("🌐 **Live auth check** · contacting YouTube once…")
    try:
        ok, detail = await asyncio.wait_for(
            asyncio.to_thread(probe_youtube_access),
            timeout=45,
        )
    except asyncio.TimeoutError:
        ok, detail = False, "YouTube did not respond within 45 seconds."

    if ok:
        clear_youtube_cooldown()
        resumed = False
        if state is not None:
            if state.settings.get("pause_reason") == "youtube_auth":
                state.settings["paused"] = False
                state.settings.pop("pause_reason", None)
                resumed = True
            if state.settings.get("watcher_pause_reason") == "youtube_auth":
                state.settings["watcher_paused"] = False
                state.settings.pop("watcher_pause_reason", None)
                resumed = True
            if resumed:
                state.mark_dirty()
        await wait.edit(
            f"✅ **Live YouTube check passed**\n{detail}\n\n"
            "Railway can currently see playable media formats."
            + ("\n▶️ Auth-paused queue/watcher resumed automatically." if resumed else "")
        )
    else:
        await wait.edit(
            f"❌ **Live YouTube check failed**\n{detail}\n\n"
            "Upload fresh `/cookies`. For HTTP 429, also wait 30–60 minutes "
            "before retrying; repeatedly pressing resume makes the block longer."
        )


@Client.on_message(filters.command("authcheck") & admin_filter)
async def cmd_authcheck(client: Client, message: Message):
    await _run_auth_live_check(message)


@Client.on_message(filters.command("ytdlpupdate") & owner_filter)
async def cmd_ytdlpupdate(client: Client, message: Message):
    import subprocess
    import sys
    wait_msg = await message.reply("⏳ _Updating yt-dlp…_")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=180,
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
# /purge  (owner only)
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
        f"🗑 Delete last **{n}** uploaded messages from the destination chat?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"purge_confirm_{n}"),
            InlineKeyboardButton("✖️ Cancel", callback_data="confirm_no"),
        ]]),
    )


# ---------------------------------------------------------------------------
# /resetqueue  &  /pause  &  /resume
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("resetqueue") & admin_filter)
async def cmd_resetqueue(client: Client, message: Message):
    counts = state.counts()
    total  = counts["total"]
    if total == 0:
        await message.reply("_Queue is already empty._")
        return
    active = counts["pending"] + counts["downloading"] + counts["downloaded"] + counts["uploading"]
    if active == 0:
        await message.reply("_No active tasks. Use `/clear` to remove finished ones._")
        return
    await message.reply(
        f"⚠️ Cancel all **{active}** active tasks?\n_(completed/failed stay in the log)_",
        reply_markup=kb_confirm("resetqueue"),
    )


@Client.on_message(filters.command("pause") & admin_filter)
async def cmd_pause(client: Client, message: Message):
    state.settings["paused"] = True
    state.settings["pause_reason"] = "manual"
    state.mark_dirty()
    await message.reply(
        "⏸ **Paused.**\nDownloads & uploads on hold — `/resume` to continue.",
        reply_markup=kb_processing(paused=True),
    )


@Client.on_message(filters.command("resume") & admin_filter)
async def cmd_resume(client: Client, message: Message):
    force = len(message.command or []) > 1 and message.command[1].casefold() == "force"
    cooldown = youtube_cooldown_remaining()
    if cooldown > 0 and not force:
        await message.reply(
            "🛑 **YouTube cooldown is still active**\n"
            f"Remaining: `{human_time_short(cooldown)}`\n\n"
            "Upload fresh `/cookies` to clear it, or wait before resuming. "
            "If you have fixed auth manually, use `/resume force`."
        )
        return
    if force:
        clear_youtube_cooldown()
    state.settings["paused"] = False
    state.settings.pop("pause_reason", None)
    state.mark_dirty()
    await message.reply(
        "▶️ **Resumed.** Back to work!",
        reply_markup=kb_processing(paused=False),
    )


# ---------------------------------------------------------------------------
# /cancel <video_id | url>
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("cancel") & admin_filter)
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
# /setparallel  &  /setquality
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
    await message.reply(f"⚡ Parallel downloads → `{n}` workers")


VALID_QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "audio")


@Client.on_message(filters.command(["setquality", "quality"]) & admin_filter)
async def cmd_setquality(client: Client, message: Message):
    args = message.text.split()
    current = state.settings.get("quality", "best")

    if len(args) < 2:
        await message.reply(
            f"❖ **𝗩𝗶𝗱𝗲𝗼 𝗤𝘂𝗮𝗹𝗶𝘁𝘆**\n"
            f"{SEP}\n"
            f"Current: {quality_label(current)}\n"
            f"_Pick a quality — applies to new tasks_",
            reply_markup=kb_quality(current),
        )
        return

    q = args[1].strip().lower()
    if q not in VALID_QUALITIES:
        await message.reply(
            f"❌ Unknown quality `{q}`.\nChoose from: `{'`, `'.join(VALID_QUALITIES)}`"
        )
        return

    state.settings["quality"] = q
    state.mark_dirty()
    await message.reply(
        f"🎞 Quality set → {quality_label(q)}\n_Applies to newly added tasks._",
        reply_markup=kb_quality(q),
    )


# ---------------------------------------------------------------------------
# /caption  &  /setname  &  /setusername — upload caption control
# ---------------------------------------------------------------------------

def _caption_panel_text() -> str:
    from core.uploader import get_caption_settings, signature_preview
    cfg = get_caption_settings(state)
    on  = lambda b: "✅ ON" if b else "❌ OFF"

    lines = [
        "❖ **𝗖𝗮𝗽𝘁𝗶𝗼𝗻 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀**",
        SEP,
        f"⋄ 📝 Captions: {on(cfg['enabled'])}",
        f"⋄ ⚡ Uploaded-by signature: {on(cfg['signature'])}",
        f"⋄ 👤 Name: `{cfg['name'] or '—'}`",
        f"⋄ 🔗 Username: `{('@' + cfg['username']) if cfg['username'] else '—'}`",
        f"⋄ 🆔 Show owner ID: {on(cfg['show_id'])}",
        SEP,
        "✏️ Set name: `/setname Your Name`",
        "🔗 Set username: `/setusername handle`",
        "_(or just `/setusername` to use yours)_",
    ]
    preview = signature_preview(cfg, video_num=1)
    if preview:
        lines += ["", "**Live preview:**", preview]
    return "\n".join(lines)


@Client.on_message(filters.command("caption") & admin_filter)
async def cmd_caption(client: Client, message: Message):
    args = message.text.split()
    if len(args) > 1 and args[1].lower() in ("on", "off", "enable", "disable"):
        enabled = args[1].lower() in ("on", "enable")
        state.settings["caption_enabled"] = enabled
        state.mark_dirty()
        from core.uploader import get_caption_settings
        await message.reply(
            f"📝 Upload captions → {'✅ ON' if enabled else '❌ OFF'}\n"
            + ("_Videos will upload with full info captions._" if enabled
               else "_Videos will upload **without** captions._"),
            reply_markup=kb_caption(get_caption_settings(state)),
        )
        return

    from core.uploader import get_caption_settings
    await message.reply(
        _caption_panel_text(),
        reply_markup=kb_caption(get_caption_settings(state)),
    )


@Client.on_message(filters.command("setname") & admin_filter)
async def cmd_setname(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        cur = state.settings.get("caption_name", "")
        await message.reply(
            f"Usage: `/setname KAL BABU`\n_Current name: `{cur or '—'}`_"
        )
        return
    name = args[1].strip()[:40]
    state.settings["caption_name"] = name
    state.mark_dirty()

    from core.uploader import get_caption_settings, signature_preview
    await message.reply(
        f"✅ Signature name set → **{md_escape(name)}**\n\n"
        f"**Preview:**\n{signature_preview(get_caption_settings(state))}"
    )


@Client.on_message(filters.command("setusername") & admin_filter)
async def cmd_setusername(client: Client, message: Message):
    args = message.text.split()
    uname = args[1].strip().lstrip("@") if len(args) > 1 else ""

    if not uname:
        # Auto-pick the sender's own username
        uname = (message.from_user.username or "") if message.from_user else ""
        if not uname:
            await message.reply(
                "❌ You have no Telegram username.\n"
                "Usage: `/setusername YourHandle`"
            )
            return

    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", uname):
        await message.reply("❌ Invalid username (5–32 chars: letters, numbers, `_`).")
        return

    state.settings["caption_username"] = uname
    state.mark_dirty()

    from core.uploader import get_caption_settings, signature_preview
    await message.reply(
        f"✅ Signature username set → @{uname}\n\n"
        f"**Preview:**\n{signature_preview(get_caption_settings(state))}"
    )


# ---------------------------------------------------------------------------
# /retryfailed  &  /clear
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("retryfailed") & admin_filter)
async def cmd_retryfailed(client: Client, message: Message):
    counts = state.counts()
    if counts.get("failed", 0) == 0:
        await message.reply("_No failed tasks to retry._ ✅")
        return
    n = state.retry_failed()
    await message.reply(
        f"🔁 Re-queued **{n}** failed task{'s' if n != 1 else ''}.\n"
        f"They'll start automatically — `/status` to watch."
    )


# chat_id (where the command was issued) -> pending destination awaiting confirm
_pending_dest: dict = {}


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
        f"All future uploads (unless watch-specific) will go here.",
        reply_markup=kb_confirm("setdest"),
    )


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


@Client.on_message(filters.command("channels") & admin_filter)
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


@Client.on_message(filters.command("destinfo") & user_filter)
async def cmd_destinfo(client: Client, message: Message):
    dest_id    = Config.DEST_CHAT_ID
    dest_title = state.settings.get("dest_chat_title", "—")
    watches    = state.all_watches()
    w_lines    = "\n".join(
        f"⋄ `{w.id}` {short(w.title or w.url, 26)} → "
        f"{short(w.dest_chat_title or (str(w.dest_chat_id) if w.dest_chat_id else 'global'), 22)}"
        for w in watches[:8]
    )
    watch_block = f"\n\n**👀 Watch routing:**\n{w_lines}" if watches else ""
    await message.reply(
        f"📍 **Global Destination**\n"
        f"{SEP}\n"
        f"⋄ 𝗡𝗮𝗺𝗲: **{dest_title}**\n"
        f"⋄ 𝗜𝗗: `{dest_id}`"
        f"{watch_block}\n\n"
        f"`/setchannel` to change global · `/watchdest` for per-watch."
    )


@Client.on_message(filters.command("clear") & admin_filter)
async def cmd_clear(client: Client, message: Message):
    c        = state.counts()
    finished = c.get("completed", 0) + c.get("failed", 0) + c.get("cancelled", 0) + c.get("skipped", 0)
    if finished == 0:
        await message.reply("_No finished tasks to clear._")
        return
    await message.reply(
        f"🗑 Clear `{finished}` finished tasks from the list?",
        reply_markup=kb_confirm("clear"),
    )


# ---------------------------------------------------------------------------
# YouTube URL handler (any registered user)
# ---------------------------------------------------------------------------

_scanned_items_cache: dict = {}

# Per-chat scan lock — prevents double-trigger when user sends URL rapidly
_scan_locks:     dict = {}   # chat_id -> asyncio.Lock
_scan_in_flight: dict = {}   # chat_id -> bool


def _get_scan_lock(chat_id: int):
    if chat_id not in _scan_locks:
        _scan_locks[chat_id] = asyncio.Lock()
    return _scan_locks[chat_id]


def _match_watch(meta: dict, url: str):
    """If this URL belongs to a watched channel, return that watch."""
    meta = meta or {}
    return state.watch_by_key(
        meta.get("channel_url", ""),
        meta.get("source_url", ""),
        url,
    )


def _fmt_total_dur(secs: int) -> str:
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _scan_metadata_caption(
    meta: dict,
    kind: str,
    filename: str,
    item_count: int,
    channel: str = "",
    total_secs: int = 0,
    date_range: str = "",
    quality: str = "",
) -> str:
    """Rich Telegram caption for the downloadable scan TXT (the 'outside')."""
    meta = meta or {}
    icon = "📋" if kind == "playlist" else ("📺" if kind == "channel" else "📹")
    ch_name  = channel or meta.get("channel_url", "") or "Unknown"
    verified = " ✔" if meta.get("verified") else ""
    subs     = meta.get("subscribers") or "—"
    pl_title = meta.get("playlist_title", "") or ch_name
    ch_url   = meta.get("channel_url", "")

    dur_s = _fmt_total_dur(total_secs) if total_secs else "—"

    lines = [
        f"{icon} **{pl_title}**{verified}",
        SEP,
        f"⋄ 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: **{ch_name}**",
        f"⋄ 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗯𝗲𝗿𝘀: `{subs}`",
        f"⋄ 𝗩𝗶𝗱𝗲𝗼𝘀: `{item_count}`",
        f"⋄ 𝗧𝗼𝘁𝗮𝗹 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: `{dur_s}`",
    ]
    if date_range:
        lines.append(f"⋄ 𝗗𝗮𝘁𝗲 𝗥𝗮𝗻𝗴𝗲: `{date_range}`")
    if quality:
        lines.append(f"⋄ 𝗤𝘂𝗮𝗹𝗶𝘁𝘆: {quality_label(quality)}")
    if ch_url:
        lines.append(f"⋄ 𝗨𝗥𝗟: {ch_url}")
    lines += [SEP, f"📄 `{filename}`"]
    return "\n".join(lines)


@Client.on_message(filters.regex(YT_PATTERN) & user_filter & filters.create(not_a_command))
async def on_youtube_url(client: Client, message: Message):
    # Find the actual YouTube URL token (message may contain extra words)
    tokens = (message.text or "").split()
    url = next((t for t in tokens if "youtu" in t.lower()), tokens[0] if tokens else "")
    kind = classify_url(url)
    chat_id = message.chat.id

    # ── Deduplication: skip if this chat is already scanning ───────────────
    lock = _get_scan_lock(chat_id)
    if _scan_in_flight.get(chat_id):
        await message.reply("⏳ _Already scanning, please wait…_")
        return
    _scan_in_flight[chat_id] = True
    try:
        async with lock:
            logger.info("Received %s URL from user %s: %s",
                        kind, message.from_user.id if message.from_user else "?", url)
            if kind == "video":
                await _handle_video(client, message, url)
            else:
                await _handle_scan(client, message, url, kind)
    finally:
        _scan_in_flight[chat_id] = False


async def _show_scan_error(status_msg: Message, exc: Exception) -> None:
    """Show an actionable auth error and globally pause on explicit blocks."""
    error = str(exc)
    if is_bot_detection_error(error):
        cooldown = register_youtube_block()
        state.settings["paused"] = True
        state.settings["pause_reason"] = "youtube_auth"
        state.mark_dirty()
        await status_msg.edit(
            "🛑 **YouTube blocked this scan**\n"
            f"Cooldown: `{human_time_short(cooldown)}`\n\n"
            "Nothing was queued. Upload fresh `/cookies`, then run `/authcheck`. "
            "For HTTP 429, wait 30–60 minutes instead of repeatedly retrying."
        )
        return
    await status_msg.edit(f"❌ Scan failed: `{md_escape(short(error, 300))}`")


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
        await _show_scan_error(status_msg, exc)
        return

    if not result["items"]:
        await status_msg.edit("❌ Could not fetch video info.")
        return

    item    = result["items"][0]
    title   = item.get("title", "Unknown")
    dur     = item.get("duration", "—")
    channel = result.get("channel") or "—"
    meta    = result.get("meta", {}) or {}
    cur_q   = state.settings.get("quality", "best")

    # Send a downloadable TXT with full metadata header.
    try:
        itms     = result["items"]
        txt_path = generate_txt(itms, channel, "video", meta, quality=cur_q)
        await client.send_document(
            chat_id=message.chat.id,
            document=str(txt_path),
            caption=_scan_metadata_caption(
                meta, "video", txt_path.name, len(itms), channel=channel,
                date_range=date_range_of(itms), quality=cur_q,
            ),
        )
    except Exception as exc:
        logger.warning("Could not send video TXT listing: %s", exc)

    # Auto-route to watch destination if this channel is being watched
    watch = _match_watch(meta, url)
    dest  = watch.dest_chat_id if watch else 0

    # Cache for action_start
    _scanned_items_cache[message.chat.id] = {
        "items":        result["items"],
        "channel":      channel,
        "kind":         "video",
        "meta":         meta,
        "dest_chat_id": dest,
        "watch_id":     watch.id if watch else "",
    }

    current_q = state.settings.get("quality", "best")
    q_label   = quality_label(current_q)
    watch_txt = (
        f"⋄ 👀 Watch: `{watch.id}` — routes to **{watch.dest_chat_title or watch.dest_chat_id}**\n"
        if watch else ""
    )

    await status_msg.edit(
        f"📹 **{title}**\n"
        f"{SEP}\n"
        f"⋄ 𝗖𝗵𝗮𝗻𝗻𝗲𝗹: {channel}\n"
        f"⋄ 𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻: `{dur}`\n"
        f"⋄ 𝗤𝘂𝗮𝗹𝗶𝘁𝘆: {q_label}\n"
        f"{watch_txt}"
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
        await _show_scan_error(status_msg, exc)
        return

    items   = result["items"]
    channel = result["channel"]
    meta    = result.get("meta", {})

    if not items:
        await status_msg.edit("❌ No videos found.")
        return

    # Send TXT immediately with full metadata in caption.
    cur_q = state.settings.get("quality", "best")
    try:
        txt_path = generate_txt(items, channel, kind, meta, quality=cur_q)
        await client.send_document(
            chat_id=message.chat.id,
            document=str(txt_path),
            caption=_scan_metadata_caption(
                meta, kind, txt_path.name, len(items),
                channel=channel,
                total_secs=total_duration_secs(items),
                date_range=date_range_of(items),
                quality=cur_q,
            ),
        )
    except Exception as exc:
        logger.warning("Could not send scan TXT listing: %s", exc)

    watch = _match_watch(meta, url)
    dest  = watch.dest_chat_id if watch else 0

    _scanned_items_cache[message.chat.id] = {
        "items":        items,
        "channel":      channel,
        "kind":         kind,
        "meta":         meta,
        "dest_chat_id": dest,
        "watch_id":     watch.id if watch else "",
    }

    icon      = "📋" if kind == "playlist" else "📺"
    current_q = state.settings.get("quality", "best")
    q_label   = quality_label(current_q)
    watch_txt = (
        f"⋄ 👀 Watch `{watch.id}` → **{watch.dest_chat_title or watch.dest_chat_id}**\n"
        if watch else ""
    )

    await status_msg.edit(
        f"{icon} **{channel or kind.title()}**\n"
        f"{SEP}\n"
        f"⋄ 𝗩𝗶𝗱𝗲𝗼𝘀: `{len(items)}`\n"
        f"⋄ 𝗤𝘂𝗮𝗹𝗶𝘁𝘆: {q_label}\n"
        f"{watch_txt}"
        f"{SEP}\n"
        f"_Pick sort order & quality, then Start_",
        reply_markup=kb_sort(),
    )


# ---------------------------------------------------------------------------
# Callback helpers
# ---------------------------------------------------------------------------

async def _refresh_dashboard_msg(cq: CallbackQuery) -> None:
    """Re-render the dashboard/status message a button lives on."""
    from bot import dashboard as dash_mod
    paused = state.settings.get("paused", False)
    try:
        txt = (cq.message.text or "")
        if "╭" in txt:  # live dashboard box → full re-render
            await cq.message.edit(
                dash_mod.format_dashboard(state),
                reply_markup=kb_processing(paused),
            )
            dash_mod._last_text = ""
        else:
            await cq.message.edit_reply_markup(kb_processing(paused))
    except Exception:
        pass


async def _cb_require(cq: CallbackQuery, *roles: str) -> bool:
    """Return True if cq.from_user has one of the roles, else deny + toast."""
    uid  = cq.from_user.id if cq.from_user else 0
    role = _role_of_user(uid) if uid else None
    if role in roles:
        return True
    await cq.answer("🔒 You don't have permission for this.", show_alert=True)
    return False


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------

@Client.on_callback_query(user_filter)
async def on_callback(client: Client, cq: CallbackQuery):
    data    = cq.data
    chat_id = cq.message.chat.id

    # --- noop ---
    if data == "noop":
        await cq.answer()
        return

    # ── YouTube auth / cookie controls ──────────────────────────────────
    if data == "auth_live_check":
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        await cq.answer("Contacting YouTube…")
        await _run_auth_live_check(cq.message)
        return

    if data == "auth_refresh":
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        waiting = bool(_pending_cookie_upload(chat_id))
        try:
            await cq.message.edit(
                _cookie_panel_text(chat_id),
                reply_markup=kb_auth(waiting=waiting),
            )
        except Exception:
            pass
        await cq.answer("Auth status refreshed")
        return

    if data == "cookie_ready":
        if not await _cb_require(cq, ROLE_OWNER):
            return
        _start_cookie_upload(chat_id, cq.message.id, cq.message.id)
        try:
            await cq.message.edit(
                _cookie_panel_text(chat_id),
                reply_markup=kb_auth(waiting=True),
            )
        except Exception:
            pass
        await cq.answer(
            "Now attach the Netscape .txt as a File/Document. Reply is optional.",
            show_alert=True,
        )
        return

    if data == "cookie_cancel":
        if not await _cb_require(cq, ROLE_OWNER):
            return
        _cookie_upload_requests.pop(chat_id, None)
        try:
            await cq.message.edit(
                _cookie_panel_text(chat_id),
                reply_markup=kb_auth(waiting=False),
            )
        except Exception:
            pass
        await cq.answer("Upload cancelled")
        return

    if data == "auth_manual_path":
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        await cq.message.reply(
            "📂 **Manual cookie location**\n"
            f"`{configured_cookie_path()}`\n\n"
            "Copy a fresh Netscape `cookies.txt` there. The next download "
            "detects it automatically — no restart required.\n"
            "⚠️ Never commit this file to GitHub."
        )
        await cq.answer()
        return

    if data == "cookie_help":
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        await cq.message.reply(
            "📖 **Cookie export checklist**\n"
            "1. Sign in to `youtube.com` in your browser.\n"
            "2. Use **Get cookies.txt LOCALLY**.\n"
            "3. Export the current site in **Netscape** format.\n"
            "4. In Telegram choose 📎 → **File**, then send the `.txt`.\n\n"
            "JSON, ZIP, screenshots and pasted cookie text are rejected."
        )
        await cq.answer()
        return

    # --- Sort ---
    if data.startswith("sort_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
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
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        await cq.message.edit_reply_markup(kb_quality(state.settings.get("quality", "best")))

    elif data.startswith("quality_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        q = data.replace("quality_", "")
        if q == "back":
            cached = _scanned_items_cache.get(chat_id)
            if cached:
                kind = cached.get("kind", "playlist")
                markup = kb_video() if kind == "video" else kb_sort()
                await cq.message.edit_reply_markup(markup)
            else:
                try:
                    await cq.message.edit_reply_markup(None)
                except Exception:
                    pass
            return
        state.settings["quality"] = q
        state.mark_dirty()
        await cq.answer(f"Quality: {quality_label(q)}")
        try:
            await cq.message.edit_reply_markup(kb_quality(q))
        except Exception:
            pass

    # ── Watch callbacks ─────────────────────────────────────────────────
    elif data.startswith("wtoggle_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        w = state.get_watch(data.replace("wtoggle_", ""))
        if not w:
            await cq.answer("Watch not found", show_alert=True)
            return
        w.enabled = not w.enabled
        state.mark_dirty()
        await cq.answer(f"{'🟢 Enabled' if w.enabled else '⏸ Paused'}: {w.title or w.id}")
        try:
            await cq.message.edit(_watchlist_text(), reply_markup=kb_watchlist(state.all_watches()))
        except Exception:
            pass

    elif data.startswith("winfo_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        w = state.get_watch(data.replace("winfo_", ""))
        if not w:
            await cq.answer("Watch not found", show_alert=True)
            return
        dest  = w.dest_chat_title or (str(w.dest_chat_id) if w.dest_chat_id else "global")
        ago   = human_time_short(time.time() - w.last_check) + " ago" if w.last_check else "never"
        sched = state.watch_schedule_label(w)
        q     = quality_label(w.quality) if w.quality else "global"
        await cq.answer(
            f"{short(w.title or w.url, 40)}\n"
            f"📍 {dest} · {sched}\n"
            f"🎞 Quality: {q} · 🎬 {len(w.known_ids)} known\n"
            f"✅ {w.checks} checks · last {ago}",
            show_alert=True,
        )

    elif data.startswith("wdel_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        w = state.remove_watch(data.replace("wdel_", ""))
        await cq.answer(f"🗑 Removed {w.title if w else ''}", show_alert=True)
        watches = state.all_watches()
        try:
            if watches:
                await cq.message.edit(_watchlist_text(), reply_markup=kb_watchlist(watches))
            else:
                await cq.message.edit(_watchlist_text())
        except Exception:
            pass

    elif data.startswith("wcheck_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        w = state.get_watch(data.replace("wcheck_", ""))
        if not w:
            await cq.answer("Watch not found", show_alert=True)
            return
        await cq.answer("🔍 Checking…")
        status = await cq.message.reply(f"🔍 Checking **{w.title or w.id}**…")
        try:
            new_items = await check_watch(state, w)
        except Exception as exc:
            await status.edit(f"❌ Check failed: `{short(str(exc), 100)}`")
            return
        state.save()
        if new_items:
            from core.watcher import notify_new_videos
            await notify_new_videos(client, state, w, new_items)
            await status.edit(f"🔔 **{len(new_items)}** new videos queued from **{md_escape(short(w.title, 30))}** ✅")
        else:
            await status.edit(f"✔️ No new videos on **{md_escape(short(w.title, 30))}**")

    elif data == "wcheckall":
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        await cq.answer("🔍 Checking all watches…")
        total = 0
        for w in [w for w in state.all_watches() if w.enabled]:
            try:
                new_items = await check_watch(state, w)
                if new_items:
                    total += len(new_items)
                    from core.watcher import notify_new_videos
                    await notify_new_videos(client, state, w, new_items)
            except Exception as exc:
                logger.warning("checkall error on %s: %s", w.id, exc)
            state.save()
        await cq.message.reply(f"🔍 All watches checked — `{total}` new videos queued.")

    elif data in ("watchlist_open", "watchlist_refresh"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        watches = state.all_watches()
        try:
            await cq.message.edit(
                _watchlist_text(),
                reply_markup=kb_watchlist(watches) if watches else None,
            )
        except Exception:
            pass
        await cq.answer("↻")

    elif data == "watch_this":
        """Turn the scanned channel/playlist into an auto-watch in one tap."""
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        cached = _scanned_items_cache.get(chat_id)
        if not cached or cached.get("kind") == "video":
            await cq.answer("Scan a channel or playlist first.", show_alert=True)
            return

        meta  = cached.get("meta", {}) or {}
        url   = meta.get("source_url", "") or meta.get("channel_url", "")
        key   = meta.get("channel_url", "") or url
        title = meta.get("playlist_title") or cached.get("channel") or "Unknown"

        if state.watch_by_key(key, url):
            await cq.answer("Already being watched — see /watchlist", show_alert=True)
            return

        wid   = state.next_watch_id()
        known = [it["id"] for it in cached["items"] if it.get("id")]
        uid   = cq.from_user.id if cq.from_user else 0
        state.add_watch(wid, url, key, title, known, added_by=uid)
        state.save()

        await cq.answer(f"👀 Watching {title}", show_alert=False)
        await cq.message.reply(
            f"✅ **𝗪𝗮𝘁𝗰𝗵 𝗔𝗰𝘁𝗶𝘃𝗲!**\n"
            f"{SEP}\n"
            f"⋄ 📺 Channel: **{md_escape(title)}**\n"
            f"⋄ 🆔 Watch ID: `{wid}`\n"
            f"⋄ 🎬 Known videos: `{len(known)}` _(not re-downloaded)_\n"
            f"⋄ 📍 Destination: **Global** (`/destinfo`)\n"
            f"{SEP}\n"
            f"🆕 Only **new uploads** will be auto-backed up.\n"
            f"⏰ `/watchtime {wid} 06:00` — daily 6 AM check\n"
            f"📍 `/watchdest {wid} <chat_id>` — separate destination",
            reply_markup=kb_watch_actions(wid),
        )

    # ── Caption settings callbacks (admin) ──────────────────────────────
    elif data.startswith("cap_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        which = data.replace("cap_", "")
        toast = "↻"
        if which == "main":
            state.settings["caption_enabled"] = not state.settings.get("caption_enabled", True)
            toast = "📝 Captions " + ("ON" if state.settings["caption_enabled"] else "OFF")
        elif which == "sig":
            state.settings["caption_signature"] = not state.settings.get("caption_signature", True)
            toast = "⚡ Signature " + ("ON" if state.settings["caption_signature"] else "OFF")
        elif which == "id":
            state.settings["caption_show_id"] = not state.settings.get("caption_show_id", False)
            toast = "🆔 Show ID " + ("ON" if state.settings["caption_show_id"] else "OFF")
        state.mark_dirty()

        from core.uploader import get_caption_settings
        try:
            await cq.message.edit(
                _caption_panel_text(),
                reply_markup=kb_caption(get_caption_settings(state)),
            )
        except Exception:
            pass
        await cq.answer(toast)

    # ── User management callbacks (owner) ───────────────────────────────
    elif data.startswith("urole_"):
        if not await _cb_require(cq, ROLE_OWNER):
            return
        uid = int(data.replace("urole_", ""))
        cur = state.role_of(uid)
        new = ROLE_USER if cur == ROLE_ADMIN else ROLE_ADMIN
        if state.set_role(uid, new):
            state.save()
            await cq.answer(f"{ROLE_ICON[new]} {new.title()}")
            try:
                await cq.message.edit(_users_text(), reply_markup=kb_users(state.all_users()))
            except Exception:
                pass
        else:
            await cq.answer("User not found", show_alert=True)

    elif data.startswith("udel_"):
        if not await _cb_require(cq, ROLE_OWNER):
            return
        uid = int(data.replace("udel_", ""))
        if state.remove_user(uid):
            state.save()
            await cq.answer("🗑 Removed", show_alert=True)
            try:
                await cq.message.edit(_users_text(), reply_markup=kb_users(state.all_users()))
            except Exception:
                pass
        else:
            await cq.answer("User not found", show_alert=True)

    elif data == "users_refresh":
        if not await _cb_require(cq, ROLE_OWNER):
            return
        try:
            await cq.message.edit(_users_text(), reply_markup=kb_users(state.all_users()))
        except Exception:
            pass
        await cq.answer("↻")

    # --- Actions ---
    elif data.startswith("action_"):
        action = data.replace("action_", "")

        if action == "start":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN, ROLE_USER):
                return
            cached = _scanned_items_cache.get(chat_id)
            if not cached:
                await cq.answer("Session expired. Resend the URL.", show_alert=True)
                return
            quality = state.settings.get("quality", "best")
            uid     = cq.from_user.id if cq.from_user else 0
            added, _ = state.add_tasks(
                cached["items"], source=cached["kind"], quality=quality,
                dest_chat_id=cached.get("dest_chat_id", 0), added_by=uid,
            )
            _scanned_items_cache.pop(chat_id, None)

            q_label = quality_label(quality)
            kind    = cached.get("kind", "video")
            icon    = "📹" if kind == "video" else ("📋" if kind == "playlist" else "📺")

            try:
                await cq.message.edit_reply_markup(
                    kb_processing(paused=state.settings.get("paused", False))
                )
            except Exception:
                pass
            await cq.answer(
                f"{icon} {added} added · {q_label}",
                show_alert=(added == 0),
            )

        elif action == "pause":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            state.settings["paused"] = True
            state.settings["pause_reason"] = "manual"
            state.mark_dirty()
            await cq.answer("⏸ Paused")
            await _refresh_dashboard_msg(cq)

        elif action == "resume":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            cooldown = youtube_cooldown_remaining()
            if cooldown > 0:
                await cq.answer(
                    f"YouTube cooldown: {human_time_short(cooldown)} left. Upload fresh cookies.",
                    show_alert=True,
                )
                return
            state.settings["paused"] = False
            state.settings.pop("pause_reason", None)
            state.mark_dirty()
            await cq.answer("▶️ Resumed")
            await _refresh_dashboard_msg(cq)

        elif action == "refresh":
            await cq.answer("↻")
            await _refresh_dashboard_msg(cq)

        elif action == "refresh_tasks":
            active_t, other_t = _ordered_tasks()
            ordered = active_t + other_t
            if ordered:
                try:
                    await cq.message.edit_reply_markup(kb_tasks_page(ordered, page=0))
                except Exception:
                    pass
                await cq.answer("↻ Refreshed")
            else:
                await cq.answer("Queue is empty", show_alert=True)

        elif action == "cancel":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            # Discard cached items (used when user taps Discard on scan result)
            _scanned_items_cache.pop(chat_id, None)
            removed = 0
            for t in list(state.all_tasks()):
                if t.status in ACTIVE_STATUSES:
                    trigger_cancel(t.id)
                    state.cancel_and_remove(t.id)
                    removed += 1
            if removed:
                await cq.answer(f"🚫 {removed} tasks removed", show_alert=True)
            else:
                await cq.answer("✖️ Discarded")

        elif action == "auth":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            waiting = bool(_pending_cookie_upload(chat_id))
            await cq.message.reply(
                _cookie_panel_text(chat_id),
                reply_markup=kb_auth(waiting=waiting),
            )
            await cq.answer("YouTube auth panel")

        elif action == "help":
            await cq.message.reply(START_TEXT, reply_markup=kb_start())
            await cq.answer("Command guide")

        elif action == "status":
            c = state.counts()
            await cq.answer(
                f"✅{c['completed']} ❌{c['failed']} ⏳{c['pending']} ⬇{c['downloading']} 📤{c['uploading']}",
                show_alert=True,
            )

        elif action == "tasks":
            active_t, other_t = _ordered_tasks()
            ordered = active_t + other_t
            if not ordered:
                await cq.answer("Queue is empty", show_alert=True)
                return
            text = (
                f"❖ **𝗧𝗮𝘀𝗸 𝗟𝗶𝘀𝘁**\n"
                f"{SEP}\n"
                f"⋄ Active: `{len(active_t)}`  ·  Total: `{len(ordered)}`\n"
                f"_Tap ℹ️ for details · ❌ to cancel_"
            )
            await cq.message.reply(text, reply_markup=kb_tasks_page(ordered, page=0))
            await cq.answer()

        elif action == "stats":
            await cq.message.reply(_stats_text())
            await cq.answer()

        elif action == "dashboard":
            from bot import dashboard as dash_mod
            paused = state.settings.get("paused", False)
            msg = await cq.message.reply(
                dash_mod.format_dashboard(state),
                reply_markup=kb_processing(paused),
            )
            dash_mod.dashboard_msg_id   = msg.id
            dash_mod._dashboard_chat_id = cq.message.chat.id
            dash_mod._last_text         = ""
            await cq.answer("📊 Live dashboard")

        elif action == "speedtest":
            await cq.answer("🌐 Starting…")
            wait_msg = await cq.message.reply("🌐 _Running speed test (25 MB download)…_")
            result   = await run_speedtest()
            await wait_msg.edit(result)

        elif action == "diskspace":
            await cq.answer(sys_disk_report().replace("`", "")[:200], show_alert=True)

        elif action == "serverinfo":
            await cq.answer(sys_server_report().replace("`", "")[:200], show_alert=True)

        elif action == "channels":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
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
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
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
        await cq.answer(f"✅ Switched to {entry.get('title', target_id)}")

    # --- Per-task cancel ---
    elif data.startswith("cancel_task_"):
        if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
            return
        vid   = data.replace("cancel_task_", "")
        task  = state.get(vid)
        title = short((task.title if task else None) or vid, 30)
        trigger_cancel(vid)
        removed = state.cancel_and_remove(vid)
        if removed:
            await cq.answer(f"🚫 Removed: {title}", show_alert=True)
        else:
            await cq.answer("Task not found", show_alert=True)

        active_t, other_t = _ordered_tasks()
        ordered = active_t + other_t
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
            q   = quality_label(getattr(task, "quality", "best"))
            err = f"\n⚠️ {task.error[:100]}" if task.error else ""
            dest = f" → {task.dest_chat_id}" if task.dest_chat_id else ""
            await cq.answer(
                f"{short(task.title, 40)}{dur}\n{task.status} · {q}{dest}{err}",
                show_alert=True,
            )
        else:
            await cq.answer("Task not found", show_alert=True)

    # --- Tasks pagination ---
    elif data.startswith("tasks_page_"):
        page     = int(data.replace("tasks_page_", ""))
        active_t, other_t = _ordered_tasks()
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
        if not await _cb_require(cq, ROLE_OWNER):
            return
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
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            removed = state.reset_finished()
            await cq.message.edit(f"✅ Cleared `{removed}` finished tasks.")
            await cq.answer()

        elif action == "resetqueue":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            removed = 0
            for t in list(state.all_tasks()):
                if t.status in ACTIVE_STATUSES:
                    trigger_cancel(t.id)
                    state.cancel_and_remove(t.id)
                    removed += 1
            await cq.message.edit(f"🚫 `{removed}` tasks removed from queue.")
            await cq.answer("Done!")

        elif action == "setdest":
            if not await _cb_require(cq, ROLE_OWNER):
                return
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


# ---------------------------------------------------------------------------
# Catch-all — MUST stay registered LAST (very end of this file)
# ---------------------------------------------------------------------------
# Pyrogram's dispatcher runs the FIRST handler whose filter matches in a group
# and skips the rest. This catch-all was previously registered right after
# /start, so it swallowed EVERY private-chat text message that wasn't /start
# or /help — meaning all commands (and YouTube links) sent to the bot's DM
# silently did nothing. Handlers here are registered in file order, so this
# nudge only fires when no other handler claimed the message.

@Client.on_message(
    filters.private & filters.text
    & ~filters.command(["start", "help"])
    & ~user_filter
)
async def on_unregistered(client: Client, message: Message):
    """Unregistered users in private chat get a polite nudge."""
    uid = message.from_user.id if message.from_user else 0
    if uid:
        await message.reply(
            "🔒 You're not registered with this bot.\n"
            f"Ask the owner to run `/adduser` (replying to your message).\n\n"
            f"Your user ID: `{uid}`"
        )
