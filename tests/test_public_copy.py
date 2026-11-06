"""Public files stay on the owner's identity and keep the paper labels."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk_snapshot import validate_public_snapshot  # noqa: E402
FORBIDDEN_NAME = "\u041c\u0430\u0440\u0438"
BRANDS = ("gro" + "k", "cur" + "sor")
TEXT_SUFFIXES = {".py", ".html", ".md", ".json", ".svg", ".webmanifest", ".txt", ".yml", ".yaml"}


class PublicCopyTests(unittest.TestCase):
    def test_tracked_text_has_no_personal_name_or_product_branding(self) -> None:
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in {".git", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            if FORBIDDEN_NAME in text:
                offenders.append(f"{path.name}: personal name")
            for brand in BRANDS:
                if brand in lowered:
                    offenders.append(f"{path.name}: {brand}")
        self.assertEqual(offenders, [])

    def test_dashboard_shell_keeps_paper_labels_and_relative_fetch(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for label in ("PAPER", "SIMULATION", "LIVE OFF"):
            self.assertIn(label, html)
        self.assertIn('content="width=device-width, initial-scale=1', html)
        self.assertIn("setInterval(refresh, 30000)", html)
        self.assertIn('fetch("./data.json?ts="', html)
        self.assertIn("since $1,000", html)
        self.assertNotIn("day pnl", html.lower())
        self.assertNotIn("day_pnl", html)
        self.assertNotIn("meta http-equiv=\"refresh\"", html)
        self.assertIn("ahagaraya", html)
        self.assertNotIn("file://", html)

    def test_embedded_fallback_is_a_valid_paper_snapshot(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        match = re.search(
            r'<script type="application/json" id="fallback">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match)
        doc = json.loads(match.group(1))
        validate_public_snapshot(doc)
        self.assertFalse(doc["live"])
        self.assertTrue(doc["simulation"])
        self.assertEqual(doc["mode"], "paper")
        self.assertEqual(doc, json.loads((ROOT / "data.json").read_text(encoding="utf-8")))
        self.assertNotIn("day_pnl", doc)


if __name__ == "__main__":
    unittest.main()
