"""
Regression tests for bugs found during the YTtoTg review.

Covers:
  • daily watch schedule no longer double-scans around HH:MM
  • DynamicSemaphore enforces a live-updating concurrency limit
  • splitter streams chunks (bounded memory) and produces valid ZIP parts
  • no literal "\\n" escapes in user-facing handler messages
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

# Keep the unit tests runnable before optional runtime dependencies are installed.
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ytbot"))

from config import Config  # noqa: E402
from core.state import StateManager, Watch  # noqa: E402
from core.downloader import DynamicSemaphore  # noqa: E402
from core.splitter import cleanup_parts, needs_split, split_to_zip_parts  # noqa: E402


class DailyWatchScheduleTests(unittest.TestCase):
    """`watch_due` in daily_at mode must trigger at most once per day."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateManager(Path(self.tmp.name) / "state.json")
        self.state.load()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_start_before_hhmm_is_due(self) -> None:
        w = Watch(id="w1", url="u", key="k", title="T", enabled=True, daily_at="06:00")
        now = datetime.datetime(2026, 8, 29, 5, 59, 30).timestamp()
        self.assertTrue(self.state.watch_due(w, now))

    def test_check_just_before_hhmm_is_not_duplicated_at_hhmm(self) -> None:
        """Regression: catch-up scan at 05:59 must not re-trigger at 06:01."""
        w = Watch(id="w1", url="u", key="k", title="T", enabled=True, daily_at="06:00")
        w.last_check = datetime.datetime(2026, 8, 29, 5, 59, 30).timestamp()
        now = datetime.datetime(2026, 8, 29, 6, 1, 0).timestamp()
        self.assertFalse(self.state.watch_due(w, now))

    def test_due_again_next_day(self) -> None:
        w = Watch(id="w1", url="u", key="k", title="T", enabled=True, daily_at="06:00")
        w.last_check = datetime.datetime(2026, 8, 29, 6, 1, 0).timestamp()
        now = datetime.datetime(2026, 8, 30, 6, 1, 0).timestamp()
        self.assertTrue(self.state.watch_due(w, now))

    def test_interval_mode_unaffected(self) -> None:
        w = Watch(id="w1", url="u", key="k", title="T", enabled=True, interval_min=30)
        w.last_check = datetime.datetime(2026, 8, 29, 5, 30, 0).timestamp()
        now = datetime.datetime(2026, 8, 29, 6, 1, 0).timestamp()  # 31 min later
        self.assertTrue(self.state.watch_due(w, now))
        now2 = datetime.datetime(2026, 8, 29, 5, 50, 0).timestamp()  # 20 min later
        self.assertFalse(self.state.watch_due(w, now2))


class DynamicSemaphoreTests(unittest.TestCase):
    """/setparallel must change real download concurrency without a restart."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_limit_reads_live_setting(self) -> None:
        """Raising the limit mid-run unblocks waiting workers."""
        with tempfile.TemporaryDirectory() as tmp:
            state = StateManager(Path(tmp) / "state.json")
            state.load()
            state.settings["parallel_downloads"] = 1
            sem = DynamicSemaphore(state)

            async def scenario() -> None:
                completed = 0
                active = 0
                peak = 0

                async def worker() -> None:
                    nonlocal completed, active, peak
                    async with sem:
                        active += 1
                        peak = max(peak, active)
                        await asyncio.sleep(0.05)
                        active -= 1
                        completed += 1

                t1 = asyncio.create_task(worker())
                t2 = asyncio.create_task(worker())
                await asyncio.sleep(0.12)  # first finishes; second still gated
                self.assertEqual(completed, 1)
                self.assertEqual(peak, 1)  # limit=1 really caps concurrency

                state.settings["parallel_downloads"] = 2  # live change
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
                self.assertEqual(completed, 2)
                return peak

            peak = self._run(scenario())
            self.assertEqual(peak, 1)

    def test_limit_caps_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateManager(Path(tmp) / "state.json")
            state.load()
            state.settings["parallel_downloads"] = 2
            sem = DynamicSemaphore(state)

            async def scenario() -> None:
                active = 0
                peak = 0
                completed = 0

                async def worker() -> None:
                    nonlocal active, peak, completed
                    async with sem:
                        active += 1
                        peak = max(peak, active)
                        await asyncio.sleep(0.05)
                        active -= 1
                        completed += 1

                tasks = [asyncio.create_task(worker()) for _ in range(5)]
                await asyncio.gather(*tasks)
                return peak, completed

            peak, completed = self._run(scenario())
            self.assertEqual(completed, 5)
            self.assertEqual(peak, 2)  # never exceeded the limit


class SplitterTests(unittest.TestCase):
    """Split parts must be valid ZIPs and byte-identical to the source."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        old_split, old_tg = Config.SPLIT_SIZE_MB, Config.TG_MAX_UPLOAD_MB
        Config.SPLIT_SIZE_MB = 1       # 1 MB parts -> a 2.5 MB file = 3 parts
        Config.TG_MAX_UPLOAD_MB = 1    # needs_split compares against the TG cap
        self.addCleanup(lambda: setattr(Config, "SPLIT_SIZE_MB", old_split))
        self.addCleanup(lambda: setattr(Config, "TG_MAX_UPLOAD_MB", old_tg))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_split_roundtrip_preserves_bytes(self) -> None:
        src = Path(self.tmp.name) / "big.bin"
        data = os.urandom(2_500_000)  # ~2.4 MB
        src.write_bytes(data)

        self.assertTrue(needs_split(str(src)))
        parts = split_to_zip_parts(str(src))
        self.assertGreaterEqual(len(parts), 3)

        try:
            restored = b""
            for part in parts:
                with zipfile.ZipFile(part) as zf:
                    (name,) = zf.namelist()
                    restored += zf.read(name)
            self.assertEqual(restored, data)
        finally:
            cleanup_parts(parts)

    def test_small_file_not_split(self) -> None:
        src = Path(self.tmp.name) / "small.bin"
        src.write_bytes(os.urandom(1024))
        self.assertFalse(needs_split(str(src)))


class CookieEnvBootstrapTests(unittest.TestCase):
    """COOKIES_CONTENT seeds the managed file only when nothing else exists."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data = Config.DATA_DIR
        self.old_base = Config.BASE_DIR
        self.old_cookie_path = Config.COOKIES_PATH
        self.old_content = Config.COOKIES_CONTENT
        Config.DATA_DIR = self.root / "data"
        Config.BASE_DIR = self.root
        Config.COOKIES_PATH = ""
        Config.COOKIES_CONTENT = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSAPISID\tenv-secret\n"
        )

    def tearDown(self) -> None:
        Config.DATA_DIR = self.old_data
        Config.BASE_DIR = self.old_base
        Config.COOKIES_PATH = self.old_cookie_path
        Config.COOKIES_CONTENT = self.old_content
        self.tmp.cleanup()

    def test_seeds_file_when_nothing_exists(self) -> None:
        from core.auth import bootstrap_cookies_from_env, active_cookie_path
        seeded = bootstrap_cookies_from_env()
        self.assertIsNotNone(seeded)
        path = Path(seeded)
        self.assertEqual(path, active_cookie_path())
        self.assertEqual(path, Config.DATA_DIR / "cookies.txt")
        self.assertIn("SAPISID", path.read_text(encoding="utf-8"))

    def test_existing_valid_file_wins(self) -> None:
        from core.auth import bootstrap_cookies_from_env
        existing = Config.DATA_DIR
        existing.mkdir(parents=True, exist_ok=True)
        target = existing / "cookies.txt"
        target.write_text(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSAPISID\tkeep-me\n",
            encoding="utf-8",
        )
        seeded = bootstrap_cookies_from_env()
        self.assertIsNone(seeded)
        self.assertIn("keep-me", target.read_text(encoding="utf-8"))
        self.assertNotIn("env-secret", target.read_text(encoding="utf-8"))

    def test_invalid_content_is_rejected_and_writes_nothing(self) -> None:
        from core.auth import bootstrap_cookies_from_env
        Config.COOKIES_CONTENT = "not a cookie export at all"
        seeded = bootstrap_cookies_from_env()
        self.assertIsNone(seeded)
        target = Config.DATA_DIR / "cookies.txt"
        self.assertFalse(target.exists())
        self.assertEqual(Config.DATA_DIR, Config.DATA_DIR)  # no partial writes

    def test_empty_content_is_noop(self) -> None:
        from core.auth import bootstrap_cookies_from_env
        Config.COOKIES_CONTENT = ""
        self.assertIsNone(bootstrap_cookies_from_env())


class HandlerMessageFormatTests(unittest.TestCase):
    """User-facing replies must use real newlines, not literal backslash-n."""

    def test_no_literal_backslash_n_in_handler_replies(self) -> None:
        source = (ROOT / "ytbot" / "bot" / "handlers.py").read_text(encoding="utf-8")
        for needle in ("Photos are not accepted", "was not recognised as a cookie file"):
            idx = source.find(needle)
            self.assertNotEqual(idx, -1, needle)
            block = source[idx - 60: idx + 400]
            # The bug was a literal "\\n" (two backslashes in the source, which
            # Python renders as backslash+n). The fix is a single "\n" escape,
            # which Python renders as an actual newline.
            self.assertNotIn("\\\\n", block, f"literal \\\\n still present near {needle!r}")
            self.assertIn("\\n", block.replace("\\\\n", ""), f"missing newline escapes near {needle!r}")


if __name__ == "__main__":
    unittest.main()
