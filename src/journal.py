"""
Local, durable record of every asset's state, in sqlite so a crash
mid-run doesn't lose where we were.

The one rule everything else in this file exists to protect: nothing
becomes "confirmed" except through a response that actually proves it
(a fresh 201, a replayed 200, or a 409 whose body confirms the same
asset_id). A write attempt that merely *went out* is never enough on
its own - that's why in_flight is written BEFORE the request, not
after: if the process dies mid-request, the next run sees "this one
was left in flight, go resolve it with the stored key" instead of
silently forgetting it was ever tried (which would risk a second,
differently-keyed attempt - exactly how as-0001 got duplicated).

The idempotency key itself is generated once and stored the moment an
asset is first touched. Every later attempt - including retries and
resumed runs - reads that stored string back. It is never recomputed
from a function call, on purpose: a future code change to the
derivation logic must never be able to produce a different key for an
asset that's already been attempted.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import CONSTANTS as C

# status values
PENDING = "pending"            # not yet attempted, or ready to retry
IN_FLIGHT = "in_flight"        # a request is/was in flight; outcome unknown
CONFIRMED = "confirmed"        # proven via a definitive response
CLIENT_ERROR = "client_error"  # real no from the server, not retryable
AMBIGUOUS = "ambiguous"        # got back something we can't interpret; needs a same-key retry
PARENT_BLOCKED = "parent_blocked"  # its depends_on never got confirmed in time


@dataclass
class AssetRow:
    id: str
    kind: str
    title: str
    checksum: str
    depends_on: str | None
    idempotency_key: str | None
    status: str
    outcome: str | None
    at_ms: int | None
    reason: str | None
    attempts: int
    duplicate_suspected: bool


def connect(path: str = C.JOURNAL_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            checksum TEXT NOT NULL,
            depends_on TEXT,
            idempotency_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            outcome TEXT,
            at_ms INTEGER,
            reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            duplicate_suspected INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def seed_catalog(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    """Insert every catalog asset as 'pending' if it isn't already
    tracked. Safe to call again on a resumed run - never overwrites an
    existing row."""
    now = time.time()
    conn.executemany(
        """
        INSERT OR IGNORE INTO assets
            (id, kind, title, checksum, depends_on, status, attempts,
             duplicate_suspected, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)
        """,
        [
            (it["id"], it["kind"], it["title"], it["checksum"], it.get("depends_on"), now, now)
            for it in items
        ],
    )
    conn.commit()


def manual_seed(
    conn: sqlite3.Connection,
    asset_id: str,
    *,
    idempotency_key: str,
    outcome: str,
    at_ms: int | None,
    duplicate_suspected: bool = False,
) -> None:
    """Record a manual/out-of-band attempt (e.g. as-0001, as-0002, made
    directly against the API while exploring the write endpoint, before
    the automated loop existed) as already-resolved so the loop never
    re-attempts them. Requires the row to already exist from seed_catalog."""
    now = time.time()
    conn.execute(
        """
        UPDATE assets
        SET idempotency_key = ?, status = ?, outcome = ?, at_ms = ?,
            attempts = attempts + 1, duplicate_suspected = ?, updated_at = ?
        WHERE id = ?
        """,
        (idempotency_key, CONFIRMED, outcome, at_ms, int(duplicate_suspected), now, asset_id),
    )
    conn.commit()


def get_or_create_key(conn: sqlite3.Connection, asset_id: str) -> str:
    row = conn.execute("SELECT idempotency_key FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise KeyError(f"{asset_id} not in journal - seed_catalog first")
    if row["idempotency_key"]:
        return row["idempotency_key"]
    # Deliberately just the asset_id itself, no prefix/formatting - this is
    # exactly the scheme already proven safe on as-0002 (201 then replayed
    # 200 on identical resend). Reusing the *proven* pattern rather than
    # inventing a new one means there's nothing new here to get wrong.
    key = asset_id
    conn.execute(
        "UPDATE assets SET idempotency_key = ?, updated_at = ? WHERE id = ?",
        (key, time.time(), asset_id),
    )
    conn.commit()
    return key


def mark_in_flight(conn: sqlite3.Connection, asset_id: str) -> None:
    conn.execute(
        "UPDATE assets SET status = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (IN_FLIGHT, time.time(), asset_id),
    )
    conn.commit()


def mark_confirmed(conn: sqlite3.Connection, asset_id: str, outcome: str, at_ms: int | None) -> None:
    conn.execute(
        "UPDATE assets SET status = ?, outcome = ?, at_ms = ?, reason = NULL, updated_at = ? WHERE id = ?",
        (CONFIRMED, outcome, at_ms, time.time(), asset_id),
    )
    conn.commit()


def mark_client_error(conn: sqlite3.Connection, asset_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE assets SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
        (CLIENT_ERROR, reason, time.time(), asset_id),
    )
    conn.commit()


def mark_ambiguous(conn: sqlite3.Connection, asset_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE assets SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
        (AMBIGUOUS, reason, time.time(), asset_id),
    )
    conn.commit()


def mark_parent_blocked(conn: sqlite3.Connection, asset_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE assets SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
        (PARENT_BLOCKED, reason, time.time(), asset_id),
    )
    conn.commit()


def get_row(conn: sqlite3.Connection, asset_id: str) -> AssetRow | None:
    r = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return _to_row(r) if r else None


def all_rows(conn: sqlite3.Connection) -> list[AssetRow]:
    return [_to_row(r) for r in conn.execute("SELECT * FROM assets ORDER BY id").fetchall()]


def ready_to_attempt(conn: sqlite3.Connection) -> list[str]:
    """Assets that are pending or ambiguous (i.e. worth trying/retrying
    right now) whose dependency, if any, is already confirmed."""
    rows = conn.execute(
        """
        SELECT a.id FROM assets a
        LEFT JOIN assets p ON p.id = a.depends_on
        WHERE a.status IN (?, ?)
          AND (a.depends_on IS NULL OR p.status = ?)
        ORDER BY a.id
        """,
        (PENDING, AMBIGUOUS, CONFIRMED),
    ).fetchall()
    return [r["id"] for r in rows]


def blocked_on_dead_parent(conn: sqlite3.Connection) -> list[str]:
    """Pending/ambiguous assets whose parent is definitively not going
    to be confirmed (client_error or parent_blocked itself) - no point
    retrying these, they can be reported blocked now."""
    rows = conn.execute(
        """
        SELECT a.id FROM assets a
        JOIN assets p ON p.id = a.depends_on
        WHERE a.status IN (?, ?)
          AND p.status IN (?, ?)
        """,
        (PENDING, AMBIGUOUS, CLIENT_ERROR, PARENT_BLOCKED),
    ).fetchall()
    return [r["id"] for r in rows]


def _to_row(r: sqlite3.Row) -> AssetRow:
    return AssetRow(
        id=r["id"], kind=r["kind"], title=r["title"], checksum=r["checksum"],
        depends_on=r["depends_on"], idempotency_key=r["idempotency_key"],
        status=r["status"], outcome=r["outcome"], at_ms=r["at_ms"], reason=r["reason"],
        attempts=r["attempts"], duplicate_suspected=bool(r["duplicate_suspected"]),
    )
