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
    from utils.helpers import bar_smooth
    du = disk_usage()
    used_folder = folder_size(Config.DOWNLOAD_DIR)
    alert = "\n⚠️ **HIGH USAGE!**" if du["percent"] > Config.DISK_ALERT_PERCENT else ""

    bar = bar_smooth(du["used"], du["total"], 14)

    return (
        f"❖ **𝗗𝗶𝘀𝗸 𝗨𝘀𝗮𝗴𝗲**{alert}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"`{bar}` **{du['percent']:.1f}%**\n\n"
        f"⋄ 𝗧𝗼𝘁𝗮𝗹: `{human_bytes(du['total'])}`\n"
        f"⋄ 𝗨𝘀𝗲𝗱:  `{human_bytes(du['used'])}`\n"
        f"⋄ 𝗙𝗿𝗲𝗲:  `{human_bytes(du['free'])}`\n\n"
        f"⋄ 📂 Download folder: `{human_bytes(used_folder)}`\n"
        f"⋄ 🛡️ Safety margin: `{human_bytes(Config.safety_margin_bytes())}`"
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
    from utils.helpers import bar_smooth
    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = disk_usage()

    boot = psutil.boot_time()
    uptime_seconds = time.time() - boot
    days, rem = divmod(int(uptime_seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)

    cpu_pct = psutil.cpu_percent(interval=0.5)

    return (
        f"❖ **𝗦𝗲𝗿𝘃𝗲𝗿 𝗜𝗻𝗳𝗼**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💻 **CPU** `{cpu_pct:>3.0f}%`  `{bar_smooth(cpu_pct, 100, 10)}`\n"
        f"🧠 **RAM** `{ram.percent:>3.0f}%`  `{bar_smooth(ram.used, ram.total, 10)}`  "
        f"`{human_bytes(ram.used, True)}/{human_bytes(ram.total, True)}`\n"
        f"📀 **Swap** `{swap.percent:>3.0f}%`  `{bar_smooth(swap.used, max(swap.total, 1), 10)}`\n"
        f"💾 **Disk** `{disk['percent']:>3.0f}%`  "
        f"`{bar_smooth(disk['used'], disk['total'], 10)}`  "
        f"`{human_bytes(disk['free'], True)} free`\n\n"
        f"⋄ ⏱ Uptime: `{days}d {hours}h {mins}m`\n"
        f"⋄ 💻 Cores: `{psutil.cpu_count(logical=True)}` logical"
    )


# ---------------------------------------------------------------------------
# Speedtest — 10s min, 200 MB DL + 200 MB UL + ping
# ---------------------------------------------------------------------------

SPEEDTEST_DL_URL = "https://speed.cloudflare.com/__down?bytes=209715200"  # 200 MB
SPEEDTEST_UL_URL = "https://speed.cloudflare.com/__up"
PING_URL = "https://speed.cloudflare.com/__down?bytes=0"
PING_COUNT = 3
SPEEDTEST_MIN_SECONDS = 10

SPEEDTEST_FILES = [
    "https://speed.cloudflare.com/__down?bytes=104857600",
    "https://speed.cloudflare.com/__down?bytes=26214400",
]


async def _measure_ping(session) -> str:
    import asyncio
    pings = []
    for _ in range(PING_COUNT):
        t0 = time.monotonic()
        try:
            async with session.head(PING_URL, timeout=10) as resp:
                await resp.read()
                pings.append((time.monotonic() - t0) * 1000)
        except Exception:
            pass
        await asyncio.sleep(0.15)
    if not pings:
        return "—"
    avg = sum(pings) / len(pings)
    return f"{avg:.0f} ms (min {min(pings):.0f} / max {max(pings):.0f})"


async def run_speedtest() -> str:
    """Full speedtest: ping + 200 MB download (≥10s) + 200 MB upload."""
    import aiohttp
    import asyncio

    logger.info("Running full speedtest (200MB DL + 200MB UL + ping)...")
    total_start = time.monotonic()

    try:
        async with aiohttp.ClientSession() as session:
            ping_str = await _measure_ping(session)

            # ── Download ──
            dl_downloaded = 0
            dl_start = time.monotonic()
            # Loop until we have ≥200 MB and ≥10s (covers fast networks)
            while True:
                async with session.get(SPEEDTEST_DL_URL, timeout=120) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"DL HTTP {resp.status}")
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        dl_downloaded += len(chunk)
                        if dl_downloaded >= 200 * 1024 * 1024 and (time.monotonic() - dl_start) >= SPEEDTEST_MIN_SECONDS:
                            break
                    if dl_downloaded >= 200 * 1024 * 1024 and (time.monotonic() - dl_start) >= SPEEDTEST_MIN_SECONDS:
                        break
                if dl_downloaded >= 200 * 1024 * 1024 and (time.monotonic() - dl_start) >= SPEEDTEST_MIN_SECONDS:
                    break
                if dl_downloaded >= 400 * 1024 * 1024 or (time.monotonic() - dl_start) >= 30:
                    break
            dl_elapsed = time.monotonic() - dl_start
            dl_mbps = (dl_downloaded * 8 / dl_elapsed) / 1_000_000 if dl_elapsed > 0 else 0

            # ── Upload (200 MB POST) ──
            ul_total = 200 * 1024 * 1024
            ul_chunk = os.urandom(256 * 1024)
            ul_sent = 0

            async def _gen():
                nonlocal ul_sent
                while ul_sent < ul_total:
                    yield ul_chunk
                    ul_sent += len(ul_chunk)

            try:
                ul_t0 = time.monotonic()
                async with session.post(SPEEDTEST_UL_URL, data=_gen(), timeout=120) as resp:
                    await resp.read()
                ul_elapsed = time.monotonic() - ul_t0
                ul_mbps = (ul_sent * 8 / ul_elapsed) / 1_000_000 if ul_elapsed > 0 else 0
                ul_info = f"`{human_bytes(ul_sent)}` in `{ul_elapsed:.1f}s`"
            except Exception as exc:
                logger.warning("Upload test failed: %s", exc)
                ul_mbps = 0
                ul_info = f"failed (`{exc}`)"

    except Exception as exc:
        logger.error("Speedtest failed: %s", exc)
        return f"❌ Speedtest failed: `{exc}`"

    total_elapsed = time.monotonic() - total_start
    return (
        f"🌐 **Speed Test — Cloudflare**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏓 Ping: `{ping_str}`\n"
        f"📥 Download: `{dl_mbps:.1f} Mbps` · `{human_bytes(dl_downloaded)}` in `{dl_elapsed:.1f}s`\n"
        f"📤 Upload: `{ul_mbps:.1f} Mbps` · {ul_info}\n"
        f"⏱ Total: `{total_elapsed:.1f}s`"
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
