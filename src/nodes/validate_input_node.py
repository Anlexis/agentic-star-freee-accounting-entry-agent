"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input and runs a deterministic (regex, NOT LLM)
scan of the inbound text for email addresses / access-token-like strings,
which are flag-and-redacted before anything is logged. An accounting request
legitimately names partners and accounts (the framework PII mask in
BaseNode.__call__ additionally masks emails/phones/names in user_input /
validated_input), so this is flag-and-redact for safe logging, not a hard
reject. The only deterministic auto-reject is the empty / non-request guard.
"""

import json
import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# Deterministic patterns: email addresses and bearer/JWT/API-token-like
# strings that might appear in a pasted request. Flagged + redacted before logging.
# Every quantifier is bounded ({N,} -> {N,M}) rather than open-ended; 63 is the
# DNS label length limit, 4096 comfortably covers any real JWT/API-key/
# bearer-token length.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,63}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,4096}|secret_[A-Za-z0-9]{6,4096}|sk-[A-Za-z0-9]{6,4096})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound accounting request."""

    # Inner domain node - the external trust gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        entry_hint = state.get("entry_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                entry_hint = obj.get("entry_hint", entry_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_entry_hint": bool(entry_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "entry_hint": entry_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
