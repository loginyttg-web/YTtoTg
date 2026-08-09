"""
Rotating-file + colorised console logger with sensible defaults.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FMT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# ANSI colour codes for console
# ---------------------------------------------------------------------------
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

COLOURS = {
    logging.DEBUG: "\033[38;5;244m",     # grey
    logging.INFO: "\033[38;5;39m",       # cyan
    logging.WARNING: "\033[38;5;214m",   # orange
    logging.ERROR: "\033[38;5;196m",     # red
    logging.CRITICAL: "\033[1;37;41m",   # white on red bg
}

LEVEL_ICON = {
    logging.DEBUG: "⚙",
    logging.INFO: "•",
    logging.WARNING: "⚠",
    logging.ERROR: "✗",
    logging.CRITICAL: "☠",
}


class ColourFormatter(logging.Formatter):
    """ANSI-coloured console formatter with icons."""

    def format(self, record: logging.LogRecord) -> str:
        colour = COLOURS.get(record.levelno, "")
        icon = LEVEL_ICON.get(record.levelno, " ")
        # Format:  icon | module          | message
        msg = f"{colour}{icon} {record.name:<20s} │ {record.getMessage()}{_RESET}"
        if record.levelno >= logging.ERROR:
            msg = f"{_BOLD}{msg}"
        return msg


class PlainFileFormatter(logging.Formatter):
    """Plain text format for log files — no ANSI."""

    def __init__(self) -> None:
        super().__init__(LOG_FMT, datefmt=DATE_FMT)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """Configure root logger and return it."""

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot.log"

    # --- Console handler (coloured) ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(ColourFormatter())

    # --- Rotating file handler (plain, 5 MB × 3 backups) ---
    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(PlainFileFormatter())

    # --- Root logger ---
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    # --- Silence noisy libraries ---
    for noisy in ("pyrogram", "aiohttp", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("config").info("Logging initialised — file: %s", log_file)
    return root


def tail_log(log_dir: Path, lines: int = 40, level: str | None = None) -> str:
    """Return last *lines* lines of the bot log, optionally filtered by level."""
    log_file = log_dir / "bot.log"
    if not log_file.exists():
        return "(no log file yet)"

    with open(log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    if level:
        needle = f" | {level.upper():<7}"
        all_lines = [l for l in all_lines if needle in l]

    return "".join(all_lines[-lines:]) if all_lines else "(empty log)"
