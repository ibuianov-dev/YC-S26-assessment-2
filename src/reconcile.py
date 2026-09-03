"""
The one place that turns a deploy attempt into a real answer:
confirmed, a real no, or still-unresolved. Used for both the first
attempt at an asset and every retry of it, so there is exactly one
definition of "proof" in this codebase, not two slightly different
ones living in deploy.py and report.py.

There is no read endpoint for deploy state (confirmed during
discovery - GET /s2/assets/{id}, GET /s2/deploy/{id}, and
GET /s2/deploy?asset_id={id} all 404 regardless of reality, and the
catalog listing never reflects destination state). So "proof" here can
only ever come from a deploy response itself:

  - a fresh 201, or
  - a replayed 200 (replayed: true), or
  - a 409 whose body confirms the same asset_id

Anything else - a real 4xx, or a response we can't interpret at all -
is not proof of anything and must not be recorded as confirmed.
"""

from __future__ import annotations

import sqlite3

import journal
from client import ApiClient, Outcome


def attempt(conn: sqlite3.Connection, client: ApiClient, asset_id: str) -> str:
    """Makes one deploy attempt for asset_id using its stored (or
    newly-created-and-stored) idempotency key, and updates the journal
    to reflect what the response actually proved. Returns the resulting
    journal status."""
    row = journal.get_row(conn, asset_id)
    if row is None:
        raise KeyError(f"{asset_id} not in journal")

    key = journal.get_or_create_key(conn, asset_id)

    # Written BEFORE the request goes out: if this process dies mid-request,
    # the next run finds this asset in_flight and knows to resolve it with
    # this exact stored key, not start over with a fresh one.
    journal.mark_in_flight(conn, asset_id)

    result = client.deploy_asset(asset_id, row.checksum, key)

    if result.outcome in (Outcome.SUCCESS_FRESH, Outcome.SUCCESS_REPLAYED):
        journal.mark_confirmed(conn, asset_id, result.outcome.value, result.at_ms)
        return journal.CONFIRMED

    if result.outcome == Outcome.CONFLICT_MATCH:
        # 409 whose body confirms this exact asset_id already exists under
        # this key - that's still proof it's there, just not proof it's
        # there *exactly once*. Confirmed for reporting purposes; flagged
        # for the honesty write-up either way.
        journal.mark_confirmed(conn, asset_id, result.outcome.value, result.at_ms)
        return journal.CONFIRMED

    if result.outcome == Outcome.CLIENT_ERROR:
        journal.mark_client_error(conn, asset_id, result.detail or "client error")
        return journal.CLIENT_ERROR

    # AMBIGUOUS or CONFLICT_UNCLEAR: we genuinely don't know. Leave it
    # retryable - a later pass in deploy.py will resend this exact same
    # (asset_id, checksum, key) and let whatever comes back resolve it.
    journal.mark_ambiguous(conn, asset_id, result.detail or f"unresolved: {result.outcome.value}")
    return journal.AMBIGUOUS
