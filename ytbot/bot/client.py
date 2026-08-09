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
    BotCommand("start", "Welcome + instructions"),
    BotCommand("status", "Current queue status"),
    BotCommand("tasks", "Task list with cancel buttons"),
    BotCommand("pause", "Pause all processing"),
    BotCommand("resume", "Resume processing"),
    BotCommand("cancel", "Cancel a video by ID/URL"),
    BotCommand("resetqueue", "Remove ALL active tasks"),
    BotCommand("setparallel", "Change parallel downloads (1-5)"),
    BotCommand("clear", "Clear completed/failed tasks"),
    BotCommand("setchannel", "Set upload destination (this chat or a chat_id)"),
    BotCommand("channels", "Saved destinations — tap to switch"),
    BotCommand("destinfo", "Show current upload destination"),
    BotCommand("diskspace", "Disk usage report"),
    BotCommand("serverinfo", "RAM, CPU, uptime"),
    BotCommand("logs", "Last 40 log lines"),
    BotCommand("cookies", "Upload cookies.txt for YouTube auth"),
    BotCommand("authstatus", "Check YouTube auth state"),
    BotCommand("ytdlpupdate", "Update yt-dlp to latest version"),
    BotCommand("purge", "Delete last N messages from this chat"),
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
