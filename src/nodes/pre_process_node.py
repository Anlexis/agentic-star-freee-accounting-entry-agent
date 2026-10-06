"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: serialize the caller's request (raw NL text + validated
journal data) into a single JSON string in `validated_input`, which the
GraphNode (`main` slot) hands to the inner freee workflow graph. Business
validation happens inside the inner graph's ValidateInputNode - this node owns
the CALLER-DATA CONTRACT: the empty-guard, the HTML/length sanitize, the
template-owned injection screen, and the field-by-field validation of
`input_context` (every accepted value is bounded; a malformed value is refused
with an error that names the field and never echoes the value).

Why input_context carries the journal data: the pipeline's other input channel
(the request text serialized into validated_input) is rewritten by the
framework's PII masking heuristics at every node boundary - a Title Case
account title ("Travel Expenses") arrives at the freee call as "[MASKED]".
Structured caller data therefore travels through input_context, which is not
masked, and this node screens that channel itself: prompt-injection content
(post-parse, keys included, both raw and after the markup strip) is refused,
and every value is held to a bounded, inert shape before it can render into
the confirmation the caller reads back.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    ACCOUNT_LABEL_RE,
    AMOUNT_MAX,
    AMOUNT_MIN,
    DATE_VALUE_RE,
    ENTRY_HINT_RE,
    finite_int_in_range,
    sanitize_query,
    screen_text,
)

# input_context keys that may carry an explicit target journal entry (caller-
# supplied only - a target is never inferred here). First present key wins.
_HINT_KEYS = ("entry_id", "entry_hint", "journal_id")

# The structured journal contract: input_context["journal"] may carry these
# fields, each bounded below. An unknown field inside `journal` is refused
# (silently dropping a misspelled field would book an entry missing data the
# caller supplied); the refusal names the container, never the unknown key
# itself (a field NAME is caller-controlled text too).
_JOURNAL_LABEL_KEYS = ("account", "debit", "credit")
_JOURNAL_KEYS = _JOURNAL_LABEL_KEYS + ("amount", "issue_date")


def _iter_context_strings(value: Any) -> "list[str]":
    """Every string in a parsed input_context - keys AND values, at any depth.

    The injection screen runs post-parse over this list, so a payload hidden in
    a mapping KEY, nested one level down, or JSON-escaped on the wire (parsed
    back to its real characters by then) is screened exactly like a top-level
    value.
    """
    found: "list[str]" = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_iter_context_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_context_strings(item))
    return found


class PreProcessNode(FunctionNode):
    """Validate + serialize caller input for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner freee call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # Caller context must be a mapping; anything else is refused without
        # being echoed (fail closed - never guess at a malformed contract).
        if input_context is None:
            input_context = {}
        if not isinstance(input_context, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input_context must be an object"],
            }

        # Template-owned injection screen. Both channels are screened RAW and
        # again after the markup strip, and input_context is screened
        # post-parse over every key and string value at any depth. Refusals
        # name the pattern type only - hostile content is never echoed.
        findings = screen_text(user_input)
        if findings:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: user_input contains disallowed content ({findings[0]})"],
            }
        for text in _iter_context_strings(input_context):
            findings = screen_text(text)
            if findings:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context contains disallowed content ({findings[0]})"],
                }

        # Strip HTML markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        # Target journal entry is caller-supplied and never inferred here: an
        # explicit hint from input_context, validated against the bounded inert
        # shape. A present-but-malformed value (wrong type, wrong charset,
        # over-length) is a hard error naming the FIELD, never the value -
        # silently dropping it could read or amend a different journal entry
        # than the caller intended.
        entry_hint = ""
        for key in _HINT_KEYS:
            value = input_context.get(key)
            if value is None:
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context.{key} must be a string"],
                }
            value = value.strip()
            if not value:
                continue  # blank = absent
            if not ENTRY_HINT_RE.match(value):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"PreProcessNode: input_context.{key} is not a valid journal-entry "
                        "number or account fragment"
                    ],
                }
            entry_hint = value
            break

        # Structured journal fields: validated field-by-field against explicit
        # bounds; labels must match the inert render charset, the amount goes
        # through the finite+bounded number parser, and the issue date must be
        # an explicit ISO date. Absent data degrades to the text-inference
        # baseline in the inner graph.
        caller_journal, problem = self._validate_journal(input_context.get("journal"))
        if problem:
            return {"status": AgentStatus.ERROR.value, "error_log": [problem]}

        validated_input = json.dumps({"text": sanitized_input, "entry_hint": entry_hint})

        # Audit the shaped request - presence signals only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_entry_hint": bool(entry_hint), "has_caller_journal": bool(caller_journal)},
            state,
        )

        return {
            "validated_input": validated_input,
            "entry_hint": entry_hint,
            "caller_journal": to_json(caller_journal) if caller_journal else None,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller journal validation --------------------------------------------

    def _validate_journal(self, journal: Any) -> "tuple[dict[str, Any], str]":
        """Validate input_context.journal -> (accepted fields, "" | problem).

        Fail closed: wrong container type, an unsupported field, a wrong-typed
        or over-bound value, a label outside the inert render charset, a
        malformed issue date, or a non-finite / out-of-range amount each refuse
        with a field-naming error. Values are never echoed; the
        unsupported-field refusal does not echo the key either (a field NAME is
        caller-controlled text too).
        """
        if journal is None:
            return {}, ""
        if not isinstance(journal, dict):
            return {}, "PreProcessNode: input_context.journal must be an object"
        unknown = [key for key in journal if key not in _JOURNAL_KEYS]
        if unknown:
            return {}, "PreProcessNode: input_context.journal contains an unsupported field"

        accepted: "dict[str, Any]" = {}
        for key in _JOURNAL_LABEL_KEYS:
            value = journal.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return {}, f"PreProcessNode: input_context.journal.{key} must be a string"
            value = value.strip()
            if not value:
                continue  # blank = absent
            if not ACCOUNT_LABEL_RE.match(value):
                return {}, (
                    f"PreProcessNode: input_context.journal.{key} must be an account label of at "
                    "most 100 letters, digits, spaces, hyphens or parentheses"
                )
            accepted[key] = value

        amount = journal.get("amount")
        if amount is not None:
            bounded = finite_int_in_range(amount, AMOUNT_MIN, AMOUNT_MAX)
            if bounded is None:
                return {}, (
                    f"PreProcessNode: input_context.journal.amount must be a finite whole number "
                    f"between {AMOUNT_MIN} and {AMOUNT_MAX}"
                )
            accepted["amount"] = bounded

        issue_date = journal.get("issue_date")
        if issue_date is not None:
            if not isinstance(issue_date, str):
                return {}, "PreProcessNode: input_context.journal.issue_date must be a string"
            issue_date = issue_date.strip()
            if issue_date:
                if not DATE_VALUE_RE.match(issue_date):
                    return {}, (
                        "PreProcessNode: input_context.journal.issue_date must be an explicit " "ISO date (YYYY-MM-DD)"
                    )
                accepted["issue_date"] = issue_date

        return accepted, ""
