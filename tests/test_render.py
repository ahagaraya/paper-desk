"""Headless render of the static desk at phone and desktop sizes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from desk_snapshot import build_snapshot, dump  # noqa: E402

CHROME = os.environ.get("CHROME", "google-chrome")
SHOTS = Path(os.environ.get("PAPER_DESK_SHOTS", "/tmp/paper-desk-shots"))
PUBLISHED = datetime(2026, 11, 8, 0, 6, tzinfo=timezone(timedelta(hours=3)))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return


class Server:
    def __init__(self, directory: Path):
        handler = partial(QuietHandler, directory=str(directory))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def chrome(url: str, width: int, height: int, screenshot: Path | None = None) -> str:
    # once=1 performs the same fetch and then does not schedule another one,
    # so a headless browser can finish instead of waiting on the 30s timer.
    joiner = "&" if "?" in url else "?"
    target = url + joiner + "once=1"
    profile = tempfile.mkdtemp(prefix="desk-chrome-")
    command = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--force-device-scale-factor=1",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--default-background-color=FF07090D",
    ]
    if screenshot is not None:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"--screenshot={screenshot}")
    command.extend(["--dump-dom", target])
    try:
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            # This Chrome build writes the dump and the screenshot, then stays
            # up. The captured output is complete; the process is killed here.
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if "</html>" not in stdout:
        raise AssertionError((stderr or "")[-2000:])
    if screenshot is not None and (not screenshot.exists() or screenshot.stat().st_size < 1000):
        raise AssertionError("screenshot was not written\n" + (stderr or "")[-2000:])
    return stdout


def compact(dom: str) -> str:
    return re.sub(r">\s+<", "><", dom)


def png_size(path: Path) -> tuple[int, int]:
    blob = path.read_bytes()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG")
    return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which(CHROME) is None:
            raise unittest.SkipTest("chrome is not installed")
        SHOTS.mkdir(parents=True, exist_ok=True)
        cls.site = Server(ROOT)

    @classmethod
    def tearDownClass(cls) -> None:
        site = getattr(cls, "site", None)
        if site is not None:
            site.close()

    def test_historical_snapshot_renders_paper_labels(self) -> None:
        desktop = SHOTS / "paper_desk_desktop.png"
        mobile = SHOTS / "paper_desk_mobile.png"
        desktop_dom = chrome(self.site.url, 1280, 1320, desktop)
        mobile_dom = chrome(self.site.url, 390, 1760, mobile)
        for dom in (desktop_dom, mobile_dom):
            self.assertIn("PAPER", dom)
            self.assertIn("SIMULATION", dom)
            self.assertIn("LIVE OFF", dom)
            self.assertIn("since $1,000", dom)
            self.assertIn("$997.34", dom)
            self.assertIn("−$2.66", dom)
            self.assertIn("COGE", dom)
            self.assertIn("AMD", dom)
            self.assertIn("Orchestrator", dom)
            self.assertIn("Snapshot file loaded", dom)
            self.assertNotIn("day pnl", dom.lower())
            self.assertNotIn("\u041c\u0430\u0440\u0438", dom)
        self.assertGreater(desktop.stat().st_size, 10000)
        self.assertGreater(mobile.stat().st_size, 10000)
        self.assertEqual(png_size(desktop)[0], 1280)
        self.assertEqual(png_size(mobile)[0], 390)

    def test_fetched_open_book_replaces_the_cached_snapshot(self) -> None:
        state = SHOTS / "open-state"
        if state.exists():
            shutil.rmtree(state)
        state.mkdir()
        (state / "equity.json").write_text(
            json.dumps(
                {
                    "mode": "paper",
                    "live": False,
                    "currency": "USD",
                    "cash": 200,
                    "equity": 1100,
                    "realized_pnl": 50,
                    "unrealized_pnl": 50,
                    "wins": 3,
                    "losses": 1,
                    "closed_trades": 4,
                    "peak_equity": 1120,
                    "max_drawdown_pct": 1.5,
                    "day_start_equity": 1100,
                    "updated_at": "2026-11-08T00:05:00+03:00",
                }
            ),
            encoding="utf-8",
        )
        (state / "risk_status.json").write_text(
            json.dumps(
                {
                    "day_pnl_usd": 0,
                    "halt_new_entries": True,
                    "halt_reasons": ["daily loss cap"],
                }
            ),
            encoding="utf-8",
        )
        (state / "positions.json").write_text(
            json.dumps(
                {
                    "open": [
                        {
                            "token": "WIDGET",
                            "size_usd": 900,
                            "entry_price": 1.25,
                            "mark": 1.3,
                            "unrealized_pnl": 50,
                            "opened_at": "2026-11-07T18:00:00+03:00",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        doc = build_snapshot(state, now=PUBLISHED)
        site_dir = SHOTS / "open-site"
        if site_dir.exists():
            shutil.rmtree(site_dir)
        site_dir.mkdir()
        shutil.copy(ROOT / "index.html", site_dir / "index.html")
        shutil.copy(ROOT / "favicon.svg", site_dir / "favicon.svg")
        shutil.copy(ROOT / "site.webmanifest", site_dir / "site.webmanifest")
        (site_dir / "data.json").write_text(dump(doc), encoding="utf-8")
        server = Server(site_dir)
        try:
            dom = chrome(server.url, 390, 1620, SHOTS / "paper_desk_open_mobile.png")
        finally:
            server.close()
        flat = compact(dom)
        self.assertIn("WIDGET", flat)
        self.assertIn("$1,100.00", flat)
        self.assertIn("+$100.00", flat)
        self.assertIn(">HALT<", flat)
        self.assertIn(">LIVE OFF<", flat)
        self.assertIn(">PAPER<", flat)
        self.assertIn(">SIMULATION<", flat)
        self.assertIn("daily loss cap", flat)
        self.assertNotIn("$997.34", flat)
        self.assertNotIn(">NO HALT<", flat)
        self.assertEqual(doc["pnl_since_start"], 100.0)


if __name__ == "__main__":
    unittest.main()
