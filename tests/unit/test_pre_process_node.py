# CMN-C2-277 - Unit tests: PreProcessNode (outer backbone, external trust gate)
#
# Canon: every node is invoked via node(state) - BaseNode.__call__ routes the
# full security pipeline (trust gate -> PII mask -> execute() -> credential
# scan) - NEVER via bare node.execute(state). PreProcessNode is the single
# VERIFIED_EXTERNAL gate, so its own tests set
# caller_trust_level = TrustLevel.VERIFIED_EXTERNAL.value (UPPERCASE .value).
# Positive payloads are PII-free (the framework mask rewrites Title-Case
# bigrams / '@' / long digit runs in user_input to "[MASKED]") - journal
# numbers in fixtures stay <= 4 digits.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    # Audit emission is exercised by its own emit-spy tests; mute the domain events
    # here so unit runs stay log-quiet. Never sys.modules-stub shared.* -
    # patch the name imported into the node module instead.
    monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "user_input": "Look up journal entry number 4021.",
        "input_context": {},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pre-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPreProcessNode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_serializes_request_with_entry_hint(self):
        state = _state(
            user_input="Summarize the entry on file",
            input_context={"entry_hint": "4021"},
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_hint"] == "4021"
        payload = json.loads(result["validated_input"])
        assert payload["text"] == "Summarize the entry on file"
        assert payload["entry_hint"] == "4021"

    def test_entry_id_takes_priority(self):
        state = _state(input_context={"entry_id": "4021", "entry_hint": "x9", "journal_id": "y7"})
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_hint"] == "4021"

    def test_journal_id_fallback(self):
        state = _state(input_context={"journal_id": "3355"})
        result = self.node(state)
        assert result["entry_hint"] == "3355"

    def test_strips_html_markup(self):
        state = _state(user_input="Look up <script>alert(1)</script>journal entry number 4021")
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = json.loads(result["validated_input"])
        assert "<script>" not in payload["text"]
        assert "</script>" not in payload["text"]

    def test_empty_input_errors(self):
        result = self.node(_state(user_input="   "))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_missing_input_errors(self):
        state = _state()
        del state["user_input"]
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
