# CMN-C2-277 - Unit tests: InferFreeeFieldsNode (inner Step 3)
#
# Canon: invoked via node(state), which routes the full framework security
# pipeline (trust gate -> input gate -> execute() -> credential scan); this
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework PII mask rewrites Title-Case
# bigrams in validated_input (even across newlines), so Key: value lines keep
# their VALUES lower-case; journal numbers and amounts stay <= 4 digits.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_freee_fields_node import InferFreeeFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_freee_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_entry", entry_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "entry_hint": entry_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferFreeeFieldsNode:
    def setup_method(self):
        self.node = InferFreeeFieldsNode()

    def test_lookup_extracts_entry_number_from_text(self):
        result = self.node(_state("Look up journal entry number 4021 and summarize the entry on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_id"] == "4021"
        # freee_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["freee_payload"], str)
        assert from_json(result["freee_payload"], {}) == {"journal_id": "4021"}

    def test_id_shaped_hint_used_when_text_has_no_number(self):
        result = self.node(_state("Summarize the entry on file", entry_hint="4021"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_id"] == "4021"
        assert from_json(result["freee_payload"], {}) == {"journal_id": "4021"}

    def test_create_builds_manual_journal_payload(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines ("Cash\nAmount" would be
        # masked before execute() sees the text).
        text = "Register a new journal entry\nDebit: travel expenses\nCredit: cash\nAmount: 5000\nDate: 2026-07-01"
        result = self.node(_state(text, intent="create_entry"))
        assert result["status"] == AgentStatus.SUCCESS.value
        # No quoted account: the record label falls back to the explicit debit account.
        assert result["account_title"] == "travel expenses"
        payload = from_json(result["freee_payload"], {})
        journal = payload["manual_journal"]
        assert journal["issue_date"] == "2026-07-01"
        assert {"entry_side": "debit", "account_title": "travel expenses", "amount": 5000} in journal["details"]
        assert {"entry_side": "credit", "account_title": "cash", "amount": 5000} in journal["details"]

    def test_create_without_explicit_lines_never_invents_details(self):
        # Risk mitigation: debit + credit + positive amount must ALL be explicit;
        # a partial request assembles NO journal lines (the executor rejects it).
        text = "Register a new journal entry\nDebit: travel expenses"
        result = self.node(_state(text, intent="create_entry"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["freee_payload"], {})
        assert payload["manual_journal"]["details"] == []

    def test_check_balance_uses_quoted_account(self):
        result = self.node(_state('Check the balance of "cash"', intent="check_balance"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["account_title"] == "cash"
        assert from_json(result["freee_payload"], {}) == {"account": "cash"}

    def test_check_balance_falls_back_to_non_id_hint(self):
        result = self.node(_state("check the current balance please", intent="check_balance", entry_hint="petty cash"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["freee_payload"], {}) == {"account": "petty cash"}

    def test_unresolved_number_left_empty_never_invented(self):
        result = self.node(_state("Summarize the entry on file"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_id"] == ""
        assert from_json(result["freee_payload"], {}) == {"journal_id": ""}

    def test_non_id_shaped_hint_left_unresolved(self):
        result = self.node(_state("Summarize the entry on file", entry_hint="not a journal number!"))
        assert result["entry_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class _FakeLLM:
    """Test-double for AzureOpenAIClient — same `complete(messages) -> dict` contract."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.calls.append(messages)
        return {"content": self._content}


class TestInferFreeeFieldsNodeLLM:
    """LLM-backed field extraction (test-double injection — no real Azure call).

    Regex stays authoritative on any conflict; the LLM only fills gaps the
    regex found nothing for (see the module docstring "LLM extraction" — a
    financial-write-safety judgment call, not the generic override pattern).
    """

    def test_llm_fills_gap_the_regex_found_nothing_for(self):
        """Free-form phrasing with no 'Key: value' lines -> regex extracts
        nothing; the LLM-extracted fields assemble the journal."""
        llm = _FakeLLM('{"debit": "travel expenses", "credit": "cash", "amount": "5000", "date": "2026-07-01"}')
        node = InferFreeeFieldsNode(llm=llm)
        text = "please book a new journal entry for the trip"
        result = node(_state(text, intent="create_entry"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["freee_payload"], {})
        journal = payload["manual_journal"]
        assert journal["issue_date"] == "2026-07-01"
        assert {"entry_side": "debit", "account_title": "travel expenses", "amount": 5000} in journal["details"]
        assert {"entry_side": "credit", "account_title": "cash", "amount": 5000} in journal["details"]
        assert len(llm.calls) == 1

    def test_regex_wins_over_llm_on_conflict(self):
        """Both an explicit 'Debit:' line AND an LLM value are present -> the
        regex-extracted value wins (financial-write safety: an explicit
        literal match is never overridden by a fuzzy LLM extraction)."""
        llm = _FakeLLM('{"debit": "office supplies", "credit": "bank", "amount": "9999"}')
        text = "Register a new journal entry\nDebit: travel expenses\nCredit: cash\nAmount: 5000"
        result = InferFreeeFieldsNode(llm=llm)(_state(text, intent="create_entry"))
        payload = from_json(result["freee_payload"], {})
        journal = payload["manual_journal"]
        assert {"entry_side": "debit", "account_title": "travel expenses", "amount": 5000} in journal["details"]
        assert {"entry_side": "credit", "account_title": "cash", "amount": 5000} in journal["details"]

    def test_llm_out_of_range_amount_never_booked(self):
        """An LLM-supplied amount still goes through the identical finite/bounded
        validation as a regex-extracted one — an absurd magnitude is refused,
        not silently trusted because it came from the LLM."""
        llm = _FakeLLM('{"debit": "travel expenses", "credit": "cash", "amount": "99999999999999999999"}')
        text = "please book a new journal entry for the trip"
        result = InferFreeeFieldsNode(llm=llm)(_state(text, intent="create_entry"))
        payload = from_json(result["freee_payload"], {})
        # amount unresolved (out of range) -> no journal lines assembled, ever.
        assert payload["manual_journal"]["details"] == []

    def test_llm_never_invents_entry_id_over_explicit_text_match(self):
        """An explicit journal-entry number in the text always wins over an
        LLM-extracted 'entry' value, even if they disagree (risk mitigation:
        never touch the wrong journal entry)."""
        llm = _FakeLLM('{"entry": "9999"}')
        result = InferFreeeFieldsNode(llm=llm)(_state("Look up journal entry number 4021.", intent="lookup_entry"))
        assert result["entry_id"] == "4021"

    def test_malformed_llm_response_falls_back_to_regex_only(self):
        llm = _FakeLLM("not valid json at all")
        text = "Register a new journal entry\nDebit: travel expenses\nCredit: cash\nAmount: 5000"
        result = InferFreeeFieldsNode(llm=llm)(_state(text, intent="create_entry"))
        payload = from_json(result["freee_payload"], {})
        assert len(payload["manual_journal"]["details"]) == 2

    def test_llm_exception_falls_back_to_regex_only(self):
        class _RaisingLLM:
            def complete(self, messages):
                raise RuntimeError("simulated Azure OpenAI API error")

        result = InferFreeeFieldsNode(llm=_RaisingLLM())(
            _state("Look up journal entry number 4021 and summarize the entry on file.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_id"] == "4021"

    def test_no_secret_configured_falls_back_to_regex_only(self):
        """No llm= injected and no secret provisioned -> regex-only, no crash."""
        result = InferFreeeFieldsNode()(
            _state("Look up journal entry number 4021 and summarize the entry on file.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entry_id"] == "4021"

    def test_empty_input_skips_llm_call(self):
        """No validated_input and no caller_journal -> execute() short-circuits
        (status=error) before the LLM is ever called."""
        llm = _FakeLLM('{"debit": "travel expenses"}')
        InferFreeeFieldsNode(llm=llm)(_state(""))
        assert llm.calls == []
