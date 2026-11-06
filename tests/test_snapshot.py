"""Publisher whitelist, cumulative PnL, and refusal to invent a book."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk_snapshot import (  # noqa: E402
    STARTING_EQUITY,
    StateMissing,
    build_snapshot,
    dump,
    metrics_hash,
    validate_public_snapshot,
)
import publish_data  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "historical_snapshot"
PUBLISHED = datetime.fromisoformat("2026-11-07T00:40:10+03:00")
FORBIDDEN_NAME = "\u041c\u0430\u0440\u0438"


def _write(dirpath: Path, name: str, payload) -> None:
    (dirpath / name).write_text(json.dumps(payload), encoding="utf-8")


class SnapshotTests(unittest.TestCase):
    def test_historical_snapshot_uses_start_not_midnight_day_pnl(self) -> None:
        doc = build_snapshot(FIXTURE, now=PUBLISHED)
        self.assertEqual(doc["starting_equity"], STARTING_EQUITY)
        self.assertEqual(doc["equity"], 997.340892)
        self.assertEqual(doc["cash"], 997.340892)
        self.assertEqual(doc["pnl_since_start"], -2.659108)
        self.assertEqual(doc["pnl_since_start_pct"], -0.2659)
        self.assertNotIn("day_pnl", doc)
        self.assertNotIn("day_pnl_pct", doc)
        self.assertNotIn("paper_path", doc)
        self.assertNotIn("orchestrator_call", doc)
        self.assertEqual(doc["live"], False)
        self.assertEqual(doc["mode"], "paper")
        self.assertTrue(doc["simulation"])
        self.assertEqual(doc["book"], "flat")
        self.assertEqual(doc["open"], [])
        self.assertEqual(doc["source_updated_at"], "2026-11-07T00:32:38+03:00")
        self.assertEqual(doc["source_age_at_publish_sec"], 452)
        self.assertEqual(doc["published_at"], "2026-11-07T00:40:10+03:00")
        self.assertEqual([row["token"] for row in doc["recent_closes"]], ["COGE", "AMD", "401kinu", "KoriMu"])
        self.assertEqual(doc["recent_closes"][0]["exits"], ["orchestrator_skip_reverse"])
        self.assertNotIn("position_id", json.dumps(doc))
        self.assertEqual(doc["agents"][-1]["name"], "Orchestrator")
        self.assertNotIn(FORBIDDEN_NAME, dump(doc))
        validate_public_snapshot(doc)
        self.assertEqual(doc["content_hash"], metrics_hash(doc))

    def test_drawdown_is_computed_from_peak_not_a_stale_risk_field(self) -> None:
        doc = build_snapshot(FIXTURE, now=PUBLISHED)
        self.assertEqual(doc["peak_equity"], 1004.583077)
        self.assertAlmostEqual(doc["drawdown_from_peak_pct"], -0.7209, places=4)
        self.assertEqual(doc["max_drawdown_pct"], 0.8107)

    def test_repeat_build_is_byte_identical(self) -> None:
        first = dump(build_snapshot(FIXTURE, now=PUBLISHED))
        second = dump(build_snapshot(FIXTURE, now=PUBLISHED))
        self.assertEqual(first, second)

    def test_midnight_reset_does_not_zero_cumulative_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write(
                state,
                "equity.json",
                {
                    "mode": "paper",
                    "live": False,
                    "cash": 200,
                    "equity": 1100,
                    "realized_pnl": 50,
                    "unrealized_pnl": 50,
                    "wins": 3,
                    "losses": 1,
                    "closed_trades": 4,
                    "peak_equity": 1120,
                    "day_start_equity": 1100,
                    "updated_at": "2026-11-08T00:05:00+03:00",
                },
            )
            _write(
                state,
                "risk_status.json",
                {
                    "day_pnl_usd": 0,
                    "day_start_equity": 1100,
                    "halt_new_entries": False,
                    "halt_reasons": [],
                },
            )
            _write(
                state,
                "positions.json",
                {
                    "open": [
                        {
                            "token": "WIDGET",
                            "size_usd": 900,
                            "entry_price": 1.25,
                            "mark": 1.3,
                            "unrealized_pnl": 50,
                            "opened_at": "2026-11-07T18:00:00+03:00",
                            "notes": "do not publish this private note",
                            "paper_path": "/workspace/trading-paper/state/secret.log",
                        }
                    ]
                },
            )
            doc = build_snapshot(state, now=datetime.fromisoformat("2026-11-08T00:06:00+03:00"))
            self.assertEqual(doc["pnl_since_start"], 100.0)
            self.assertEqual(doc["pnl_since_start_pct"], 10.0)
            self.assertEqual(doc["book"], "open")
            self.assertEqual(doc["open_count"], 1)
            self.assertEqual(doc["open"][0]["token"], "WIDGET")
            self.assertNotIn("notes", doc["open"][0])
            encoded = dump(doc)
            self.assertNotIn("private note", encoded)
            self.assertNotIn("secret.log", encoded)
            self.assertNotIn("day_pnl", encoded)

    def test_private_fields_and_personal_name_never_pass_the_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write(
                state,
                "equity.json",
                {
                    "equity": 1000,
                    "cash": 1000,
                    "live": False,
                    "mode": "paper",
                    "updated_at": "2026-11-07T00:32:38+03:00",
                    "display_name": FORBIDDEN_NAME,
                    "paper_path": "/workspace/trading-paper",
                    "token": "ghp_" + "a" * 36,
                },
            )
            _write(
                state,
                "risk_status.json",
                {
                    "halt_new_entries": True,
                    "halt_reasons": [
                        FORBIDDEN_NAME,
                        "daily loss cap",
                        "see /workspace/trading-paper/state/run.log",
                        "api_key=should-not-pass",
                    ],
                    "orchestrator_call": "CLEAR_HALT",
                    "closed_detail": [
                        {
                            "token": FORBIDDEN_NAME,
                            "realized": 1,
                            "outcome": "win",
                            "exits": ["full_exit", "not a valid exit"],
                        }
                    ],
                },
            )
            _write(state, "positions.json", {"open": []})
            doc = build_snapshot(state, now=PUBLISHED)
            encoded = dump(doc)
            self.assertNotIn(FORBIDDEN_NAME, encoded)
            self.assertNotIn("ghp_", encoded)
            self.assertNotIn("api_key", encoded)
            self.assertNotIn("trading-paper", encoded)
            self.assertEqual(doc["halt_reasons"], ["daily loss cap"])
            self.assertEqual(doc["recent_closes"][0]["token"], "unknown")
            self.assertEqual(doc["recent_closes"][0]["exits"], ["full_exit"])
            self.assertTrue(doc["halt_new_entries"])

    def test_live_flag_is_passed_through_and_never_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write(
                state,
                "equity.json",
                {
                    "equity": 1000,
                    "cash": 1000,
                    "live": True,
                    "mode": "paper",
                    "updated_at": "2026-11-07T00:32:38+03:00",
                },
            )
            _write(state, "risk_status.json", {"live": False, "halt_new_entries": False})
            _write(state, "positions.json", {"open": []})
            doc = build_snapshot(state, now=PUBLISHED)
            self.assertTrue(doc["live"])
            self.assertEqual(doc["mode"], "paper")
            self.assertTrue(doc["simulation"])

    def test_string_false_live_flag_stays_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write(
                state,
                "equity.json",
                {
                    "equity": 1000,
                    "cash": 1000,
                    "live": "false",
                    "mode": "paper",
                    "updated_at": "2026-11-07T00:32:38+03:00",
                },
            )
            _write(state, "risk_status.json", {})
            _write(state, "positions.json", {"open": []})
            doc = build_snapshot(state, now=PUBLISHED)
            self.assertFalse(doc["live"])

    def test_missing_state_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"
            out = Path(tmp) / "data.json"
            out.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(StateMissing):
                build_snapshot(missing, now=PUBLISHED)
            code = publish_data.main(["--state", str(missing), "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(encoding="utf-8"), "{}\n")

    def test_cli_writes_checked_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data.json"
            code = publish_data.main(
                ["--state", str(FIXTURE), "--out", str(out), "--now", PUBLISHED.isoformat()]
            )
            self.assertEqual(code, 0)
            check = publish_data.main(["--check", str(out)])
            self.assertEqual(check, 0)
            script = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "publish_data.py"),
                    "--check",
                    str(out),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(script.returncode, 0, script.stderr)
            self.assertNotIn(FORBIDDEN_NAME, out.read_text(encoding="utf-8"))

    def test_non_usd_state_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write(
                state,
                "equity.json",
                {
                    "equity": 1000,
                    "cash": 1000,
                    "currency": "EUR",
                    "live": False,
                    "mode": "paper",
                    "updated_at": "2026-11-07T00:32:38+03:00",
                },
            )
            _write(state, "risk_status.json", {})
            _write(state, "positions.json", {"open": []})
            with self.assertRaises(ValueError):
                build_snapshot(state, now=PUBLISHED)

    def test_committed_snapshot_matches_the_publisher(self) -> None:
        expected = dump(build_snapshot(FIXTURE, now=PUBLISHED))
        self.assertEqual((ROOT / "data.json").read_text(encoding="utf-8"), expected)


if __name__ == "__main__":
    unittest.main()
