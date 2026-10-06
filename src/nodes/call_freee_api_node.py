"""AgentCore Platform v1.0 - inner workflow Step 4: CallFreeeApi (tool side-effect).

Performs the lookup/create/balance call against the freee accounting REST API
endpoints via src/services/freee_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration token is read via
       ctx.secrets.get("FREEE_TOKEN") (InvocationContext.from_state(state)) -
       never os.environ, never stored in state. While the deterministic
       network-free stub transport is active a missing token is tolerated (a
       sentinel placeholder is used - it is never sent anywhere because no
       request leaves the process); with a LIVE transport injected, a missing
       token is a hard status=error - a real API is never called
       unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external accounting system; HTTP 4xx/5xx surfaces as
       status=error + error_log (no silent pass).
  Error surface: an error names the FIELD or the condition, never the caller
       value that produced it, and never the transport exception text - both
       are caller-controlled or upstream-controlled strings that would be
       echoed back into the caller-facing error envelope.

Configuration: this node takes NO constructor arguments (nodes are no-arg). freee settings (base_url, company_id, timeout_s) arrive as the JSON
`freee_config` state field - injected by the inner graph's
_extra_initial_state() from the runtime config forwarded by
FreeeWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["freee"]` argument for direct invocation. The client
is constructed locally per call (no module-global mutation).
"""

import time
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.freee_client import FreeeApiError, FreeeClient
from src.services.security import TIMEOUT_MAX, TIMEOUT_MIN, finite_int_in_range

_SECRET_KEY = "FREEE_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Deadline applied to the freee call when config/config.yaml declares
# `timeout_s`. A result that arrives after the deadline is discarded and the
# request fails closed rather than surfacing data the deployment declared too
# late to trust. A live transport should also set its own socket timeout so a
# hung connection is cut rather than only observed.
_DEFAULT_TIMEOUT_S = 30


class CallFreeeApiNode(FunctionNode):
    """Look up / create a journal entry or check a balance via the freee API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("freee_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallFreeeApiNode: missing freee_payload"],
            }

        intent = state.get("intent", "lookup_entry") or "lookup_entry"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["freee"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("freee_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("freee") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        company_id = str(settings.get("company_id", "") or "").strip()
        # The declared deadline goes through the finite+bounded parser; an
        # absent or unusable declaration falls back to the documented default
        # rather than running unbounded.
        timeout_s = finite_int_in_range(settings.get("timeout_s"), TIMEOUT_MIN, TIMEOUT_MAX)
        if timeout_s is None:
            timeout_s = _DEFAULT_TIMEOUT_S
        client = (
            FreeeClient(base_url=base_url, company_id=company_id) if base_url else FreeeClient(company_id=company_id)
        )

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallFreeeApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        entry_id = state.get("entry_id", "") or str(payload.get("journal_id", "") or "")
        account_title = state.get("account_title", "") or ""
        balance = ""

        started = time.monotonic()
        try:
            if intent == "lookup_entry":
                if not entry_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeApiNode: unresolved journal-entry number - cannot look up entry"],
                    }
                resp = client.find_journal(entry_id, api_token) or {}
                journal = resp.get("manual_journal") or {}
                if not journal:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeApiNode: freee returned no journal entry for the " "requested number"],
                    }
                record_id = str(journal.get("id", "")) or entry_id
                if not account_title:
                    details = journal.get("details") or []
                    first = details[0] if isinstance(details, list) and details else {}
                    account_title = str(first.get("account_title", "") or "")
                record_ref = f"freee://manual_journals/{record_id}"
            elif intent == "create_entry":
                journal = payload.get("manual_journal") or {}
                if not journal.get("details"):
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallFreeeApiNode: incomplete journal entry - an explicit debit "
                            "account, credit account, and positive amount are required "
                            "(never invented)"
                        ],
                    }
                resp = client.create_journal(payload, api_token) or {}
                created = resp.get("manual_journal") or {}
                record_id = str(created.get("id", ""))
                if not record_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeApiNode: freee returned no journal id on create"],
                    }
                record_ref = f"freee://manual_journals/{record_id}"
                entry_id = entry_id or record_id
            elif intent == "check_balance":
                account = str(payload.get("account", "") or "") or account_title
                if not account:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeApiNode: unresolved account title - cannot check balance"],
                    }
                resp = client.get_balance(account, api_token) or {}
                balances = (resp.get("trial_bs") or {}).get("balances") or []
                if not balances:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallFreeeApiNode: freee returned no balance for the requested account"],
                    }
                row = balances[0]
                record_id = str(row.get("account_item_name", ""))[:100] or account
                record_ref = f"freee://accounts/{record_id}"
                balance = str(row.get("closing_balance", ""))[:64]
                account_title = account_title or record_id
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallFreeeApiNode: unsupported intent for a freee call"],
                }
        except FreeeApiError as exc:
            # Status code only: a freee error body is upstream-controlled text
            # that would otherwise be echoed into the caller-facing envelope.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallFreeeApiNode: freee returned HTTP {exc.status_code}"],
            }
        except Exception:  # transport failure - no silent pass, no exception text
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallFreeeApiNode: the freee call failed at the transport layer"],
            }

        elapsed = time.monotonic() - started
        if elapsed > timeout_s:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"CallFreeeApiNode: the freee call exceeded the configured "
                    f"{timeout_s}s deadline - result discarded"
                ],
            }

        # Audit the tool side-effect - intent + presence signals only, never
        # journal content or credentials.
        emit_trace_event(
            "call_freee_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "entry_id": entry_id or record_id,
            "account_title": account_title,
            "balance": balance,
            "status": AgentStatus.SUCCESS.value,
        }
