"""
Builds the report from the journal - the journal is the only source of
truth available (there is no read endpoint to double-check against, see
reconcile.py) - and, as a genuinely separate and deliberate step, can
submit it.

Only journal.CONFIRMED becomes "deployed". Every other status -
client_error, ambiguous, parent_blocked, or never having reached a
terminal state at all - becomes "blocked" with a reason. "Probably
fine" is not "confirmed": an asset that got an ambiguous response and
was never subsequently resolved to a definitive one is reported
blocked, even if it's quite likely it actually deployed. See NOTES.md
for why that bar is where it is.

POST /s2/report seals the run - there is exactly one real shot at it.
This module never calls it as a side effect of anything else; the only
path to actually sending it is running this file directly and typing
the confirmation phrase.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import journal
from client import ApiClient

REPORT_OUTPUT_PATH = "report.json"


def build_report(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    report: dict[str, dict[str, str]] = {}
    for row in journal.all_rows(conn):
        if row.status == journal.CONFIRMED:
            report[row.id] = {"status": "deployed"}
        else:
            reason = row.reason or f"never reached a confirmed state (status={row.status})"
            report[row.id] = {"status": "blocked", "reason": reason}
    return report


def summarize(conn: sqlite3.Connection) -> None:
    rows = journal.all_rows(conn)
    deployed = [r for r in rows if r.status == journal.CONFIRMED]
    blocked = [r for r in rows if r.status != journal.CONFIRMED]
    suspected_dupes = [r for r in rows if r.duplicate_suspected]
    print(f"total tracked: {len(rows)}")
    print(f"deployed: {len(deployed)}")
    print(f"blocked: {len(blocked)}")
    if suspected_dupes:
        print(f"duplicate_suspected (reported deployed, disclosed in NOTES.md): "
              f"{[r.id for r in suspected_dupes]}")
    by_status: dict[str, int] = {}
    for r in blocked:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    if by_status:
        print("blocked breakdown:", by_status)


def save_locally(report: dict, path: str = REPORT_OUTPUT_PATH) -> None:
    with open(path, "w") as f:
        json.dump({"report": report}, f, indent=2, sort_keys=True)
    print(f"wrote {path} ({len(report)} assets) - review this before submitting")


def submit_for_real(client: ApiClient, report: dict) -> None:
    """The one and only place /s2/report is called. Requires typing the
    confirmation phrase - there is no programmatic bypass, on purpose."""
    print(f"About to submit a report covering {len(report)} assets.")
    print("This seals the run. There is no undo and no second attempt.")
    typed = input('Type "SUBMIT" (all caps) to proceed, anything else aborts: ')
    if typed != "SUBMIT":
        print("Aborted. Nothing was sent.")
        return
    resp = client.submit_report({"report": report})
    print(f"Response: {resp.status_code}")
    print(resp.text)


def main() -> None:
    conn = journal.connect()
    summarize(conn)
    report = build_report(conn)
    save_locally(report)

    if "--submit" in sys.argv:
        client = ApiClient()
        submit_for_real(client, report)
    else:
        print("\nDry run only - rerun with --submit to actually seal the report.")


if __name__ == "__main__":
    main()
