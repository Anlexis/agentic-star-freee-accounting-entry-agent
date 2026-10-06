"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner freee workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline.

Three properties are load-bearing and easy to get wrong:

- The gate walks the WHOLE nested output structure (dicts, lists, tuples),
  mapping keys included, not just top-level strings. The caller-facing output
  carries the assembled freee request body as a nested mapping, so a
  credential-shaped string riding one level down (inside a journal line's
  account title, say) must be caught exactly like a top-level one.
- EVERY non-success return - a gate violation AND a pre-existing
  inner-workflow error - goes through the one module-level `_contain()`
  helper, which CLEARS every output-bearing state field instead of merely
  returning an error status. The base graph's output shaping falls back to
  `state["result"]` when `formatted_output` is falsy, so a gate that only
  raised - or that returned an error without clearing - would still ship the
  un-gated inner answer inside the error envelope. Containment means the
  answer is gone, not merely relabelled. Omitting a field from one envelope is
  not clearing it: a checkpoint or a downstream reader picks it straight back
  up out of state. The replacement `formatted_output` is a truthy mapping - an
  empty/falsy value would activate the `result` fallback it exists to prevent.
- What the ERROR envelope may say: closed-set labels only. It carries a
  constant reason code chosen by this module (one of `ERROR_REASONS`) and
  nothing else - never `error_log`, never the gate's violation entries, never
  any other node-authored text. Those lines can embed upstream response text
  (a freee error body), identifiers, names or caller-derived fragments, and
  truncating or redacting them is not a closed set. `error_log` stays the
  INTERNAL channel: the state reducer appends to it and the audit trail needs
  it; it is simply never projected to the caller. Gate violations are written
  to `error_log` naming the offending PATH (fixed keys and indices, never the
  value - and a credential-shaped mapping KEY is withheld from the label,
  because the label rides `error_log`, where the framework's own credential
  scan would raise on it and replace this node's cleared delta with a bare
  error, re-opening the `result` fallback the clearing closed), and the audit
  event carries a count.

No error envelope carries freee record evidence either. `record_id` /
`record_ref` are this agent's WRITE EVIDENCE - the SUCCESS branch of the gate
below REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being informed of failure that a journal entry was
nonetheless created or read, and which one. freee is an accounting system: the
entry id, the account title and the balance are a customer's bookkeeping.

The stated output invariant this gate enforces is the template's own, not a
rounding grid: a monetary figure this agent reports is a specific journal
amount or account balance the caller asked for, and reporting it to anything
other than its exact bookkeeping value would be wrong. What the gate enforces
instead is (a) no SUCCESS response without record evidence, and (b) no
credential-shaped string anywhere in the caller-facing output - see
docs/02_design.md, "Output contract".
"""

import re
from typing import Any, Iterator

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework output-side credential scan in FunctionNode also runs on every
# result). Every quantifier is bounded ({N,} -> {N,M}) rather than open-ended -
# 4096 comfortably covers any real JWT/API-key/bearer-token length without
# leaving an unbounded repeat that a pathological input could exploit.
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,4096}|sk-[A-Za-z0-9]{20,4096}|Bearer\s+[A-Za-z0-9._-]{16,4096}")

# Stand-in for a mapping key that cannot itself be written into a path label.
_UNNAMEABLE_KEY = "<withheld>"

# Every state field that can carry released answer text. On EVERY non-success
# return all of them are cleared, so nothing downstream can fall back to one of
# them.
#
# `record_id` / `record_ref` / `entry_id` are the freee write evidence and
# `intent` is the action label. They are cleared for the same reason the error
# envelope omits them: a caller told the operation failed must not be able to
# recover, from a checkpoint or a downstream reader, that a journal entry was
# touched and which one.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "confirmation",
    "record_id",
    "record_ref",
    "entry_id",
    "account_title",
    "balance",
    "freee_payload",
    "intent",
)

# The cleared value of every output-bearing field. Spread into every
# non-success return by _contain().
_CLEARED_OUTPUT_STATE: "dict[str, Any]" = {
    field: (None if field in ("result", "freee_payload") else "") for field in _OUTPUT_BEARING_FIELDS
}

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which entry and never in whose words.
_REASON_WORKFLOW_FAILED = "freee_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    Error status, every output-bearing field cleared (_CLEARED_OUTPUT_STATE),
    and an envelope made of closed-set labels only: `reason` is one of
    ERROR_REASONS. `new_errors` (gate violations - path labels only) are
    appended to `error_log`, the internal channel the state reducer
    accumulates, and never enter the envelope. Nothing is read out of state:
    not the record, not `error_log` - the inner entries are already there, and
    re-emitting them would duplicate every line.

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output()
    applies no status check) serves this envelope and never whatever survived
    in `result`.
    """
    contained: "dict[str, Any]" = dict(_CLEARED_OUTPUT_STATE)
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _iter_strings(value: Any, path: str) -> "Iterator[tuple[str, str]]":
    """Yield (path, string) for EVERY string in a nested structure, keys included.

    Walks dicts, lists and tuples so a value nested inside the freee request
    body (e.g. formatted_output["freee_payload"]["manual_journal"]["details"])
    is scanned exactly like a top-level field. A mapping key is yielded as a
    string in its own right: a credential rides a key as easily as a value.

    A credential-shaped KEY is reported as a violation by the caller of this
    walk, so the path label must not repeat it: the label travels in
    error_log, where the framework's own credential scan would raise on it and
    replace the cleared result around it with a bare error - restoring the
    very leak the clearing closed.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                label = _UNNAMEABLE_KEY if _CREDENTIAL_LIKE_RE.search(key) else key
                key_path = f"{path}.{label}" if path else label
                yield key_path, key
            else:
                key_path = f"{path}.{key}" if path else str(key)
            yield from _iter_strings(item, key_path)
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{idx}]")


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (record_id/record_ref),
        which would misrepresent the freee action outcome to the caller;
      - any credential-shaped string ANYWHERE in the caller-facing output,
        including strings nested inside mappings/lists (the assembled freee
        request body is a nested dict) and mapping keys. Violations name the
        field PATH, never the value, and are written to `error_log` only - the
        internal channel; they never reach the caller.
    """
    problems: "list[str]" = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for path, value in _iter_strings(formatted_output, ""):
        if _CREDENTIAL_LIKE_RE.search(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{path}']")
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive;
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it: error_log already carries the inner
        # entries (the state reducer appends, so re-emitting them here would
        # duplicate every line) and the caller receives the reason code only.
        # The delta clears every output-bearing field, so the identifiers and
        # the balance cannot be recovered from the checkpoint or by a
        # downstream reader either. Under the real pipeline
        # AgentBaseGraph.route() sends ERROR to finalize and BaseNode.__call__
        # short-circuits on an errored state before execute() runs, so this
        # branch is defence in depth for direct invocation; it is held to the
        # same standard as the gate path.
        if state.get("status") == AgentStatus.ERROR.value:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for journal content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "errors": len(state.get("error_log", []) or [])},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "account_title": state.get("account_title", ""),
            "balance": state.get("balance", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "freee_payload": from_json(state.get("freee_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring). A
        # refusal is contained the same way as an inner error: the violations
        # go to error_log only, the caller receives the reason code only.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # A refusal is a decision, so it gets its own audit event - a block
            # that leaves no trace is indistinguishable from a request that was
            # never made. Counts and the violated rule only, never the values.
            emit_trace_event(
                "post_process_gate_blocked",
                {
                    "reason": _REASON_OUTPUT_WITHHELD,
                    "intent": state.get("intent", ""),
                    "violation_count": len(violations),
                    "missing_record_evidence": not (
                        formatted_output.get("record_id") or formatted_output.get("record_ref")
                    ),
                },
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
