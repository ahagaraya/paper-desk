#!/usr/bin/env python3
"""Upload only data.json through the GitHub contents API.

Uses the caller's existing ``gh`` login. The token stays with ``gh`` and
is never read from argv, the repository, or the snapshot. A repeated run
does nothing when the remote metrics are already the same and still fresh.

Documented endpoint:
https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents

    python3 upload_data.py --file data.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from desk_snapshot import metrics_hash, parse_time, validate_public_snapshot

ALLOWED_REPOS = frozenset({"ahagaraya/paper-desk"})
ALLOWED_BRANCHES = frozenset({"main"})
REMOTE_PATH = "data.json"
DEFAULT_MAX_AGE_SEC = 240


class UploadError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def gh_api(method: str, endpoint: str, payload: dict | None = None) -> tuple[int, object]:
    """Call ``gh api``. Auth comes from the gh credential store, not from arguments."""
    command = ["gh", "api", "--include", "--method", method, endpoint]
    body = None
    if payload is not None:
        command.extend(["--input", "-"])
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        completed = subprocess.run(
            command,
            input=body,
            capture_output=True,
            timeout=45,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UploadError("gh is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise UploadError("gh api timed out") from exc
    raw = completed.stdout or completed.stderr
    status, parsed = _parse_response(raw)
    if status == 0:
        message = completed.stderr.decode("utf-8", "replace").strip() or "gh api failed"
        raise UploadError(message.splitlines()[-1][:300], status=completed.returncode or 1)
    if status >= 400:
        message = _api_message(parsed) or f"HTTP {status}"
        raise UploadError(message, status=status)
    return status, parsed


def _parse_response(raw: bytes) -> tuple[int, object]:
    text = raw.decode("utf-8", "replace")
    marker = text.find("\r\n\r\n")
    sep = 4
    if marker < 0:
        marker = text.find("\n\n")
        sep = 2
    head = text if marker < 0 else text[:marker]
    tail = "" if marker < 0 else text[marker + sep :]
    status = 0
    for line in head.splitlines():
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
    parsed: object = tail
    stripped = tail.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = tail
    return status, parsed


def _api_message(payload: object) -> str:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message[:300]
    return ""


def _split_repo(repo: str) -> tuple[str, str]:
    if repo not in ALLOWED_REPOS:
        raise UploadError("repository is not in the upload allowlist")
    owner, name = repo.split("/", 1)
    return owner, name


def load_snapshot(path: Path) -> tuple[dict, bytes]:
    if path.name != REMOTE_PATH:
        raise UploadError("local file name must be data.json")
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise UploadError(f"cannot read snapshot: {exc.strerror}") from exc
    if len(blob) > 65536:
        raise UploadError("snapshot exceeds 64 KiB")
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UploadError("snapshot is not UTF-8 JSON") from exc
    try:
        validate_public_snapshot(doc)
    except ValueError as exc:
        raise UploadError(f"snapshot rejected: {exc}") from exc
    return doc, blob


def _remote_doc(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    encoding = payload.get("encoding")
    if not isinstance(content, str) or encoding != "base64":
        return None
    try:
        decoded = base64.b64decode(content, validate=False)
        doc = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def decide(local: dict, remote: dict | None, *, now: datetime, max_age_sec: int) -> str:
    """Return skip, create, or update. Same metrics inside the fresh window skip."""
    if remote is None:
        return "create"
    try:
        validate_public_snapshot(remote)
        same_metrics = metrics_hash(local) == metrics_hash(remote)
    except ValueError:
        same_metrics = False
    if not same_metrics:
        return "update"
    local_dump = json.dumps(local, sort_keys=True, ensure_ascii=False, allow_nan=False)
    remote_dump = json.dumps(remote, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if local_dump == remote_dump:
        return "skip"
    published = parse_time(remote.get("published_at"))
    if published is None:
        return "update"
    age = (now - published).total_seconds()
    if 0 <= age <= max_age_sec:
        return "skip"
    return "update"


def put_snapshot(
    local_bytes: bytes,
    *,
    repo: str,
    branch: str,
    sha: str | None,
    api=gh_api,
) -> dict:
    owner, name = _split_repo(repo)
    payload = {
        "message": "paper-desk: refresh sanitized snapshot",
        "content": base64.b64encode(local_bytes).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    endpoint = f"/repos/{owner}/{name}/contents/{REMOTE_PATH}"
    _status, body = api("PUT", endpoint, payload)
    return body if isinstance(body, dict) else {}


def upload(
    path: Path,
    *,
    repo: str = "ahagaraya/paper-desk",
    branch: str = "main",
    max_age_sec: int = DEFAULT_MAX_AGE_SEC,
    dry_run: bool = False,
    now: datetime | None = None,
    api=gh_api,
) -> str:
    if branch not in ALLOWED_BRANCHES:
        raise UploadError("branch is not in the upload allowlist")
    if max_age_sec < 0 or max_age_sec > 86400:
        raise UploadError("max age is out of range")
    owner, name = _split_repo(repo)
    doc, blob = load_snapshot(path)
    moment = now or datetime.now(timezone.utc)
    endpoint = f"/repos/{owner}/{name}/contents/{REMOTE_PATH}?ref={branch}"
    remote_payload = None
    remote_sha = None
    try:
        _status, remote_payload = api("GET", endpoint, None)
    except UploadError as exc:
        if exc.status != 404:
            raise
    else:
        if isinstance(remote_payload, dict):
            sha = remote_payload.get("sha")
            remote_sha = sha if isinstance(sha, str) else None
    remote_doc = _remote_doc(remote_payload) if remote_payload is not None else None
    if remote_payload is not None and remote_doc is None:
        action = "update"
    else:
        action = decide(doc, remote_doc, now=moment, max_age_sec=max_age_sec)
    if action == "skip":
        return "skip"
    if dry_run:
        return action
    put_snapshot(blob, repo=repo, branch=branch, sha=remote_sha if action == "update" else None, api=api)
    return action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish data.json with the GitHub contents API.")
    parser.add_argument("--file", default="data.json", help="local data.json to upload")
    parser.add_argument("--repo", default=os.environ.get("PAPER_DESK_REPO", "ahagaraya/paper-desk"))
    parser.add_argument("--branch", default=os.environ.get("PAPER_DESK_BRANCH", "main"))
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SEC)
    parser.add_argument("--dry-run", action="store_true", help="validate and print the action only")
    parser.add_argument("--check", action="store_true", help="validate the file and do not call GitHub")
    args = parser.parse_args(argv)
    try:
        if args.check:
            doc, _blob = load_snapshot(Path(args.file))
            print(f"upload_data: {Path(args.file).name} ok hash={doc['content_hash'][:12]}")
            return 0
        action = upload(
            Path(args.file),
            repo=args.repo,
            branch=args.branch,
            max_age_sec=args.max_age,
            dry_run=args.dry_run,
        )
    except UploadError as exc:
        print(f"upload_data: {exc}", file=sys.stderr)
        return 1
    prefix = "dry-run: " if args.dry_run else ""
    print(f"upload_data: {prefix}{action} {REMOTE_PATH} on {args.repo}@{args.branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
