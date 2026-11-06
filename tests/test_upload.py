"""Contents API uploader: allowlist, idempotence, and no token handling."""

from __future__ import annotations

import base64
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk_snapshot import build_snapshot, dump  # noqa: E402
import upload_data  # noqa: E402
from upload_data import UploadError, decide, upload  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "historical_snapshot"
NOW = datetime(2026, 11, 7, 0, 40, 10, tzinfo=timezone(timedelta(hours=3)))


def _doc(published: datetime, equity: float = 997.340892) -> dict:
    # Historical fixture equity is fixed; equity argument rebuilds via a copy
    # only when the caller passes the fixture value. Other equities are patched
    # after a validated build so tests can force a metric change.
    doc = build_snapshot(FIXTURE, now=published)
    if equity != doc["equity"]:
        doc["equity"] = equity
        doc["pnl_since_start"] = round(equity - 1000.0, 6)
        doc["pnl_since_start_pct"] = round(((equity / 1000.0) - 1.0) * 100.0, 4)
        from desk_snapshot import metrics_hash

        doc["content_hash"] = metrics_hash(doc)
    return doc


class FakeGitHub:
    def __init__(self, remote: dict | None, sha: str | None = "abc123"):
        self.remote = remote
        self.sha = sha
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, endpoint: str, payload: dict | None = None):
        self.calls.append((method, endpoint, payload))
        if method == "GET":
            if self.remote is None:
                raise UploadError("Not Found", status=404)
            encoded = base64.b64encode(dump(self.remote).encode("utf-8")).decode("ascii")
            return 200, {"sha": self.sha, "content": encoded, "encoding": "base64"}
        if method == "PUT":
            if payload is None or payload.get("content") is None:
                raise UploadError("missing content", status=422)
            if ".." in endpoint or not endpoint.endswith("/contents/data.json"):
                raise UploadError("bad path", status=422)
            return 200, {"content": {"path": "data.json", "sha": "new"}}
        raise UploadError(f"unexpected {method}", status=400)


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data.json"
        self.doc = _doc(NOW)
        self.path.write_text(dump(self.doc), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_identical_file_skips_without_put(self) -> None:
        github = FakeGitHub(self.doc)
        action = upload(self.path, now=NOW, api=github)
        self.assertEqual(action, "skip")
        self.assertEqual([call[0] for call in github.calls], ["GET"])

    def test_same_metrics_inside_fresh_window_skip(self) -> None:
        remote = _doc(NOW - timedelta(seconds=60))
        github = FakeGitHub(remote)
        action = upload(self.path, now=NOW, max_age_sec=240, api=github)
        self.assertEqual(action, "skip")
        self.assertEqual([call[0] for call in github.calls], ["GET"])

    def test_same_metrics_past_fresh_window_updates_timestamp(self) -> None:
        remote = _doc(NOW - timedelta(minutes=10))
        github = FakeGitHub(remote)
        action = upload(self.path, now=NOW, max_age_sec=240, api=github)
        self.assertEqual(action, "update")
        method, endpoint, payload = github.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(endpoint.endswith("/repos/ahagaraya/paper-desk/contents/data.json"))
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["sha"], "abc123")
        self.assertNotIn("token", payload)
        self.assertNotIn("authorization", {key.lower() for key in payload})

    def test_metric_change_updates(self) -> None:
        remote = _doc(NOW, equity=1100.0)
        github = FakeGitHub(remote)
        action = upload(self.path, now=NOW, api=github)
        self.assertEqual(action, "update")

    def test_missing_remote_creates_without_sha(self) -> None:
        github = FakeGitHub(None)
        action = upload(self.path, dry_run=True, now=NOW, api=github)
        self.assertEqual(action, "create")
        self.assertEqual([call[0] for call in github.calls], ["GET"])
        action = upload(self.path, now=NOW, api=github)
        self.assertEqual(action, "create")
        self.assertIsNone(github.calls[-1][2].get("sha"))

    def test_rejects_other_paths_repos_and_branches(self) -> None:
        other = Path(self.tmp.name) / "README.md"
        other.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(UploadError):
            upload(other, now=NOW, api=FakeGitHub(None))
        with self.assertRaises(UploadError):
            upload(self.path, repo="someone/else", now=NOW, api=FakeGitHub(None))
        with self.assertRaises(UploadError):
            upload(self.path, branch="feature/not-allowed", now=NOW, api=FakeGitHub(None))

    def test_rejects_snapshot_that_breaks_the_whitelist(self) -> None:
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["paper_path"] = "/workspace/trading-paper"
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(UploadError):
            upload(self.path, now=NOW, api=FakeGitHub(None))

    def test_dry_run_does_not_put(self) -> None:
        github = FakeGitHub(None)
        action = upload(self.path, dry_run=True, now=NOW, api=github)
        self.assertEqual(action, "create")
        self.assertEqual([call[0] for call in github.calls], ["GET"])

    def test_check_cli_does_not_need_github(self) -> None:
        code = upload_data.main(["--file", str(self.path), "--check"])
        self.assertEqual(code, 0)

    def test_cli_dry_run_uses_injected_boundary_only_through_gh_api_source(self) -> None:
        source = inspect.getsource(upload_data.gh_api)
        self.assertNotIn("auth token", source)
        self.assertNotIn("GH_TOKEN", source)
        self.assertIn('"gh", "api"', source)
        script = (ROOT / "upload_data.py").read_text(encoding="utf-8")
        self.assertNotIn("ghp_", script)
        self.assertNotIn("github_pat_", script)

    def test_real_cli_check_subprocess(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "upload_data.py"), "--file", str(self.path), "--check"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("ghs_", completed.stdout + completed.stderr)

    def test_corrupt_remote_is_replaced(self) -> None:
        github = FakeGitHub(self.doc)
        github.remote = None

        def api(method, endpoint, payload=None):
            github.calls.append((method, endpoint, payload))
            if method == "GET":
                return 200, {"sha": "abc123", "content": base64.b64encode(b"not-json").decode(), "encoding": "base64"}
            return 200, {"content": {"path": "data.json"}}

        action = upload(self.path, now=NOW, api=api)
        self.assertEqual(action, "update")
        self.assertEqual(github.calls[-1][2]["sha"], "abc123")

    def test_decide_treats_invalid_remote_as_update(self) -> None:
        self.assertEqual(
            decide(self.doc, {"equity": 1}, now=NOW, max_age_sec=240),
            "update",
        )


if __name__ == "__main__":
    unittest.main()
