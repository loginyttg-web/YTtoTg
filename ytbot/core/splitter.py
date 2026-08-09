"""
Split large files into 1.9 GB ZIP parts for Telegram upload.
"""

import logging
import os
import zipfile
from pathlib import Path
from typing import List

from config import Config
from utils.helpers import human_bytes

logger = logging.getLogger("splitter")


def needs_split(filepath: str) -> bool:
    """Return True if file > TG_MAX_UPLOAD_MB."""
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return False
    return size > Config.tg_max_bytes()


def split_to_zip_parts(filepath: str) -> List[str]:
    """
    Split a file into ZIP parts of SPLIT_SIZE_MB each.
    Each part is a valid ZIP (ZIP_STORED) containing one raw chunk.

    Naming: `filepath.zip.001`, `.zip.002`, ...

    Returns list of part paths.
    """
    filepath = Path(filepath)
    chunk_size = Config.split_size_bytes()
    total_size = filepath.stat().st_size
    total_chunks = (total_size + chunk_size - 1) // chunk_size

    logger.info(
        "Splitting %s (%s) into %d parts of %s each",
        filepath.name, human_bytes(total_size), total_chunks, human_bytes(chunk_size),
    )

    parts: List[str] = []

    try:
        with open(filepath, "rb") as src:
            for i in range(total_chunks):
                part_path = str(filepath) + f".zip.{i + 1:03d}"
                chunk = src.read(chunk_size)

                with zipfile.ZipFile(part_path, "w", zipfile.ZIP_STORED) as zf:
                    zf.writestr(f"chunk_{i + 1:03d}", chunk)

                parts.append(part_path)
                logger.debug("  Part %d/%d: %s", i + 1, total_chunks, Path(part_path).name)

    except OSError as exc:
        logger.error("Failed to split %s: %s", filepath.name, exc)
        cleanup_parts(parts)
        raise

    logger.info("Split complete: %d parts", len(parts))
    return parts


def cleanup_parts(parts: List[str]) -> None:
    """Delete all ZIP part files."""
    for p in parts:
        try:
            os.unlink(p)
        except OSError:
            pass
    if parts:
        logger.info("Cleaned up %d split parts", len(parts))
