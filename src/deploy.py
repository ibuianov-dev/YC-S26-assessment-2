"""
The actual runner.

Fetches the catalog, seeds the journal, then loops: each pass, attempt
every asset that's currently "ready" (pending or ambiguous, and its
depends_on - if any - is already confirmed). Because readiness is
re-derived from the journal on every pass rather than computed once as
a fixed sort order, this naturally handles dependency chains of any
depth without an explicit topological sort: roots get confirmed on
pass 1, their children become ready and get confirmed on pass 2, their
children's children on pass 3, and so on, however deep the real graph
turns out to go.

Stops issuing brand-new (never-attempted) writes once
run_minutes_remaining drops to STOP_NEW_WRITES_MINUTES, so there's
guaranteed runway left to resolve anything already in flight. Stops
everything, including retries, at HARD_STOP_MINUTES - past that point
an unresolved asset is reported blocked, not guessed as deployed.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import CONSTANTS as C
import journal
import reconcile
from client import ApiClient

# Known from manual exploration of the write endpoint, before this loop
# existed - see NOTES.md for the full story. Recorded here so the
# automated loop never re-attempts them (a re-attempt of as-0001 in
# particular would risk creating a *third* copy).
MANUAL_PRECHECK_RESULTS = [
    dict(
        asset_id="as-0001",
        idempotency_key="manual-precheck-mismatched-key",
        outcome="success_fresh",
        at_ms=None,  # not recorded during manual exploration
        duplicate_suspected=True,  # a mistyped retry key produced a second 201 - see NOTES.md
    ),
    dict(
        asset_id="as-0002",
        idempotency_key="as-0002",
        outcome="success_replayed",
        at_ms=1788469896427,
        duplicate_suspected=False,
    ),
    dict(
        asset_id="as-0012",
        idempotency_key="as-0012",
        outcome="success_fresh",
        at_ms=None,  # not recorded during manual exploration
        duplicate_suspected=False,  # clean single 201, correct key confirmed
    ),
]


def bootstrap(conn: sqlite3.Connection, client: ApiClient) -> None:
    items = client.get_all_assets()
    if len(items) != 300:
        print(f"WARNING: catalog returned {len(items)} items, expected 300")
    journal.seed_catalog(conn, items)
    for rec in MANUAL_PRECHECK_RESULTS:
        row = journal.get_row(conn, rec["asset_id"])
        if row is not None and row.status == journal.PENDING:
            journal.manual_seed(
                conn, rec["asset_id"],
                idempotency_key=rec["idempotency_key"],
                outcome=rec["outcome"],
                at_ms=rec["at_ms"],
                duplicate_suspected=rec["duplicate_suspected"],
            )


def run(conn: sqlite3.Connection, client: ApiClient) -> None:
    while True:
        try:
            _do_pass(conn, client)
        except _StopRun:
            return
        except Exception as exc:
            # Anything unexpected here (a transient DB error, an ApiError
            # that got past client.py's own retries, etc.) should not kill
            # the whole process - the journal is durable, so the safe move
            # is to log it, back off briefly, and let the next pass retry
            # whatever was in progress rather than silently losing however
            # much of the clock is left because nobody was watching.
            print(f"pass failed unexpectedly, backing off and retrying: {exc}")
            time.sleep(5)


class _StopRun(Exception):
    """Internal signal that a pass decided the run is over (done, or out
    of time) - lets _do_pass just raise instead of threading a sentinel
    return value through run()'s try/except."""


def _do_pass(conn: sqlite3.Connection, client: ApiClient) -> None:
    remaining = client.run_minutes_remaining()
    if remaining <= C.HARD_STOP_MINUTES:
        print(f"HARD STOP: {remaining} min remaining, leaving anything unresolved as-is")
        raise _StopRun

    # Anything whose parent is definitively dead can be marked now
    # instead of sitting in "ready" limbo for the rest of the run.
    for asset_id in journal.blocked_on_dead_parent(conn):
        row = journal.get_row(conn, asset_id)
        if row is None or row.depends_on is None:
            continue  # blocked_on_dead_parent only returns rows with a live parent id
        parent = journal.get_row(conn, row.depends_on)
        reason = f"parent {row.depends_on} did not deploy (status={parent.status if parent else '?'})"
        journal.mark_parent_blocked(conn, asset_id, reason)

    ready = journal.ready_to_attempt(conn)
    if remaining <= C.STOP_NEW_WRITES_MINUTES:
        ready = [
            aid for aid in ready
            if (r := journal.get_row(conn, aid)) is not None and r.attempts > 0
        ]
        if not ready:
            print(f"Only new-write candidates left and {remaining} min remaining - stopping.")
            raise _StopRun

    if not ready:
        still_open = [
            r for r in journal.all_rows(conn)
            if r.status in (journal.PENDING, journal.IN_FLIGHT, journal.AMBIGUOUS)
        ]
        if not still_open:
            print("Nothing left to do.")
            raise _StopRun
        # Nothing ready right now but work remains (waiting on parents
        # that are themselves still ambiguous) - brief pause, loop again.
        time.sleep(2)
        return

    for asset_id in ready:
        remaining = client.run_minutes_remaining()
        if remaining <= C.HARD_STOP_MINUTES:
            print(f"HARD STOP mid-pass: {remaining} min remaining")
            raise _StopRun
        try:
            status = reconcile.attempt(conn, client, asset_id)
            print(f"{asset_id}: {status}")
        except Exception as exc:
            # reconcile.attempt() writes in_flight *before* the request
            # goes out. If it dies partway through (a bug, an exception
            # client.py didn't turn into a DeployResult), the row would
            # otherwise be stuck at in_flight forever - ready_to_attempt()
            # only ever picks up pending/ambiguous, so it would silently
            # never be retried. Explicitly put it back in the retryable
            # pool instead of trusting whatever state it was left in.
            print(f"{asset_id}: unexpected error, marking ambiguous for retry: {exc}")
            journal.mark_ambiguous(conn, asset_id, f"unexpected exception during attempt: {exc}")


def main() -> None:
    client = ApiClient()
    conn = journal.connect()
    print(f"run_minutes_remaining at start of this process: {client.run_minutes_remaining()}")
    bootstrap(conn, client)
    run(conn, client)

    rows = journal.all_rows(conn)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("Final journal status counts:", counts)


if __name__ == "__main__":
    main()
