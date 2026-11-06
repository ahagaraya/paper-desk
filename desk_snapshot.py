#!/usr/bin/env python3
"""Sanitized public snapshot for the paper desk.

Builds a fixed-key document from local paper-state files. The public
document carries equity, cash, cumulative PnL since the $1,000 start,
open positions, recent closes, risk flags, and timestamps. It never
copies raw logs, paths, credentials, or unknown fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "paper-desk.v1"
STARTING_EQUITY = 1000.0
MSK = timezone(timedelta(hours=3))
MAX_OPENS = 30
MAX_CLOSES = 10
MAX_REASONS = 8

# Personal name that must not appear on the public desk.
_FORBIDDEN_NAME = "\u041c\u0430\u0440\u0438"

AGENTS = (
    {"id": "scanner", "name": "Scanner", "role": "pair search"},
    {"id": "rugcheck", "name": "RugCheck", "role": "risk filter"},
    {"id": "sizer", "name": "Sizer", "role": "position size"},
    {"id": "executor", "name": "Executor", "role": "entries and exits"},
    {"id": "equity", "name": "Equity", "role": "accounting and risk"},
    {"id": "logger", "name": "Logger", "role": "records"},
    {"id": "news", "name": "News", "role": "headlines and halt"},
    {"id": "orchestrator", "name": "Orchestrator", "role": "coordination"},
)

ROOT_KEYS = (
    "schema",
    "mode",
    "simulation",
    "live",
    "currency",
    "starting_equity",
    "equity",
    "cash",
    "pnl_since_start",
    "pnl_since_start_pct",
    "realized",
    "unrealized",
    "wins",
    "losses",
    "closed_trades",
    "wl",
    "peak_equity",
    "drawdown_from_peak_pct",
    "max_drawdown_pct",
    "halt_new_entries",
    "halt_reasons",
    "book",
    "open_count",
    "open",
    "recent_closes",
    "agents",
    "source_updated_at",
    "source_age_at_publish_sec",
    "published_at",
    "content_hash",
    "generated_by",
)

HASH_EXCLUDE = frozenset(
    {"published_at", "source_age_at_publish_sec", "content_hash"}
)

OPEN_KEYS = ("token", "size_usd", "entry", "mark", "unrealized", "opened_at")
CLOSE_KEYS = ("token", "realized", "outcome", "exits", "closed_at")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_EXIT_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_REASON_RE = re.compile(r"[A-Za-z0-9 .,_:+-]{1,80}")
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_BRAND_RE = re.compile("cur" + "sor|" + "gro" + "k", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(ghp_|github_pat_|gho_|ghs_|ghu_|xox[baprs]-|"
    r"BEGIN (?:RSA |OPENSSH )?PRIVATE|AKIA[0-9A-Z]{16}|"
    r"api[_-]?key|secret|password|mnemonic|seed phrase)",
    re.IGNORECASE,
)


class StateMissing(FileNotFoundError):
    """Paper state files are not available. Do not invent a book."""


def dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def metrics_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k not in HASH_EXCLUDE}
    blob = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _finite(value, places: int | None = None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if places is None:
        return number
    return round(number, places)


def _usd(value) -> float:
    number = _finite(value, 6)
    if number is None:
        raise ValueError("expected a finite amount")
    return number


def _int(value, default: int | None = None) -> int:
    if isinstance(value, bool) or value is None:
        if default is None:
            raise ValueError("expected an integer")
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip() or ""):
        return int(value.strip())
    if default is None:
        raise ValueError("expected an integer")
    return default


def _bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError("expected a boolean")


def _dirty(text: str) -> bool:
    return bool(
        _BRAND_RE.search(text)
        or _FORBIDDEN_NAME in text
        or _SECRET_RE.search(text)
    )


def _token(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    text = value.strip()
    if not _TOKEN_RE.fullmatch(text) or _dirty(text):
        return "unknown"
    return text


def _stamp(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if parse_time(text) is None or _dirty(text):
        return None
    return text


def _reason(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if (
        not text
        or len(text) > 80
        or "/" in text
        or "\\" in text
        or _dirty(text)
        or not _REASON_RE.fullmatch(text)
    ):
        return None
    return text


def _load_object(path: Path) -> dict:
    if not path.is_file():
        raise StateMissing(f"missing {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateMissing(f"unreadable {path.name}") from exc
    if not isinstance(data, dict):
        raise StateMissing(f"{path.name} is not an object")
    return data


def _mode(equity: dict, risk: dict) -> str:
    raw = equity.get("mode", risk.get("mode", "paper"))
    if not isinstance(raw, str):
        return "unknown"
    token = raw.strip().lower()
    if token in {"paper", "simulation", "sim", "live"}:
        return "paper" if token in {"simulation", "sim"} else token
    return "unknown"


def _pick_time(equity: dict, risk: dict, positions: dict) -> str | None:
    for source in (equity, risk, positions):
        stamp = _stamp(source.get("updated_at"))
        if stamp:
            return stamp
    return None


def _opens(positions: dict) -> list[dict]:
    rows = positions.get("open")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("positions open list is invalid")
    if len(rows) > MAX_OPENS:
        raise ValueError("too many open positions to publish")
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("open position is invalid")
        token = _token(row.get("token") or row.get("symbol") or row.get("ticker"))
        size = _finite(row.get("size_usd", row.get("notional_usd")), 6)
        if size is None:
            raise ValueError("open position size is invalid")
        entry = _finite(row.get("entry_price", row.get("avg_entry")), 8)
        mark = _finite(row.get("mark", row.get("mark_price")), 8)
        unrealized = _finite(row.get("unrealized_pnl"), 6)
        if unrealized is None:
            raise ValueError("open position unrealized pnl is invalid")
        cleaned.append(
            {
                "token": token,
                "size_usd": size,
                "entry": entry,
                "mark": mark,
                "unrealized": unrealized,
                "opened_at": _stamp(row.get("opened_at") or row.get("ts")),
            }
        )
    return cleaned


def _closes(risk: dict) -> list[dict]:
    detail = risk.get("closed_detail")
    rows: list[dict] = []
    if isinstance(detail, list) and detail:
        source_rows = detail[-MAX_CLOSES:]
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            realized = _finite(row.get("realized"), 6)
            if realized is None:
                continue
            outcome = row.get("outcome")
            if outcome not in {"win", "loss", "flat"}:
                outcome = "win" if realized > 0 else "loss" if realized < 0 else "flat"
            exits = []
            for item in (row.get("exits") or [])[:6]:
                if isinstance(item, str) and _EXIT_RE.fullmatch(item) and not _dirty(item):
                    exits.append(item)
            rows.append(
                {
                    "token": _token(row.get("token")),
                    "realized": realized,
                    "outcome": outcome,
                    "exits": exits,
                    "closed_at": _stamp(row.get("closed_at") or row.get("ts")),
                }
            )
        return list(reversed(rows))

    legacy = risk.get("closes")
    if not isinstance(legacy, list):
        return []
    for row in legacy[-MAX_CLOSES:]:
        if not isinstance(row, dict):
            continue
        realized = _finite(row.get("realized_pnl_usd", row.get("realized")), 6)
        if realized is None:
            continue
        exit_type = row.get("exit_type")
        exits = (
            [exit_type]
            if isinstance(exit_type, str) and _EXIT_RE.fullmatch(exit_type)
            else []
        )
        rows.append(
            {
                "token": _token(row.get("token")),
                "realized": realized,
                "outcome": "win" if realized > 0 else "loss" if realized < 0 else "flat",
                "exits": exits,
                "closed_at": _stamp(row.get("ts") or row.get("closed_at")),
            }
        )
    return list(reversed(rows))


def _reasons(risk: dict) -> list[str]:
    raw = risk.get("halt_reasons") or []
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw:
        reason = _reason(item)
        if reason and reason not in cleaned:
            cleaned.append(reason)
        if len(cleaned) >= MAX_REASONS:
            break
    return cleaned


def build_snapshot(state_dir: Path, now: datetime | None = None) -> dict:
    state_dir = Path(state_dir)
    equity = _load_object(state_dir / "equity.json")
    risk = _load_object(state_dir / "risk_status.json")
    positions = _load_object(state_dir / "positions.json")

    if "equity" not in equity and "equity_mtm" not in risk:
        raise StateMissing("equity is absent; refusing to invent a snapshot")
    if "cash" not in equity and "cash" not in risk:
        raise StateMissing("cash is absent; refusing to invent a snapshot")

    eq = _usd(equity["equity"] if "equity" in equity else risk["equity_mtm"])
    cash = _usd(equity["cash"] if "cash" in equity else risk["cash"])
    realized = _usd(equity.get("realized_pnl", risk.get("realized_pnl", 0)) or 0)
    unrealized = _usd(equity.get("unrealized_pnl", risk.get("unrealized_pnl", 0)) or 0)
    wins = _int(equity.get("wins", risk.get("wins", 0)), 0)
    losses = _int(equity.get("losses", risk.get("losses", 0)), 0)
    closed = _int(equity.get("closed_trades", risk.get("closed_trades", 0)), 0)
    if min(wins, losses, closed) < 0:
        raise ValueError("trade counts cannot be negative")

    peak_raw = equity.get("peak_equity", risk.get("peak_equity"))
    peak = _finite(peak_raw, 6) if peak_raw is not None else None
    if peak is not None and peak > 0:
        drawdown = round(((eq / peak) - 1.0) * 100.0, 4)
    else:
        peak = None
        drawdown = None

    max_raw = equity.get("max_drawdown_pct", risk.get("max_drawdown_pct"))
    max_dd = None if max_raw is None else round(abs(_usd(max_raw)), 4)

    currency = equity.get("currency", risk.get("currency", "USD"))
    if currency not in {None, "", "USD"}:
        raise ValueError("only USD snapshots are published")

    mode = _mode(equity, risk)
    live = _bool(equity.get("live", risk.get("live", False)), False)
    published = now or datetime.now(MSK).replace(microsecond=0)
    if published.tzinfo is None:
        published = published.replace(tzinfo=MSK)
    published_at = published.isoformat(timespec="seconds")
    source_updated_at = _pick_time(equity, risk, positions)
    source_age = None
    if source_updated_at:
        source_age = int((published - parse_time(source_updated_at)).total_seconds())

    opens = _opens(positions)
    book = "open" if opens else "flat"
    pnl = round(eq - STARTING_EQUITY, 6)
    pnl_pct = round(((eq / STARTING_EQUITY) - 1.0) * 100.0, 4)

    doc = {
        "schema": SCHEMA,
        "mode": mode,
        "simulation": mode == "paper",
        "live": live,
        "currency": "USD",
        "starting_equity": STARTING_EQUITY,
        "equity": eq,
        "cash": cash,
        "pnl_since_start": pnl,
        "pnl_since_start_pct": pnl_pct,
        "realized": realized,
        "unrealized": unrealized,
        "wins": wins,
        "losses": losses,
        "closed_trades": closed,
        "wl": f"{wins}/{losses}",
        "peak_equity": peak,
        "drawdown_from_peak_pct": drawdown,
        "max_drawdown_pct": max_dd,
        "halt_new_entries": _bool(risk.get("halt_new_entries", False), False),
        "halt_reasons": _reasons(risk),
        "book": book,
        "open_count": len(opens),
        "open": opens,
        "recent_closes": _closes(risk),
        "agents": [dict(row) for row in AGENTS],
        "source_updated_at": source_updated_at,
        "source_age_at_publish_sec": source_age,
        "published_at": published_at,
        "content_hash": "",
        "generated_by": "publish_data.py",
    }
    doc["content_hash"] = metrics_hash(doc)
    validate_public_snapshot(doc)
    return doc


def validate_public_snapshot(doc: dict) -> None:
    if not isinstance(doc, dict):
        raise ValueError("snapshot is not an object")
    if tuple(doc.keys()) != ROOT_KEYS:
        extra = set(doc) - set(ROOT_KEYS)
        missing = [key for key in ROOT_KEYS if key not in doc]
        raise ValueError(f"snapshot keys mismatch missing={missing} extra={sorted(extra)}")
    if doc["schema"] != SCHEMA:
        raise ValueError("unexpected schema")
    if doc["mode"] not in {"paper", "live", "unknown"}:
        raise ValueError("unexpected mode")
    if not isinstance(doc["simulation"], bool) or not isinstance(doc["live"], bool):
        raise ValueError("simulation and live must be booleans")
    if doc["simulation"] != (doc["mode"] == "paper"):
        raise ValueError("simulation flag does not match mode")
    if doc["currency"] != "USD" or doc["starting_equity"] != STARTING_EQUITY:
        raise ValueError("baseline must stay USD 1000")
    for key in (
        "equity",
        "cash",
        "pnl_since_start",
        "pnl_since_start_pct",
        "realized",
        "unrealized",
    ):
        if isinstance(doc[key], bool) or _finite(doc[key]) is None:
            raise ValueError(f"{key} is not a finite number")
    expected_pnl = round(float(doc["equity"]) - STARTING_EQUITY, 6)
    if abs(float(doc["pnl_since_start"]) - expected_pnl) > 1e-6:
        raise ValueError("pnl_since_start must be equity minus 1000")
    expected_pct = round(((float(doc["equity"]) / STARTING_EQUITY) - 1.0) * 100.0, 4)
    if abs(float(doc["pnl_since_start_pct"]) - expected_pct) > 1e-6:
        raise ValueError("pnl_since_start_pct does not match equity")
    if "day_pnl" in doc or "day_pnl_pct" in doc:
        raise ValueError("daily pnl fields are not part of the public snapshot")
    for key in ("wins", "losses", "closed_trades", "open_count"):
        if isinstance(doc[key], bool) or not isinstance(doc[key], int) or doc[key] < 0:
            raise ValueError(f"{key} is invalid")
    if doc["wl"] != f"{doc['wins']}/{doc['losses']}":
        raise ValueError("wl does not match wins and losses")
    if doc["book"] not in {"flat", "open"}:
        raise ValueError("book must be flat or open")
    if not isinstance(doc["halt_new_entries"], bool):
        raise ValueError("halt flag is invalid")
    if not isinstance(doc["halt_reasons"], list) or any(
        not isinstance(item, str) for item in doc["halt_reasons"]
    ):
        raise ValueError("halt reasons are invalid")
    if not isinstance(doc["open"], list) or doc["open_count"] != len(doc["open"]):
        raise ValueError("open_count does not match open positions")
    if doc["book"] != ("open" if doc["open"] else "flat"):
        raise ValueError("book does not match open positions")
    for row in doc["open"]:
        if not isinstance(row, dict) or tuple(row.keys()) != OPEN_KEYS:
            raise ValueError("open position keys mismatch")
        if row["token"] == "" or _dirty(str(row["token"])):
            raise ValueError("open token is invalid")
    if not isinstance(doc["recent_closes"], list) or len(doc["recent_closes"]) > MAX_CLOSES:
        raise ValueError("recent closes are invalid")
    for row in doc["recent_closes"]:
        if not isinstance(row, dict) or tuple(row.keys()) != CLOSE_KEYS:
            raise ValueError("close keys mismatch")
        if row["outcome"] not in {"win", "loss", "flat"}:
            raise ValueError("close outcome is invalid")
        if not isinstance(row["exits"], list):
            raise ValueError("close exits are invalid")
    if doc["agents"] != [dict(row) for row in AGENTS]:
        raise ValueError("desk roles were modified")
    if parse_time(doc["published_at"]) is None:
        raise ValueError("published_at is invalid")
    if doc["source_updated_at"] is not None and parse_time(doc["source_updated_at"]) is None:
        raise ValueError("source_updated_at is invalid")
    if doc["content_hash"] != metrics_hash(doc):
        raise ValueError("content hash does not match the snapshot")
    if doc["generated_by"] != "publish_data.py":
        raise ValueError("unexpected generator")
    for key in ("peak_equity", "drawdown_from_peak_pct", "max_drawdown_pct"):
        if doc[key] is not None and (isinstance(doc[key], bool) or _finite(doc[key]) is None):
            raise ValueError(f"{key} is invalid")
    encoded = dump(doc)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("snapshot exceeds 64 KiB")
    if _dirty(encoded) or "paper_path" in encoded or "trading-paper" in encoded:
        raise ValueError("snapshot contains a forbidden string")
    if "day_pnl" in encoded:
        raise ValueError("snapshot contains a daily pnl field")
