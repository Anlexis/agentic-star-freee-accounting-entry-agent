# CMN-C2-277 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state), which routes the full framework security
# pipeline (trust gate -> input gate -> execute() -> credential scan); this
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "4021",
        "record_ref": "freee://manual_journals/4021",
        "account_title": "supplies",
        "balance": "",
        "intent": "lookup_entry",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved journal entry" in result["confirmation"]
        assert "'supplies'" in result["confirmation"]
        assert "ref=freee://manual_journals/4021" in result["confirmation"]
        assert "id=4021" in result["confirmation"]
        assert result["result"]["record_id"] == "4021"
        assert result["result"]["record_ref"] == "freee://manual_journals/4021"

    def test_create_verb(self):
        result = self.node(_state(intent="create_entry", account_title="travel expenses"))
        assert "Created journal entry" in result["confirmation"]

    def test_balance_verb_and_amount(self):
        result = self.node(
            _state(
                intent="check_balance",
                record_id="cash",
                record_ref="freee://accounts/cash",
                account_title="cash",
                balance="5000",
            )
        )
        assert "Retrieved account balance" in result["confirmation"]
        assert "balance=5000" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed accounting request" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=4021" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_title_missing(self):
        result = self.node(_state(account_title=""))
        assert "'4021'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
