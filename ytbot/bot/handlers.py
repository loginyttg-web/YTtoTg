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
import re
import time
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import Config, quality_label
from core.scraper import scan, sort_items, generate_txt
from core.state import (
    StateManager, PENDING, ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_ICON,
)
from core.system import (
    disk_report as sys_disk_report,
    server_report as sys_server_report,
    run_speedtest,
)
from core.auth import auth_status, bot_detection_help
from core.downloader import reset_bot_alert, trigger_cancel
from core.watcher import check_watch
from utils.helpers import (
    human_bytes, human_time, human_time_short, short, md_escape,
    parse_video_id, classify_url, styled_progress_bar, bar_smooth, SEP,
)
from utils.logger import tail_log
from bot.keyboards import (
    kb_sort, kb_quality, kb_processing, kb_confirm, kb_start,
    kb_tasks_page, kb_video, kb_channels,
    kb_watch_actions, kb_watchlist, kb_users,
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

yt_filter = filters.create(yt_url)

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
    "`/resetqueue` · `/clear` · `/retryfailed`\n\n"
    "**👥 Users** (owner)\n"
    "`/adduser` · `/removeuser` · `/setrole` · `/users`\n"
    "`/whoami` — check your role\n\n"
    "**⚙️ Settings**\n"
    "`/setquality <best|1080|720|480|audio>`\n"
    "`/setparallel <1-5>` · `/watchinterval <min>`\n"
    "`/setchannel [id]` · `/channels` · `/destinfo`\n\n"
    "**🖥 System**\n"
    "`/serverinfo` · `/diskspace` · `/speedtest`\n"
    "`/logs [n|level]` · `/purge <n>`\n\n"
    "**🔐 Auth**\n"
    "`/cookies` · `/authstatus` · `/ytdlpupdate`"
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
    extra = ""
    if role == ROLE_USER:
        extra = f"\n\n👤 Your role: **User** — you can submit links & view status."
    elif role == ROLE_ADMIN:
        extra = f"\n\n🛡 Your role: **Admin** — you can manage watches & queue."
    await message.reply(START_TEXT + extra, reply_markup=kb_start())


# Catch-all: unregistered users in private chat get a polite nudge
@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def on_unregistered(client: Client, message: Message):
    uid = message.from_user.id if message.from_user else 0
    if uid and _role_of_user(uid) is None:
        await message.reply(
            "🔒 You're not registered with this bot.\n"
            f"Ask the owner to run `/adduser` (replying to your message).\n\n"
            f"Your user ID: `{uid}`"
        )


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

    text = (
        f"❖ **𝗤𝘂𝗲𝘂𝗲 𝗦𝘁𝗮𝘁𝘂𝘀**\n"
        f"{SEP}\n"
        f"{dot} {stat}   ⚡ `{pq}` workers   🎞 {quality_label(q)}\n\n"
        f"{summary}\n\n"
        f"✅ `{c['completed']}`   ❌ `{c['failed']}`   ⏳ `{c['pending']}`\n"
        f"⬇️ `{c['downloading']}`   📤 `{c['uploading']}`   🚫 `{c['cancelled']}`\n\n"
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
    state.mark_dirty()
    await message.reply("⏸ **Watcher paused** — no auto-checks until `/watchresume`.")


@Client.on_message(filters.command("watchresume") & admin_filter)
async def cmd_watchresume(client: Client, message: Message):
    state.settings["watcher_paused"] = False
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
    wait_msg = await message.reply("🌐 _Running speed test (25 MB download)…_")
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

@Client.on_message(filters.command("cookies") & owner_filter)
async def cmd_cookies(client: Client, message: Message):
    await message.reply(
        f"❖ **𝗨𝗽𝗹𝗼𝗮𝗱 𝗖𝗼𝗼𝗸𝗶𝗲𝘀**\n"
        f"{SEP}\n"
        f"1️⃣ Install _Get cookies.txt LOCALLY_ extension\n"
        f"2️⃣ Open YouTube while logged in\n"
        f"3️⃣ Export `cookies.txt` from the extension\n"
        f"4️⃣ **Reply to this message** with the file\n\n"
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
# /authstatus  &  /ytdlpupdate
# ---------------------------------------------------------------------------

@Client.on_message(filters.command("authstatus") & admin_filter)
async def cmd_authstatus(client: Client, message: Message):
    s         = auth_status()
    help_text = bot_detection_help() if "No auth" in s else ""
    await message.reply(
        f"❖ **𝗔𝘂𝘁𝗵 𝗦𝘁𝗮𝘁𝘂𝘀**\n{SEP}\n{s}\n\n{help_text}".rstrip()
    )


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
    state.mark_dirty()
    await message.reply(
        "⏸ **Paused.**\nDownloads & uploads on hold — `/resume` to continue.",
        reply_markup=kb_processing(paused=True),
    )


@Client.on_message(filters.command("resume") & admin_filter)
async def cmd_resume(client: Client, message: Message):
    state.settings["paused"] = False
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


@Client.on_message(filters.regex(YT_PATTERN) & user_filter)
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
            logger.info("Received %s URL from user %s: %s",
                        kind, message.from_user.id if message.from_user else "?", url)
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
    meta    = result.get("meta", {}) or {}

    # Send a downloadable TXT with full metadata header.
    try:
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
            state.mark_dirty()
            await cq.answer("⏸ Paused")
            await _refresh_dashboard_msg(cq)

        elif action == "resume":
            if not await _cb_require(cq, ROLE_OWNER, ROLE_ADMIN):
                return
            state.settings["paused"] = False
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
