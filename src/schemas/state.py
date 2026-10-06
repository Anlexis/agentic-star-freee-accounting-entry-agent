"""AgentCore Platform v1.0 - CMN-C2-277 freee Accounting Entry Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. Checkpoint
# serialization is msgpack-based; Pydantic objects (and nested dict/list
# containers) are not msgpack-safe. Extend AgentState with agent-specific
# fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). freee_payload /
# freee_config / redaction_flags are dicts/lists at the point of use but are
# stored in State as JSON strings via to_json/from_json below. Do NOT add
# credentials, secrets, or Pydantic models (PB-2 / PB-5). The freee
# integration token is NEVER stored here - it is read via ctx.secrets in
# CallFreeeApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """freee Accounting Entry agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only freee-workflow fields are added below, all NotRequired (the state
    contract). All values are JSON/msgpack-serializable primitives -
    the freee integration token is NEVER stored here (accessed via
    ctx.secrets).
    """

    # Caller-supplied target hint (journal-entry number or account fragment
    # from input_context / the request envelope). Never inferred; resolution
    # to a freee manual-journal id is explicit-only (v1: pass-through when the
    # hint or request text already carries a number).
    entry_hint: NotRequired[str]
    entry_id: NotRequired[str]  # resolved freee manual-journal (journal entry) id

    # Structured journal fields supplied by the caller through input_context
    # and validated field-by-field by PreProcessNode (account/debit/credit
    # labels, amount, issue_date). Stored as a JSON string; crosses the
    # outer->inner graph boundary through src/graph/context_bridge.py.
    caller_journal: NotRequired[Optional[str]]

    # ValidateInput (deterministic scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferFreeeFields
    account_title: NotRequired[str]  # account title / record label
    # JSON - assembled freee accounting REST API request body (stored as a
    # JSON string, not a native dict; (de)serialize via to_json/from_json).
    freee_payload: NotRequired[Optional[str]]

    # Runtime `freee:` section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    freee_config: NotRequired[Optional[str]]

    # CallFreeeApi
    record_id: NotRequired[str]  # manual-journal id / account label returned by freee
    record_ref: NotRequired[str]  # human-readable reference (freee://manual_journals/<id>)
    balance: NotRequired[str]  # closing balance for a check_balance request

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
