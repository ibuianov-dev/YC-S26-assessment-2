"""
The only place that talks to the API.

Owns: auth headers (X-Candidate-Key + Bearer), proactive token refresh,
retry/backoff for connection errors and 429/503, User-Agent, and turning
every response into one of a small set of outcomes the rest of the code
can reason about without re-deriving "what does this status code mean
*here*" every time.

Nothing in here decides what an outcome means for the report - that's
reconcile.py's job. This module just tells the truth about what the
server actually said.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import CONSTANTS as C


class ApiError(RuntimeError):
    """Something is wrong with how we're calling the API (not a normal
    business outcome - e.g. we tried to deploy without a key, or auth
    itself is broken). These should never happen if the rest of the
    code is correct; they are bugs, not "the provider said no"."""


class Outcome(Enum):
    SUCCESS_FRESH = "success_fresh"        # 201 - this exact request created it, right now
    SUCCESS_REPLAYED = "success_replayed"  # 200 replayed:true - server already had this key, same effect
    CONFLICT_MATCH = "conflict_match"      # 409 whose body confirms the same asset_id already exists
    CONFLICT_UNCLEAR = "conflict_unclear"  # 409 that doesn't confirm what it's conflicting with
    CLIENT_ERROR = "client_error"          # other 4xx - real no, don't retry
    AMBIGUOUS = "ambiguous"                # timeout / dropped connection / 5xx we couldn't resolve


@dataclass
class DeployResult:
    outcome: Outcome
    status_code: int | None
    body: dict[str, Any] | None
    at_ms: int | None          # server-reported timestamp, when present
    detail: str | None         # human-readable "why", for client_error / ambiguous / conflict_unclear


@dataclass
class _AuthState:
    access_token: str
    expires_at_monotonic: float
    run_minutes_remaining: int
    refreshed_at_monotonic: float


class ApiClient:
    def __init__(self, base_url: str = C.BASE_URL, candidate_key: str = C.MY_KEY) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._candidate_key = candidate_key
        self._session = requests.Session()
        self._session.headers["User-Agent"] = C.USER_AGENT
        self._auth: _AuthState | None = None

    # ---------------------------------------------------------------- auth

    def _refresh_auth(self) -> None:
        resp = self._session.post(
            self._base_url + C.AUTH_ENDPOINT_POST,
            headers={"X-Candidate-Key": self._candidate_key},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        now = time.monotonic()
        self._auth = _AuthState(
            access_token=body["access_token"],
            expires_at_monotonic=now + float(body["expires_in"]),
            run_minutes_remaining=int(body["run_minutes_remaining"]),
            refreshed_at_monotonic=now,
        )

    def _ensure_auth(self) -> _AuthState:
        if self._auth is None:
            self._refresh_auth()
        else:
            lifetime = self._auth.expires_at_monotonic - self._auth.refreshed_at_monotonic
            elapsed = time.monotonic() - self._auth.refreshed_at_monotonic
            if elapsed >= lifetime * C.TOKEN_REFRESH_FRACTION:
                self._refresh_auth()
        assert self._auth is not None
        return self._auth

    def run_minutes_remaining(self) -> int:
        """Best known value. Only updates on a refresh, so it's a
        conservative (slightly stale-high) estimate between refreshes."""
        return self._ensure_auth().run_minutes_remaining

    def _headers(self) -> dict[str, str]:
        auth = self._ensure_auth()
        return {
            "X-Candidate-Key": self._candidate_key,
            "Authorization": f"Bearer {auth.access_token}",
        }

    # --------------------------------------------------------- low-level http

    def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        max_attempts: int = 4,
    ) -> tuple[requests.Response | None, str | None]:
        """Retries connection errors and 429/503 with backoff. Returns
        (response, None) on getting any HTTP response back, or
        (None, error_description) if we truly never got an answer."""
        last_error: str | None = None
        for attempt in range(max_attempts):
            if attempt:
                time.sleep(min(2**attempt, 20))
            headers = self._headers()
            if extra_headers:
                headers.update(extra_headers)
            try:
                resp = self._session.request(
                    method,
                    self._base_url + path,
                    headers=headers,
                    json=json_body,
                    timeout=20,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if resp.status_code in (429, 503):
                last_error = f"rate limited ({resp.status_code})"
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), 30))
                    except ValueError:
                        pass
                continue

            return resp, None

        return None, last_error

    # ------------------------------------------------------------- assets

    def get_assets_page(self, cursor: str | None = None) -> dict[str, Any]:
        path = C.ASSETS_ENDPOINT_GET
        if cursor is not None:
            path += f"?cursor={cursor}"
        resp, err = self._request_with_retry("GET", path)
        if resp is None:
            raise ApiError(f"could not fetch assets page (cursor={cursor}): {err}")
        resp.raise_for_status()
        return resp.json()

    def get_all_assets(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = self.get_assets_page(cursor)
            items.extend(page["items"])
            next_cursor = page.get("next_cursor")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return items

    # ------------------------------------------------------------- deploy

    def deploy_asset(self, asset_id: str, checksum: str, idempotency_key: str) -> DeployResult:
        if not idempotency_key:
            # Never send an unkeyed write - see NOTES.md: unkeyed and keyed
            # requests use two different dedup mechanisms server-side, and
            # mixing them is exactly what caused the as-0001 duplicate.
            raise ApiError(f"refusing to deploy {asset_id} without an idempotency key")

        resp, err = self._request_with_retry(
            "POST",
            C.DEPLOY_ENDPOINT_POST,
            json_body={"asset_id": asset_id, "checksum": checksum},
            extra_headers={C.IDEMPOTENCY_HEADER: idempotency_key},
        )
        if resp is None:
            return DeployResult(Outcome.AMBIGUOUS, None, None, None, err)

        body: dict[str, Any] | None
        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code == 201:
            return DeployResult(Outcome.SUCCESS_FRESH, 201, body, (body or {}).get("at"), None)

        if resp.status_code == 200 and body and body.get("replayed") is True:
            return DeployResult(Outcome.SUCCESS_REPLAYED, 200, body, body.get("at"), None)

        if resp.status_code == 409:
            returned_id = (body or {}).get("asset_id")
            if returned_id == asset_id:
                return DeployResult(Outcome.CONFLICT_MATCH, 409, body, (body or {}).get("at"), None)
            return DeployResult(
                Outcome.CONFLICT_UNCLEAR, 409, body, None,
                f"409 didn't confirm asset_id={asset_id} (body={body})",
            )

        if 400 <= resp.status_code < 500:
            return DeployResult(
                Outcome.CLIENT_ERROR, resp.status_code, body, None,
                f"{resp.status_code}: {body if body is not None else resp.text[:300]}",
            )

        # Any other status (5xx that wasn't 503, or something unexpected) -
        # we can't claim to know what happened server-side.
        return DeployResult(
            Outcome.AMBIGUOUS, resp.status_code, body, None,
            f"unexpected status {resp.status_code}: {body if body is not None else resp.text[:300]}",
        )

    # ------------------------------------------------------------- report
    # Deliberately not called anywhere else in this module or by deploy.py.
    # Sending this seals the run - see report.py for the explicit,
    # separate, on-purpose submission step.

    def submit_report(self, report: dict[str, Any]) -> requests.Response:
        resp, err = self._request_with_retry(
            "POST", C.REPORT_ENDPOINT_POST, json_body=report, max_attempts=1,
        )
        if resp is None:
            raise ApiError(f"report submission got no response at all: {err}")
        return resp
