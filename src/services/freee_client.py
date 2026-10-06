"""AgentCore Platform v1.0 - freee accounting REST API client.

Service layer: a thin wrapper around the freee (accounting SaaS) REST API
manual-journal and trial-balance endpoints. Contains NO business logic, NO
routing, and NO credentials - the integration token is passed in per call by
the node (which reads it via ctx.secrets). This module imports no
framework/SDK internals - pure stdlib (import-isolation, PB-4).

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented freee response shapes (a ``manual_journal`` object for lookups
    and creates - with a synthetic ``id`` echo derived from the request; a
    ``trial_bs.balances`` list for balance checks) so the pipeline is runnable
    and testable without a live freee company or the ``requests`` package - it
    does NOT perform a live freee call. The principle is to document the
    limitation rather than fake the call.

    To perform real freee calls, inject live transports (requests-based
    ``post`` / ``get``) at construction time; the method contracts and payload
    shapes follow the documented freee accounting REST API
    (``/api/1/manual_journals``, ``/api/1/reports/trial_bs``), so no
    business-logic change is needed to go live. Live calls also require the
    ``company_id`` query parameter (accepted at construction, unused by the
    stub) and a real integration token (see CallFreeeApiNode - the stub runs
    without one because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.freee.co.jp/api/1"


class FreeeApiError(Exception):
    """Raised when the freee REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"freee API error {status_code}: {message}")


class FreeeClient:
    """freee accounting REST API client (manual journals + trial balance).

    Args:
        base_url: freee API base URL (default https://api.freee.co.jp/api/1).
        company_id: freee company id, required by live endpoints as a query
            parameter (unused by the network-free stub; supplied at go-live).
        post/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns the documented shape without
            a live freee call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        company_id: str = "",
        *,
        post: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._company_id = company_id
        self._post = post
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._post is None and self._get is None

    # -- auth / url ------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the freee REST API auth headers (OAuth2 bearer token).

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_token,
        }

    def _url(self, path: str) -> str:
        """Endpoint URL; live freee endpoints require company_id as a query param."""
        url = f"{self._base_url}{path}"
        if self._company_id:
            url = f"{url}?company_id={self._company_id}"
        return url

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free stub - returns the documented freee shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = json_body.get("_freee_op")
        if op == "lookup":
            journal_id = str(json_body.get("journal_id", "")) or str(int(digest[:6], 16))
            # Documented GET /manual_journals/{id} shape: {"manual_journal": {...}}
            return 200, {
                "manual_journal": {
                    "id": journal_id,
                    "issue_date": "1970-01-01",
                    "adjustment": False,
                    "details": [],
                },
                "_stub": True,  # marks the network-free v1 stub response
            }
        if op == "balance":
            account = str(json_body.get("account", "")) or f"account-{digest[:8]}"
            # Documented GET /reports/trial_bs shape: {"trial_bs": {"balances": [...]}}
            return 200, {
                "trial_bs": {
                    "balances": [
                        {
                            "account_item_name": account,
                            "closing_balance": int(digest[:6], 16),
                        }
                    ]
                },
                "_stub": True,  # marks the network-free v1 stub response
            }
        # POST /manual_journals (create) - documented {"manual_journal": {...}}
        # response, with a synthetic id echo so the caller can reference the
        # affected record without a follow-up lookup.
        return 200, {
            "manual_journal": {"id": str(int(digest[:6], 16))},
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def find_journal(self, journal_id: str, api_token: str) -> "dict[str, Any]":
        """GET /manual_journals/{id} - look up a manual journal entry by id.

        Returns the parsed response dict (containing ``manual_journal``).
        Raises FreeeApiError on non-2xx.
        """
        url = self._url(f"/manual_journals/{journal_id}")
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_freee_op": "lookup", "journal_id": journal_id})
        if not (200 <= status < 300):
            raise FreeeApiError(status, _err_message(body))
        return body

    def create_journal(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /manual_journals - register a new manual journal entry (仕訳).

        ``payload`` is the documented ``{"manual_journal": {...}}`` request
        body. Returns the parsed response dict (created journal). Raises
        FreeeApiError on a non-2xx status.
        """
        url = self._url("/manual_journals")
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise FreeeApiError(status, _err_message(body))
        return body

    def get_balance(self, account: str, api_token: str) -> "dict[str, Any]":
        """GET /reports/trial_bs - check an account's closing balance.

        The live freee trial-balance endpoint returns all account rows; a live
        ``get`` transport adapter is expected to filter to ``account`` (the
        stub returns the matching row directly). Returns the parsed response
        dict (containing ``trial_bs.balances``). Raises FreeeApiError on
        non-2xx.
        """
        url = self._url("/reports/trial_bs")
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_freee_op": "balance", "account": account})
        if not (200 <= status < 300):
            raise FreeeApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a freee error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
