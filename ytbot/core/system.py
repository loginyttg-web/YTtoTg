"""
System monitoring: disk, RAM, CPU, speedtest, cleanup.
"""

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Dict

import psutil

from config import Config
from utils.helpers import human_bytes

logger = logging.getLogger("system")


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def disk_usage(path: Path = None) -> Dict[str, float]:
    """Return {total, used, free, percent} for the filesystem of *path*."""
    p = str(path or Config.DOWNLOAD_DIR)
    du = shutil.disk_usage(p)
    return {
        "total": du.total,
        "used": du.used,
        "free": du.free,
        "percent": round(du.used / du.total * 100, 1) if du.total > 0 else 0,
    }


def folder_size(path: Path) -> int:
    """Total size in bytes of all files in path (recursive)."""
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def has_space_for(size_bytes: int) -> bool:
    """Check if free space > (size * 1.15 + safety_margin)."""
    required = int(size_bytes * 1.15 + Config.safety_margin_bytes())
    du = disk_usage()
    ok = du["free"] > required
    if not ok:
        logger.warning(
            "Not enough space: need %s, have %s free",
            human_bytes(required), human_bytes(du["free"]),
        )
    return ok


def disk_report() -> str:
    """Formatted disk report for /diskspace command."""
    du = disk_usage()
    used_folder = folder_size(Config.DOWNLOAD_DIR)
    alert = "⚠️ HIGH USAGE!" if du["percent"] > Config.DISK_ALERT_PERCENT else ""

    return (
        f"💾 **Disk Usage** {alert}\n\n"
        f"Total: `{human_bytes(du['total'])}`\n"
        f"Used:  `{human_bytes(du['used'])}` ({du['percent']}%)\n"
        f"Free:  `{human_bytes(du['free'])}`\n\n"
        f"📂 Download folder: `{human_bytes(used_folder)}`\n"
        f"🛡️ Safety margin: `{human_bytes(Config.safety_margin_bytes())}`"
    )


def is_disk_alert() -> bool:
    """Return True if disk usage exceeds alert threshold."""
    du = disk_usage()
    return du["percent"] > Config.DISK_ALERT_PERCENT


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------

def server_report() -> str:
    """RAM, CPU, uptime for /serverinfo."""
    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = disk_usage()

    boot = psutil.boot_time()
    uptime_seconds = time.time() - boot
    days, rem = divmod(int(uptime_seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)

    return (
        f"🖥 **Server Info**\n\n"
        f"💻 CPU: `{psutil.cpu_percent(interval=0.5)}%` used "
        f"({psutil.cpu_count(logical=True)} logical cores)\n"
        f"🧠 RAM: `{human_bytes(ram.used)} / {human_bytes(ram.total)}` "
        f"({ram.percent}%)\n"
        f"📀 Swap: `{human_bytes(swap.used)} / {human_bytes(swap.total)}` "
        f"({swap.percent}%)\n"
        f"💾 Disk: `{human_bytes(disk['used'])} / {human_bytes(disk['total'])}` "
        f"({disk['percent']}%)\n"
        f"⏱ Uptime: `{days}d {hours}h {mins}m {secs}s`"
    )


# ---------------------------------------------------------------------------
# Speedtest
# ---------------------------------------------------------------------------

SPEEDTEST_FILES = [
    "https://speed.cloudflare.com/__down?bytes=104857600",   # 100 MB
    "https://speed.cloudflare.com/__down?bytes=26214400",    # 25 MB
]


async def run_speedtest() -> str:
    """Download speed test using Cloudflare endpoint. Returns formatted report."""
    import aiohttp
    import asyncio

    url = SPEEDTEST_FILES[1]  # 25 MB file for quicker test
    logger.info("Running speed test...")

    start = time.monotonic()
    downloaded = 0

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=60) as resp:
                while True:
                    chunk = await resp.content.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
    except Exception as exc:
        logger.error("Speedtest failed: %s", exc)
        return f"❌ Speedtest failed: {exc}"

    elapsed = time.monotonic() - start
    if elapsed <= 0:
        return "❌ Speedtest: measurement error"

    speed_bps = downloaded * 8 / elapsed  # bits per second
    speed_mbps = speed_bps / 1_000_000

    return (
        f"🌐 **Speed Test** (Cloudflare)\n\n"
        f"📥 Download: `{speed_mbps:.1f} Mbps`\n"
        f"📦 Size: `{human_bytes(downloaded)}`\n"
        f"⏱ Time: `{elapsed:.1f}s`"
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_temp(older_than_hours: int = 6) -> int:
    """Remove .part/.ytdl files older than threshold. Return count removed."""
    cutoff = time.time() - (older_than_hours * 3600)
    removed = 0
    dl_dir = Config.DOWNLOAD_DIR

    if not dl_dir.exists():
        return 0

    for entry in dl_dir.rglob("*"):
        if not entry.is_file():
            continue
        if entry.suffix in (".part", ".ytdl") and entry.stat().st_mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
                logger.info("Cleaned up temp file: %s", entry.name)
            except OSError as exc:
                logger.warning("Failed to delete %s: %s", entry.name, exc)

    if removed:
        logger.info("Auto-cleanup: removed %d temp files", removed)
    return removed


def safe_delete(path: Path) -> bool:
    """Try to delete a file; return success."""
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        logger.warning("safe_delete failed for %s: %s", path, exc)
    return False
