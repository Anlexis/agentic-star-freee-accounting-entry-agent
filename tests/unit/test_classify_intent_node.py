# CMN-C2-277 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_entry / create_entry / check_balance. A deterministic
# keyword heuristic is the always-available baseline; an optional LLM pass
# (Azure OpenAI, test-double injected here - no real network call) overrides
# it when it returns a valid result. Unknown/failed heuristic falls back to
# the read-only lookup.
#
# Canon: invoked via node(state), which routes the full framework security
# pipeline (trust gate -> input gate -> execute() -> credential scan); this
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_entry(self):
        result = self.node(_state("Look up journal entry number 4021."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_entry"

    def test_keyword_create_entry(self):
        result = self.node(_state("Register a new journal entry for the office supplies purchase"))
        assert result["intent"] == "create_entry"

    def test_keyword_check_balance(self):
        result = self.node(_state("What is the balance of the cash account"))
        assert result["intent"] == "check_balance"

    def test_write_keyword_wins_over_lookup(self):
        # Priority order is the write first: a "book the entry then show it"
        # style request classifies as the write, never the read.
        result = self.node(_state("Book the journal entry and show the entry on file"))
        assert result["intent"] == "create_entry"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_entry"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_entry" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up journal entry number 4021."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_entry"
        assert payloads["classify_intent_complete"]["defaulted"] is False


class _FakeLLM:
    """Test-double for AzureOpenAIClient — same `complete(messages) -> dict` contract."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.calls.append(messages)
        return {"content": self._content}


class TestClassifyIntentNodeLLM:
    """LLM-backed classification path (test-double injection — no real Azure call)."""

    def test_llm_response_overrides_heuristic(self):
        """Heuristic would default to lookup_entry (no keyword signal); a valid
        LLM classification overrides it."""
        llm = _FakeLLM('{"intent": "create_entry"}')
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_entry"
        assert len(llm.calls) == 1
        assert llm.calls[0][0]["role"] == "system"

    def test_llm_prose_wrapped_json_is_extracted(self):
        llm = _FakeLLM('Sure, here you go:\n```json\n{"intent": "check_balance"}\n```')
        result = ClassifyIntentNode(llm=llm)(_state("please handle this for the team"))
        assert result["intent"] == "check_balance"

    def test_malformed_llm_response_falls_back_to_heuristic(self):
        """Missing 'intent' key -> heuristic result, no crash."""
        llm = _FakeLLM('{"confidence": "high"}')
        result = ClassifyIntentNode(llm=llm)(_state("Look up journal entry number 4021."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_entry"

    def test_llm_intent_outside_closed_set_falls_back_to_heuristic(self):
        """An intent value outside the 3-way enum is rejected, not trusted verbatim."""
        llm = _FakeLLM('{"intent": "delete_entry"}')
        result = ClassifyIntentNode(llm=llm)(_state("Register a new journal entry for supplies"))
        assert result["intent"] == "create_entry"  # heuristic keyword match, not the LLM value

    def test_llm_exception_falls_back_to_heuristic(self):
        class _RaisingLLM:
            def complete(self, messages):
                raise RuntimeError("simulated Azure OpenAI API error")

        result = ClassifyIntentNode(llm=_RaisingLLM())(_state("What is the balance of the cash account"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "check_balance"

    def test_no_secret_configured_falls_back_to_heuristic(self):
        """No llm= injected and no secret provisioned -> heuristic, no crash.

        Real production shape when register_nodes() never passes llm= and
        ctx.secrets.require("AZURE_OPENAI_API_KEY") has nothing bound.
        """
        result = ClassifyIntentNode()(_state("Look up journal entry number 4021."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_entry"

    def test_empty_input_skips_llm_call(self):
        """No validated_input -> execute() short-circuits before the LLM is ever called."""
        llm = _FakeLLM('{"intent": "create_entry"}')
        ClassifyIntentNode(llm=llm)(_state(""))
        assert llm.calls == []
