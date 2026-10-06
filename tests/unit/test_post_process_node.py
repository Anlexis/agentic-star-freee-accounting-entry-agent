# CMN-C2-277 - Unit tests: PostProcessNode (outer backbone domain output gate)
#
# Canon: invoked via node(state), which routes the full framework security
# pipeline (trust gate -> input gate -> execute() -> credential scan); this
# backbone formatter declares ANONYMOUS -> the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is the
# MODULE-LEVEL _security_gate_output() helper (the framework gate methods are
# @final and the real SDK auto-wraps _extra_ hooks), so the helper is also
# unit-tested directly as a plain function.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


@pytest.fixture
def audit_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        "src.nodes.post_process_node.emit_trace_event",
        lambda *args, **kwargs: events.append(args),
    )
    return events


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "4021",
        "record_ref": "freee://manual_journals/4021",
        "account_title": "supplies",
        "balance": "",
        "intent": "lookup_entry",
        "confirmation": "Retrieved journal entry 'supplies' - ref=freee://manual_journals/4021 - id=4021",
        "freee_payload": to_json({"journal_id": "4021"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the enum .value STRING, never the
        # bare AgentStatus enum member. AgentStatus subclasses str, so an
        # isinstance() check would pass for the enum member too and miss the
        # regression - the exact-type comparison is the point here.
        assert type(result["status"]) is str  # noqa: E721
        out = result["formatted_output"]
        assert out["record_id"] == "4021"
        assert out["record_ref"] == "freee://manual_journals/4021"
        assert out["intent"] == "lookup_entry"
        assert out["confirmation"].startswith("Retrieved journal entry")
        # Round-trip: the JSON freee_payload string surfaces parsed.
        assert out["freee_payload"] == {"journal_id": "4021"}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallFreeeApiNode: freee API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "freee API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_output_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])

    def test_gate_violation_clears_every_output_bearing_field(self):
        """Containment: a violation removes the answer, it does not merely relabel it.

        The base graph shapes its response from formatted_output OR result, so
        an error status alone would still ship the un-gated inner answer inside
        the error envelope.
        """
        bearer_like = "Bearer " + "b" * 24
        state = _state(confirmation=f"Retrieved journal entry - token {bearer_like}")
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        for field in ("result", "freee_payload"):
            assert result[field] is None
        for field in ("confirmation", "record_id", "record_ref", "account_title", "balance"):
            assert result[field] == ""
        retained = [f for f in ("entry_id", "intent") if f not in result or result[f]]
        assert not retained, f"output-bearing state not cleared on the gate path: {retained}"
        # The replacement envelope is the record-free withheld notice - TRUTHY
        # on purpose, so the `formatted_output or result` projection stops here
        # rather than falling back onto whatever survived in state.
        assert result["formatted_output"]
        assert result["formatted_output"].get("reason") == "output_withheld_by_gate"
        rendered = repr(result)
        assert bearer_like not in rendered
        assert "Retrieved journal entry" not in rendered

    def test_gate_violation_emits_its_own_audit_event(self, audit_events):
        """A block that leaves no trace is indistinguishable from a request that
        was never made - and the event must carry signals, not the values."""
        bearer_like = "Bearer " + "e" * 24
        self.node(_state(account_title=bearer_like))
        names = [args[0] for args in audit_events]
        assert "post_process_gate_blocked" in names
        assert "post_process_complete" not in names
        payload = next(args[1] for args in audit_events if args[0] == "post_process_gate_blocked")
        assert payload["violation_count"] == 1
        assert bearer_like not in repr(payload)

    def test_gate_walks_nested_structures(self):
        """A credential nested inside the assembled request body is caught too.

        The paired top-level case is the control: without it a clean nested
        result cannot be told apart from a gate that never ran.
        """
        bearer_like = "Bearer " + "c" * 24
        nested = self.node(
            _state(freee_payload=to_json({"manual_journal": {"details": [{"account_title": bearer_like}]}}))
        )
        assert nested["status"] == AgentStatus.ERROR.value
        assert any("freee_payload" in entry for entry in nested["error_log"])

        top_level = self.node(_state(account_title=bearer_like))
        assert top_level["status"] == AgentStatus.ERROR.value
        assert any("account_title" in entry for entry in top_level["error_log"])

        clean = self.node(_state())
        assert clean["status"] == AgentStatus.SUCCESS.value


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "4021", "record_ref": "freee://manual_journals/4021", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "4021", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []

    def test_blocks_credential_nested_in_a_list_of_mappings(self):
        """The walk descends dicts AND lists - the freee request body is both."""
        bearer_like = "Bearer " + "d" * 24
        violations = _security_gate_output(
            {
                "record_id": "4021",
                "freee_payload": {"manual_journal": {"details": [{"account_title": bearer_like}]}},
            },
            is_success=True,
        )
        assert len(violations) == 1
        assert "freee_payload.manual_journal.details[0].account_title" in violations[0]
        # The violation names the PATH, never the value.
        assert bearer_like not in violations[0]

    def test_ordinary_nested_output_passes(self):
        """Negative control: the same nested shape without a credential is clean."""
        violations = _security_gate_output(
            {
                "record_id": "4021",
                "freee_payload": {
                    "manual_journal": {"details": [{"account_title": "Travel Expenses", "amount": 5000}]}
                },
            },
            is_success=True,
        )
        assert violations == []


class TestExistingErrorPathContainment:
    """The pre-existing-ERROR branch must contain, not re-publish.

    molt source review (wave-8 batch, 2026-09-04): "the success-gate violation
    path has containment, but the independent existing-ERROR branch rebuilds a
    truthy response with record_id / record_ref and does not clear the merged
    output-bearing state."

    `record_id` / `record_ref` are the freee WRITE EVIDENCE - the success branch
    of this node's own gate REFUSES a SUCCESS that lacks them. Returning them in
    an envelope whose status is ERROR tells a caller being informed of failure
    that a journal entry was nonetheless created or read, and which one. freee
    is an accounting system: the entry id, the account title and the balance are
    a customer's bookkeeping.

    Three properties, pinned here:
      1. the shipped envelope carries no record evidence, AND stays TRUTHY - a
         falsy formatted_output re-opens the framework's
         `formatted_output or result` projection (get_output(), no status check)
         onto whatever survived in state;
      2. the returned delta CLEARS the output-bearing state fields, so a
         checkpoint or a downstream reader cannot pick them up either;
      3. nothing of `error_log` rides the envelope - not redacted, not
         truncated, not at all. The caller receives the reason code only (the
         closed-set contract itself is pinned by TestErrorEnvelopeIsClosedSet
         below, over every non-success path).

    Reachability, stated honestly: in the compiled graph AgentBaseGraph.route()
    sends an errored state to `finalize` (bypassing post_process) and
    BaseNode.__call__ short-circuits on an incoming errored state before
    execute() runs, so this branch is source-level defence in depth, reachable
    by a direct execute(). It is not a live end-to-end leak - and it is still
    the shape a future route change or a direct caller would ship.
    """

    _RECORD_ID = "70211"
    _RECORD_REF = "freee://manual_journals/70211"
    _ACCOUNT = "Accounts Receivable - Acme Trading K.K."
    _BALANCE = "1234567"

    # Every output-bearing State field: the envelope composes them, and
    # downstream formatting / the checkpoint read them.
    _OUTPUT_BEARING = (
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

    def _errored_state(self, error_log=None) -> dict:
        """An error raised AFTER the freee call resolved a record - the
        realistic shape (call succeeded, a later step failed), and the only
        shape in which record evidence is present on an error at all."""
        return _state(
            status=AgentStatus.ERROR.value,
            record_id=self._RECORD_ID,
            record_ref=self._RECORD_REF,
            entry_id=self._RECORD_ID,
            account_title=self._ACCOUNT,
            balance=self._BALANCE,
            confirmation=f"Retrieved journal entry '{self._ACCOUNT}' - ref={self._RECORD_REF}",
            freee_payload=to_json(
                {"manual_journal": {"details": [{"account_title": self._ACCOUNT, "amount": 1234567}]}}
            ),
            result={
                "record_id": self._RECORD_ID,
                "record_ref": self._RECORD_REF,
                "account_title": self._ACCOUNT,
                "balance": self._BALANCE,
            },
            error_log=error_log or ["ConfirmNode: downstream failure after the freee call"],
        )

    def test_error_envelope_is_present_and_truthy(self):
        """Containment must not be achieved by emptying the envelope: the
        framework projects `formatted_output or result` with NO status check,
        so a falsy value hands the caller `result` instead."""
        result = PostProcessNode().execute(self._errored_state())
        assert "formatted_output" in result, "error path must ship an envelope"
        assert result["formatted_output"], (
            "error envelope must be TRUTHY - a falsy one re-opens the "
            "`formatted_output or result` fallback in AgentBaseGraph.get_output()"
        )

    def test_error_envelope_carries_no_record_evidence(self):
        """The leak itself: no record identifier, account title or balance may
        ride the failure envelope back to the caller."""
        import json

        shipped = json.dumps(
            PostProcessNode().execute(self._errored_state())["formatted_output"],
            default=str,
            ensure_ascii=False,
        )
        leaked = [
            field
            for field, value in (
                ("record_id", self._RECORD_ID),
                ("record_ref", self._RECORD_REF),
                ("account_title", self._ACCOUNT),
                ("balance", self._BALANCE),
            )
            if value in shipped
        ]
        assert not leaked, f"error envelope leaked freee record evidence: {leaked}"

    def test_error_return_clears_output_bearing_state(self):
        """ "Does not clear the merged output-bearing state": omitting a field
        from ONE envelope is not clearing it. The delta must blank every
        output-bearing field so no checkpoint or downstream reader recovers
        it."""
        result = PostProcessNode().execute(self._errored_state())
        retained = [field for field in self._OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, f"output-bearing state not cleared on the error path: {retained}"

    def test_error_envelope_carries_no_error_text(self):
        """Error text can embed an upstream API response, so none of it is
        published. Redaction was the previous answer and it is not a closed
        set: a name, an identifier or an arbitrary response body all pass a
        credential-only filter untouched."""
        bearer_like = "Bearer " + "f" * 24
        result = PostProcessNode().execute(self._errored_state(error_log=[f"upstream said: {bearer_like}"]))
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        rendered = repr(result)
        assert bearer_like not in rendered
        assert "upstream said" not in rendered
        assert "[REDACTED]" not in rendered, "nothing is redacted because nothing is published"

    def test_error_status_is_still_reported(self):
        """Containment must not mask the failure."""
        assert PostProcessNode().execute(self._errored_state())["status"] == AgentStatus.ERROR.value

    def test_the_failure_is_reported_by_reason_code_only(self):
        """Containment is not silence: the caller learns WHAT happened through
        a constant reason code - and nothing else. The producing nodes' reasons
        stay in error_log, the internal channel (TestErrorReasonsAreClosedSet
        in test_call_freee_api_node.py keeps them closed-set for the audit
        trail), and they are not re-emitted here: the state reducer appends,
        so a re-emit would duplicate every line."""
        reasons = ["ConfirmNode: downstream failure after the freee call"]
        result = PostProcessNode().execute(self._errored_state(error_log=reasons))
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert "error_log" not in result
        assert "ConfirmNode" not in repr(result)

    def test_clean_path_control_still_returns_the_answer(self):
        """CONTROL. Without it every containment assertion above passes
        vacuously - a node that returned an empty envelope would satisfy them
        all. The success path must still carry the record evidence."""
        result = PostProcessNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "4021"
        assert out["record_ref"] == "freee://manual_journals/4021"
        assert out["account_title"] == "supplies"
        assert out["intent"] == "lookup_entry"
        assert out["confirmation"].startswith("Retrieved journal entry")
        assert out["freee_payload"] == {"journal_id": "4021"}


# -- the closed-set error envelope -------------------------------------------
#
# What the caller may learn from a non-success return: a constant reason code
# drawn from ERROR_REASONS, and nothing else. Not error_log (node-authored text
# that can embed an upstream response body, identifiers, names), not the gate's
# own violation entries, not record evidence. error_log is the INTERNAL channel
# - the state reducer appends to it, the audit trail needs it - and it must
# never be projected. Every non-success path below is held to the same shape.


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed - and it
    is chosen to match NO credential detector (the template's, the framework's)
    on purpose: the point is text that no redactor would catch."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")

# Credential-shaped strings, built at runtime (no credential-shaped literal is
# committed). The KEY case matters on its own: a violation label that quoted
# the key would put the credential shape into error_log, where the framework's
# own scan raises and replaces the cleared delta with a bare error.
_BEARER_KEY = "Bearer " + "k" * 24
_BEARER_VALUE = "Bearer " + "v" * 24

_OUTPUT_BEARING = (
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


def _every_string(value) -> list:
    """Every string reachable in a structure - keys AND values, at any depth."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_every_string(k), *_every_string(v))]
    if isinstance(value, (list, tuple, set)):
        return [s for item in value for s in _every_string(item)]
    return [value if isinstance(value, str) else str(value)]


def _leaked(value) -> list:
    strings = _every_string(value)
    return [fragment for fragment in _SENTINEL_FRAGMENTS if any(fragment in s for s in strings)]


def _errored_after_the_call(**overrides) -> dict:
    """The inner workflow failed AFTER the freee call resolved a record: the
    answer is merged into `result`, every domain field is populated, and
    error_log carries the upstream failure text."""
    state = _state(
        status=AgentStatus.ERROR.value,
        record_id="70211",
        record_ref="freee://manual_journals/70211",
        entry_id="70211",
        account_title="Accounts Receivable - Acme Trading K.K.",
        balance="1234567",
        confirmation="Retrieved journal entry 'Accounts Receivable - Acme Trading K.K.' - id=70211",
        freee_payload=to_json({"manual_journal": {"details": [{"account_title": "Accounts Receivable"}]}}),
        result={"record_id": "70211", "record_ref": "freee://manual_journals/70211"},
        error_log=[_sentinel()],
    )
    state.update(overrides)
    return state


def _refused(**overrides) -> dict:
    """A SUCCESS state the output gate must refuse, with the sentinel already
    in error_log (a non-fatal note left by an earlier node, say)."""
    state = _state(error_log=[_sentinel()])
    state.update(overrides)
    return state


_NON_SUCCESS_STATES = [
    pytest.param(_errored_after_the_call(), id="inner-workflow-error"),
    pytest.param(
        _errored_after_the_call(error_log=[_sentinel(), "upstream said: " + _BEARER_VALUE]),
        id="inner-error-with-credential-in-error_log",
    ),
    pytest.param(_refused(record_id="", record_ref=""), id="success-without-record-evidence"),
    pytest.param(
        _refused(freee_payload=to_json({"manual_journal": {"details": [{"account_title": _BEARER_VALUE}]}})),
        id="credential-nested-in-payload",
    ),
    pytest.param(
        _refused(freee_payload=to_json({"manual_journal": {_BEARER_KEY: "Travel Expenses"}})),
        id="credential-shaped-key-clean-value",
    ),
    pytest.param(
        _refused(freee_payload=to_json({"manual_journal": {_BEARER_KEY: _BEARER_VALUE}})),
        id="credential-shaped-key-and-value",
    ),
]


def _run(state: dict) -> dict:
    """Drive post_process over `state` the way the pipeline would.

    A refusal is reached through node(state) - the real call path, framework
    credential scan on the result included. An errored state is driven through
    execute(): BaseNode.__call__ short-circuits on it and the backbone routes
    an error straight to finalize, so that branch is only reachable from
    inside.
    """
    node = PostProcessNode()
    if state.get("status") == AgentStatus.ERROR.value:
        return node.execute(state)
    return node(state)


class TestErrorEnvelopeIsClosedSet:
    """On every non-success path the caller receives closed-set labels only."""

    @pytest.mark.parametrize("state", _NON_SUCCESS_STATES)
    def test_every_envelope_value_is_a_declared_constant(self, state):
        result = _run(state)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert envelope, "a falsy envelope re-opens the `formatted_output or result` fallback"
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize("state", _NON_SUCCESS_STATES)
    def test_error_log_text_appears_nowhere_in_the_returned_mapping(self, state):
        """The sentinel is in error_log when post_process runs; it must reach
        no key and no value of what post_process returns, at any depth."""
        assert _leaked(state["error_log"]), "the seed must be in place for the assertion to mean anything"

        result = _run(state)

        assert _leaked(result) == [], _leaked(result)
        assert _BEARER_VALUE not in " ".join(_every_string(result))
        assert _BEARER_KEY not in " ".join(_every_string(result))

    @pytest.mark.parametrize("state", _NON_SUCCESS_STATES)
    def test_every_output_bearing_field_is_cleared(self, state):
        result = _run(state)
        retained = [field for field in _OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, f"output-bearing state not cleared: {retained}"

    def test_reason_code_names_the_path_taken(self):
        assert _run(_errored_after_the_call())["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert _run(_refused(record_id="", record_ref=""))["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; the inner entries are already
        there, so post_process writes no error_log on the inner-error path."""
        result = _run(_errored_after_the_call())
        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = _run(_refused(record_id="", record_ref=""))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "error" not in result["formatted_output"]
        assert "output gate" not in " ".join(_every_string(result["formatted_output"]))

    def test_credential_shaped_key_is_withheld_from_the_label(self):
        """Through node(state), so the framework's own credential scan runs over
        the result: the violation label names the place without repeating the
        key, the scan lets the cleared delta through, and the caller receives
        the reason code - not a bare framework error with `result` intact."""
        result = PostProcessNode()(_refused(freee_payload=to_json({"manual_journal": {_BEARER_KEY: _BEARER_VALUE}})))
        labels = " ".join(result["error_log"])

        assert "freee_payload.manual_journal.<withheld>" in labels
        assert _BEARER_KEY not in labels
        assert _BEARER_VALUE not in labels
        assert "S-3 output gate" not in labels, "the framework scan raised on the label - the delta was discarded"
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] is None

    def test_credential_shaped_key_with_a_clean_value_is_refused(self):
        """A key is scanned as a string in its own right: a credential rides a
        key as easily as a value, and a SUCCESS output carrying one ships it."""
        violations = _security_gate_output(
            {"record_id": "4021", "freee_payload": {"manual_journal": {_BEARER_KEY: "Travel Expenses"}}},
            is_success=True,
        )
        assert len(violations) == 1
        assert "freee_payload.manual_journal.<withheld>" in violations[0]
        assert _BEARER_KEY not in violations[0]

    def test_clean_response_carries_no_reason_code(self):
        """CONTROL: the reason codes are error vocabulary only."""
        result = PostProcessNode()(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "reason" not in result["formatted_output"]
        assert not any(reason in " ".join(_every_string(result)) for reason in ERROR_REASONS)
