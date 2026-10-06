# CMN-C2-277 - Unit tests: CallFreeeApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state), which routes the full framework security
# pipeline (trust gate -> input gate -> execute() -> credential scan); this
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate is unaffected).
#
# The node builds its client locally (nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's FreeeClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import time

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_freee_api_node import CallFreeeApiNode
from src.services.freee_client import FreeeApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_freee_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "freee_payload": to_json({"journal_id": "4021"}),
        "intent": "lookup_entry",
        "entry_id": "4021",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-freee-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for FreeeClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_journal(self, journal_id, api_token):
        raise FreeeApiError(403, "forbidden by integration permissions")


class _FakeNotFoundClient:
    """Stands in for FreeeClient: lookup returns an empty manual_journal."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_journal(self, journal_id, api_token):
        return {"manual_journal": {}}


class _FakeSlowClient:
    """Stands in for FreeeClient: the lookup returns after the deadline."""

    delay_s = 0.05

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_journal(self, journal_id, api_token):
        time.sleep(self.delay_s)
        return {"manual_journal": {"id": journal_id, "details": []}}


class _FakeLiveClient:
    """Stands in for FreeeClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def find_journal(self, journal_id, api_token):
        _FakeLiveClient.captured = {"journal_id": journal_id, "api_token": api_token}
        return {"manual_journal": {"id": journal_id, "details": [{"account_title": "cash"}]}}


class TestCallFreeeApiNode:
    def setup_method(self):
        self.node = CallFreeeApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "4021"
        assert result["record_ref"] == "freee://manual_journals/4021"
        assert result["entry_id"] == "4021"

    def test_create_success_via_default_v1_stub(self):
        journal = {
            "manual_journal": {
                "issue_date": "2026-07-01",
                "details": [
                    {"entry_side": "debit", "account_title": "travel expenses", "amount": 5000},
                    {"entry_side": "credit", "account_title": "cash", "amount": 5000},
                ],
            }
        }
        state = _state(intent="create_entry", entry_id="", freee_payload=to_json(journal))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The stub echoes a synthetic id: assert presence + shape, never a raw value.
        assert result["record_id"]
        assert result["record_ref"] == f"freee://manual_journals/{result['record_id']}"
        assert result["entry_id"] == result["record_id"]

    def test_check_balance_success_via_default_v1_stub(self):
        state = _state(intent="check_balance", entry_id="", freee_payload=to_json({"account": "cash"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "cash"
        assert result["record_ref"] == "freee://accounts/cash"
        assert result["balance"]
        assert result["account_title"] == "cash"

    def test_freee_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `freee:` section as the JSON
        # freee_config state field; the stub transport still serves the call.
        state = _state(freee_config=to_json({"base_url": "https://freee.example.test/api/1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "freee://manual_journals/4021"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"freee": {"base_url": "https://freee.example.test/api/1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "4021"

    def test_missing_payload_errors(self):
        result = self.node(_state(freee_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_number_errors(self):
        state = _state(entry_id="", freee_payload=to_json({"journal_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved journal-entry number" in entry for entry in result["error_log"])

    def test_lookup_not_found_errors(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _FakeNotFoundClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no journal entry" in entry for entry in result["error_log"])
        # The refusal names the condition, not the caller-supplied number.
        assert not any("4021" in entry for entry in result["error_log"])

    def test_create_with_incomplete_journal_errors(self):
        # Risk mitigation: a journal without explicit debit/credit/amount is
        # never booked - the empty details assembled upstream are refused here.
        state = _state(
            intent="create_entry",
            entry_id="",
            freee_payload=to_json({"manual_journal": {"details": []}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("incomplete journal entry" in entry for entry in result["error_log"])

    def test_check_balance_with_unresolved_account_errors(self):
        state = _state(intent="check_balance", entry_id="", freee_payload=to_json({"account": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved account title" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_entry"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unsupported intent" in entry for entry in result["error_log"])
        # The refusal never echoes the caller-supplied intent string.
        assert not any("delete_entry" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])
        # Status code only - the upstream error body is never echoed onward.
        assert not any("forbidden by integration permissions" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing FREEE_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"FREEE_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["journal_id"] == "4021"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_freee_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_freee_api_complete"]
        assert payload["intent"] == "lookup_entry"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True


class TestFreeeCallDeadline:
    """The declared `timeout_s` is a real bound, not a dead declaration.

    A result that arrives after the configured deadline is discarded and the
    request fails closed - the caller is never handed data the deployment
    declared too late to trust.
    """

    def setup_method(self):
        self.node = CallFreeeApiNode()

    def test_a_late_result_is_discarded(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _FakeSlowClient)
        state = _state(freee_config=to_json({"timeout_s": 1}))
        # A 1s deadline against a 0.05s call passes; the same call against the
        # smallest expressible deadline is the interesting direction, so drive
        # the delay past it instead of shrinking the bound below its floor.
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value

        monkeypatch.setattr(_FakeSlowClient, "delay_s", 1.2)
        late = self.node(_state(freee_config=to_json({"timeout_s": 1})))
        assert late["status"] == AgentStatus.ERROR.value
        assert any("deadline" in entry for entry in late["error_log"])
        # Nothing from the discarded response is carried forward.
        assert "record_id" not in late

    @pytest.mark.parametrize(
        "declared",
        ["NaN", float("inf"), True, 0, 10**6, "abc", None],
        ids=["nan", "inf", "bool", "below-floor", "above-ceiling", "non-numeric", "absent"],
    )
    def test_an_unusable_deadline_falls_back_to_the_documented_default(self, declared):
        """An unusable declaration must not mean "no deadline at all"."""
        result = self.node(_state(freee_config=to_json({"timeout_s": declared})))
        assert result["status"] == AgentStatus.SUCCESS.value


class TestErrorReasonsAreClosedSet:
    """Every error_log entry this node writes is a closed-set label.

    This node already had the property when molt reviewed the wave-8 batch
    (2026-09-04); sibling templates in the same generation did not, so it is
    pinned here as a regression guard rather than introduced as a fix.

    Why it matters: `error_log` is the internal channel - post_process projects
    none of it to the caller (the error envelope is a constant reason code
    only) - but it feeds the audit trail and the framework's own credential
    scan runs over every node result. A reason that interpolated the
    journal-entry number would put the record evidence into the audit stream,
    and a reason that echoed the freee error body would put a live tenant's
    unbounded third-party text there - one projection away from the caller.

    The signal must still travel - a fixed phrase, the HTTP STATUS. It is the
    interpolated value that must be absent, not the diagnosis.
    """

    ENTRY_ID = "70211"

    def setup_method(self):
        self.node = CallFreeeApiNode()

    def _state_for(self, **overrides) -> dict:
        return _state(
            entry_id=self.ENTRY_ID,
            freee_payload=to_json({"journal_id": self.ENTRY_ID}),
            **overrides,
        )

    def test_no_matching_entry_reason_omits_the_entry_number(self, monkeypatch):
        class _EmptyClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_journal(self, journal_id, api_token):
                return {"manual_journal": {}}

        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _EmptyClient)
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "no journal entry" in reasons, "the diagnosis must survive"
        assert self.ENTRY_ID not in reasons, f"reason interpolated the entry number: {reasons}"

    def test_api_error_reason_carries_the_status_not_the_upstream_body(self, monkeypatch):
        class _LeakyErrorClient:
            """A tenant whose 403 body quotes the record it refused."""

            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_journal(self, journal_id, api_token):
                raise FreeeApiError(403, f"denied for 'Acme Trading K.K.' journal {journal_id} balance 1234567")

        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _LeakyErrorClient)
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "403" in reasons, "the closed-set signal must still travel"
        assert "Acme Trading" not in reasons
        assert "1234567" not in reasons
        assert self.ENTRY_ID not in reasons

    def test_transport_failure_reason_carries_no_exception_text(self, monkeypatch):
        class _BoomClient:
            """A transport error string carrying the URL, which carries the id."""

            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_journal(self, journal_id, api_token):
                raise RuntimeError(f"GET https://api.freee.co.jp/api/1/manual_journals/{journal_id} refused")

        monkeypatch.setattr("src.nodes.call_freee_api_node.FreeeClient", _BoomClient)
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "transport layer" in reasons, "the diagnosis must survive"
        assert self.ENTRY_ID not in reasons
        assert "https://" not in reasons

    def test_control_the_clean_call_still_returns_record_evidence(self):
        """CONTROL. Without it the assertions above pass vacuously - a node
        that errored on everything, or wrote no reasons at all, would satisfy
        them."""
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"]
        assert result["record_ref"].startswith("freee://manual_journals/")
