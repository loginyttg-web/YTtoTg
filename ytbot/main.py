"""
YouTube to Telegram Backup Bot — Entry Point.
"""

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from datetime import datetime

# ── Ensure node.js is on PATH so yt-dlp can solve YouTube's n-challenge ──
_node_bin = shutil.which("node")
if _node_bin:
    _node_dir = os.path.dirname(_node_bin)
    _cur_path = os.environ.get("PATH", "")
    if _node_dir not in _cur_path:
        os.environ["PATH"] = _node_dir + os.pathsep + _cur_path

from config import Config
from utils.logger import setup_logging
from utils.helpers import human_bytes
from core.state import StateManager
from core.system import cleanup_temp, disk_report, is_disk_alert, folder_size
from core.downloader import download_worker, get_bot_detection_alerted, reset_bot_alert
from core.uploader import upload_worker
from core.auth import bot_detection_help
from bot.client import create_app, set_bot_commands
from bot.dashboard import dashboard_loop
from bot.handlers import setup as handlers_setup

logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
stop_event = asyncio.Event()
state = StateManager(Config.DATA_DIR / "state.json")


# ---------------------------------------------------------------------------
# Daily report scheduler
# ---------------------------------------------------------------------------

async def daily_report_scheduler(app) -> None:
    """Send daily summary to OWNER_ID at configured hour."""
    if not Config.DAILY_REPORT:
        logger.info("Daily report disabled")
        return

    logger.info("Daily report scheduler enabled (hour=%d)", Config.DAILY_REPORT_HOUR)

    while not stop_event.is_set():
        now = datetime.now()
        target = now.replace(
            hour=Config.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0
        )
        if now >= target:
            from datetime import timedelta
            target = target + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info("Next daily report in %.1f hours", wait_seconds / 3600)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=min(wait_seconds, 3600))
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        await _send_daily_report(app)


async def _send_daily_report(app) -> None:
    """Compose and send the daily summary."""
    stats = state.stats

    lines = [
        f"📊 **Daily Summary Report — {datetime.now().strftime('%d %b %Y')}**",
        "",
        f"✅ Completed: `{stats['completed']}` videos",
        f"❌ Failed: `{stats['failed']}` videos",
        f"⏭️ Skipped: `{stats['skipped']}` videos",
        f"📦 Total Size Uploaded: `{human_bytes(stats['bytes_uploaded'])}`",
        f"⏱️ Total Time: `{_fmt_time(stats['total_time'])}`",
    ]

    du = disk_report()
    lines.append(f"💾 Disk Space: `{du}`")

    # Failed videos list
    failed = stats.get("failed_list", [])
    if failed:
        lines.append("\n**Failed Videos:**")
        for i, fv in enumerate(failed[-10:], 1):
            title = fv.get("title", "Unknown")
            error = fv.get("error", "")
            lines.append(f"{i}. \"{title}\" — `{error[:80]}`")

    try:
        await app.send_message(
            chat_id=Config.OWNER_ID,
            text="\n".join(lines),
        )
        logger.info("Daily report sent")
    except Exception as exc:
        logger.error("Failed to send daily report: %s", exc)


def _fmt_time(total_seconds: float) -> str:
    if total_seconds < 60:
        return f"{int(total_seconds)}s"
    h, rem = divmod(int(total_seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


# ---------------------------------------------------------------------------
# Auto-cleanup loop
# ---------------------------------------------------------------------------

async def auto_cleanup_loop() -> None:
    """Periodically clean up stale temp files."""
    while not stop_event.is_set():
        await asyncio.sleep(1800)  # every 30 minutes
        count = cleanup_temp(older_than_hours=6)
        if count:
            logger.info("Auto-cleanup: removed %d stale files", count)


# ---------------------------------------------------------------------------
# Bot-detection alert loop
# ---------------------------------------------------------------------------

async def bot_detection_alert_loop(app) -> None:
    """Monitor the global bot-detection flag and alert the owner once."""
    while not stop_event.is_set():
        await asyncio.sleep(5)
        if get_bot_detection_alerted():
            try:
                await app.send_message(
                    chat_id=Config.OWNER_ID,
                    text=(
                        "🛑 **YouTube Bot Detection Triggered!**\n\n"
                        "Downloads have been **paused** to avoid further blocks.\n\n"
                        + bot_detection_help()
                        + "\n\nRun `/resume` after fixing the issue."
                    ),
                )
                reset_bot_alert()
                logger.info("Bot-detection alert sent to owner")
            except Exception as exc:
                logger.error("Failed to send bot-detection alert: %s", exc)


# ---------------------------------------------------------------------------
# Disk alert loop
# ---------------------------------------------------------------------------

async def disk_alert_loop(app) -> None:
    """Send alert to owner if disk exceeds threshold."""
    alerted = False
    while not stop_event.is_set():
        await asyncio.sleep(300)  # every 5 minutes
        if is_disk_alert():
            if not alerted:
                try:
                    await app.send_message(
                        chat_id=Config.OWNER_ID,
                        text=(
                            "⚠️ **Disk Alert!**\n\n"
                            + disk_report()
                            + "\n\nConsider running /clear or freeing space."
                        ),
                    )
                    alerted = True
                except Exception as exc:
                    logger.error("Failed to send disk alert: %s", exc)
        else:
            alerted = False


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _handle_signal(sig, frame):
    logger.info("Signal %s received, shutting down...", sig)
    stop_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _send_startup_ping(app) -> None:
    """Send startup notification to owner and dest channel, delete after 10s."""
    from datetime import datetime
    text = (
        "🟢 **YouTube Backup Bot Started!**\n"
        f"🕐 `{datetime.now().strftime('%d %b %Y %H:%M:%S')}`\n"
        f"⚙️ Workers: `{Config.PARALLEL_DOWNLOADS}` | Quality: `{Config.DEFAULT_QUALITY}`"
    )
    targets = list({Config.OWNER_ID, Config.DEST_CHAT_ID})
    msgs = []
    for chat_id in targets:
        try:
            m = await app.send_message(chat_id=chat_id, text=text)
            msgs.append(m)
            logger.info("Startup ping sent to %d", chat_id)
        except Exception as exc:
            logger.warning("Startup ping failed for %d: %s", chat_id, exc)
    if msgs:
        await asyncio.sleep(10)
        for m in msgs:
            try:
                await m.delete()
            except Exception:
                pass


def _auto_update_ytdlp() -> None:
    """Update yt-dlp to latest version on startup (non-blocking)."""
    import subprocess, shutil
    # Prefer the pip3/pip in PATH; fall back to sys.executable -m pip
    pip_cmd = shutil.which("pip3") or shutil.which("pip")
    cmd = [pip_cmd, "install", "--upgrade", "--break-system-packages", "yt-dlp"] if pip_cmd else \
          [sys.executable, "-m", "pip", "install", "--upgrade", "--break-system-packages", "yt-dlp"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            import yt_dlp
            version = getattr(yt_dlp, "__version__", "unknown")
            logger.info("yt-dlp auto-updated to %s", version)
        else:
            logger.warning("yt-dlp auto-update failed: %s", result.stderr[-200:])
    except Exception as exc:
        logger.warning("yt-dlp auto-update error: %s", exc)


async def main() -> None:
    """Main entry point."""
    # 1. Validate config
    errors = Config.validate()
    if errors:
        print("❌ Configuration errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Auto-update yt-dlp in background thread
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _auto_update_ytdlp)

    # 3. Load state
    state.load()
    handlers_setup(state, stop_event)

    # Apply saved dest_chat_id override (set via /setchannel)
    saved_dest = state.settings.get("dest_chat_id", 0)
    if saved_dest:
        Config.DEST_CHAT_ID = int(saved_dest)
        logger.info("Applied saved dest_chat_id: %d", Config.DEST_CHAT_ID)

    # 4. Signal handlers
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # 5. Auto-load cookies.txt if present and not already set
    _cookies_default = Config.DATA_DIR / "cookies.txt"
    if not Config.COOKIES_PATH and _cookies_default.exists():
        Config.COOKIES_PATH = str(_cookies_default)
        logger.info("Auto-loaded cookies from %s", _cookies_default)

    # 6. Create Pyrogram client
    app = create_app()

    # 7. Start & register commands
    await app.start()
    await set_bot_commands(app)


    logger.info("🤖 Bot started! Owner: %d  Dest: %d", Config.OWNER_ID, Config.DEST_CHAT_ID)

    # Startup ping — send to owner & dest channel, auto-delete after 10s
    await _send_startup_ping(app)

    # 6. Launch background tasks
    semaphore = asyncio.Semaphore(Config.PARALLEL_DOWNLOADS)
    background_tasks: list[asyncio.Task] = []

    # State autosave
    background_tasks.append(asyncio.create_task(state.autosave_loop(15, stop_event)))

    # Download workers
    for w_id in range(Config.PARALLEL_DOWNLOADS):
        background_tasks.append(
            asyncio.create_task(download_worker(w_id + 1, semaphore, stop_event, state))
        )

    # Upload worker (single, sequential)
    background_tasks.append(
        asyncio.create_task(upload_worker(app, stop_event, state))
    )

    # Dashboard
    background_tasks.append(
        asyncio.create_task(dashboard_loop(app, stop_event, state))
    )

    # Daily report
    if Config.DAILY_REPORT:
        background_tasks.append(
            asyncio.create_task(daily_report_scheduler(app))
        )

    # Auto cleanup
    background_tasks.append(asyncio.create_task(auto_cleanup_loop()))

    # Disk alerts
    background_tasks.append(asyncio.create_task(disk_alert_loop(app)))

    # Bot-detection alerts
    background_tasks.append(asyncio.create_task(bot_detection_alert_loop(app)))

    # 7. Wait for shutdown signal
    try:
        while not stop_event.is_set():
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested")

    # 8. Graceful shutdown
    logger.info("Shutting down...")
    stop_event.set()

    # Save state one last time
    state.save()
    logger.info("Final state saved")

    # Cancel background tasks
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)

    # Stop client
    await app.stop()
    logger.info("Bot stopped")


if __name__ == "__main__":
    # Setup logging
    setup_logging(Config.DATA_DIR)
    logger.info("=" * 50)
    logger.info("YouTube Backup Bot starting...")
    logger.info("=" * 50)

    try:
        asyncio.run(main())
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
