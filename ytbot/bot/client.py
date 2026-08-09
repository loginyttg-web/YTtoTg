"""
Pyrogram Client initialisation and bot-command registration.
"""

import logging
from typing import List

from pyrogram import Client, enums
from pyrogram.types import BotCommand

from config import Config

logger = logging.getLogger("client")

# ---------------------------------------------------------------------------
# Command definitions for Telegram auto-complete menu
# ---------------------------------------------------------------------------
BOT_COMMANDS: List[BotCommand] = [
    BotCommand("start", "Welcome + all commands"),
    BotCommand("status", "Queue status at a glance"),
    BotCommand("dashboard", "Live progress panel"),
    BotCommand("tasks", "Task list with cancel buttons"),
    BotCommand("stats", "Session statistics"),
    # 👀 Auto-watch
    BotCommand("watch", "Auto-backup a channel's NEW uploads"),
    BotCommand("watchlist", "All watched channels"),
    BotCommand("unwatch", "Stop watching a channel"),
    BotCommand("checknow", "Check watches for new videos now"),
    BotCommand("backfill", "Queue ALL videos of a watched channel"),
    BotCommand("watchdest", "Change a watch's destination"),
    BotCommand("watchquality", "Quality override for one watch"),
    BotCommand("watchtime", "Daily check time (e.g. 06:00)"),
    BotCommand("watchinterval", "Check interval (12h=720, 24h=1440)"),
    BotCommand("watchpause", "Pause the watcher"),
    BotCommand("watchresume", "Resume the watcher"),
    # 👥 Users
    BotCommand("adduser", "Grant bot access (reply or user_id)"),
    BotCommand("removeuser", "Revoke bot access"),
    BotCommand("setrole", "Change a user's role (admin/user)"),
    BotCommand("users", "List all authorised users"),
    BotCommand("whoami", "Show my role & permissions"),
    # 📥 Queue
    BotCommand("pause", "Pause all processing"),
    BotCommand("resume", "Resume processing"),
    BotCommand("cancel", "Cancel a video by ID/URL"),
    BotCommand("retryfailed", "Re-queue failed tasks"),
    BotCommand("resetqueue", "Remove ALL active tasks"),
    BotCommand("clear", "Clear finished tasks"),
    BotCommand("setquality", "Video quality (best/1080/720/480/audio)"),
    BotCommand("setparallel", "Parallel downloads (1-5)"),
    BotCommand("setchannel", "Set upload destination"),
    BotCommand("channels", "Saved destinations — tap to switch"),
    BotCommand("destinfo", "Show current upload destination"),
    BotCommand("serverinfo", "CPU, RAM, disk, uptime"),
    BotCommand("diskspace", "Disk usage report"),
    BotCommand("speedtest", "Internet speed test"),
    BotCommand("logs", "Last log lines (optional level/count)"),
    BotCommand("cookies", "Upload cookies.txt for YouTube auth"),
    BotCommand("authstatus", "Check YouTube auth state"),
    BotCommand("ytdlpupdate", "Update yt-dlp to latest"),
    BotCommand("purge", "Delete last N uploads from dest chat"),
]


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def create_app() -> Client:
    """Create and return a configured Pyrogram Client instance."""

    app = Client(
        "ytbackup_bot",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN,
        workdir=str(Config.DATA_DIR),
        plugins={"root": "bot", "include": ["handlers"]},
        parse_mode=enums.ParseMode.MARKDOWN,
        max_concurrent_transmissions=3,
    )

    return app


# ---------------------------------------------------------------------------
# Register commands with Telegram
# ---------------------------------------------------------------------------

async def set_bot_commands(app: Client) -> None:
    """Sync bot command list so it shows in Telegram's suggestion menu."""
    try:
        await app.set_bot_commands(BOT_COMMANDS)
        logger.info("Bot commands registered (%d commands)", len(BOT_COMMANDS))
    except Exception as exc:
        logger.error("Failed to set bot commands: %s", exc)
