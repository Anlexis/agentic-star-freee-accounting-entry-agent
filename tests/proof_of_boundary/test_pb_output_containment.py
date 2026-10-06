# Boundary test: a blocked response must not ship the answer it blocked.
#
# The mechanism under test is easy to get wrong and invisible in unit tests.
# The base graph shapes its response as
#
#     formatted_output OR result
#
# so an output gate that merely raises - or returns an error status without
# clearing state - still hands the caller the un-gated inner answer inside the
# error envelope. Containment therefore means the answer is GONE, not relabelled.
#
# The whole compiled backbone runs here (initialize -> pre_process -> main ->
# post_process -> finalize) with a stand-in for the `main` slot that produces a
# result the gate must refuse. The stand-in exists because the real inner
# workflow cannot reach post_process with an ungated answer - its own steps
# refuse first - and the property being asserted belongs to the OUTER backbone.

import pytest

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import FreeeAccountingEntryAgent
from src.nodes.post_process_node import (
    ERROR_REASONS,
    _REASON_OUTPUT_WITHHELD,
    PostProcessNode,
    _contain,
)
from src.nodes.pre_process_node import PreProcessNode

_RELEASED_TEXT = "Retrieved journal entry 'confidential acquisition accrual' - internal ledger extract"
_REQUEST = "Look up journal entry number 4021 and summarize the entry on file."


class _StandInMainNode(FunctionNode):
    """Stands in for the inner workflow; emits an answer with chosen evidence."""

    required_trust_level = TrustLevel.ANONYMOUS

    record_id = ""
    record_ref = ""

    def execute(self, state):
        return {
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_entry",
            "record_id": self.record_id,
            "record_ref": self.record_ref,
            "account_title": "confidential acquisition accrual",
            "confirmation": _RELEASED_TEXT,
            "result": {"record_id": self.record_id, "confirmation": _RELEASED_TEXT},
        }


class _StandInAgent(FreeeAccountingEntryAgent):
    main_node_class = _StandInMainNode

    def register_nodes(self):
        AgentBaseGraph.register_nodes(self)  # initialize + finalize
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = self.main_node_class()
        self._nodes["post_process"] = PostProcessNode()


def _build(record_id="", record_ref=""):
    class _Configured(_StandInMainNode):
        required_trust_level = TrustLevel.ANONYMOUS

    _Configured.record_id = record_id
    _Configured.record_ref = record_ref

    class _Agent(_StandInAgent):
        main_node_class = _Configured

    agent = _Agent()
    agent.compile()
    return agent


def _invoke(agent, caller_id):
    return agent.invoke(
        user_input=_REQUEST,
        session_id="pb-containment",
        ctx=InvocationContext(caller_id=caller_id, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )


class TestOutputContainment:
    def test_the_base_graph_falls_back_to_result(self):
        """Why clearing is required, asserted against the framework itself.

        If this stops being true the containment below is belt-and-braces
        rather than load-bearing - but while it holds, an uncleared `result`
        ships whatever the gate refused.
        """
        shaped = _build().get_output({"result": _RELEASED_TEXT})
        assert shaped["output"] == _RELEASED_TEXT

    def test_gate_violation_ships_no_released_text(self):
        result = _invoke(_build(), "contained")
        assert result["status"] == AgentStatus.ERROR.value
        # The caller gets the gate's finding and nothing else: no record
        # evidence, no confirmation, no account label, no request body.
        envelope = result["output"]
        # The envelope carries closed-set labels only: a constant reason code,
        # no record keys (not even empty ones), none of the gate's own findings
        # - those are error_log entries, internal - and it stays truthy so the
        # `formatted_output or result` fallback never fires.
        assert envelope, "a falsy envelope re-opens the `or result` fallback"
        assert envelope == {"reason": _REASON_OUTPUT_WITHHELD}
        assert set(envelope.values()) <= ERROR_REASONS
        assert "error_log" not in result
        rendered = repr(result)
        assert "output gate" not in rendered
        assert _RELEASED_TEXT not in rendered
        assert "confidential acquisition accrual" not in rendered

    def test_error_envelope_carries_no_traceback_or_source_path(self):
        envelope = repr(_invoke(_build(), "contained-no-trace"))
        assert "Traceback" not in envelope
        assert '.py", line' not in envelope
        assert "/src/nodes/" not in envelope
        assert "site-packages" not in envelope

    def test_negative_control_a_compliant_answer_still_ships(self):
        """Proves the gate - not a broken graph - is what suppressed the answer."""
        result = _invoke(_build(record_id="4021", record_ref="freee://manual_journals/4021"), "allowed")
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["output"]["record_id"] == "4021"
        assert result["output"]["confirmation"] == _RELEASED_TEXT


@pytest.mark.parametrize(
    "field",
    [
        "result",
        "confirmation",
        "record_id",
        "record_ref",
        "entry_id",
        "account_title",
        "balance",
        "freee_payload",
        "intent",
    ],
)
def test_every_output_bearing_field_is_cleared_by_the_gate(field):
    """A field added to the response shape later must be added to the clear list."""
    contained = _contain(_REASON_OUTPUT_WITHHELD, ["gate: refused"])
    assert field in contained
    assert not contained[field]


@pytest.mark.parametrize(
    "field",
    [
        "result",
        "confirmation",
        "record_id",
        "record_ref",
        "entry_id",
        "account_title",
        "balance",
        "freee_payload",
        "intent",
    ],
)
def test_every_output_bearing_field_is_cleared_on_the_existing_error_path_too(field):
    """The SAME clear list applies to the pre-existing-ERROR branch.

    molt source review (wave-8, 2026-09-04): the gate path cleared state and
    the independent errored branch did not, so the two are pinned to the same
    list here - a field added to one and not the other is the bug that was
    found.
    """
    errored = PostProcessNode().execute(
        {
            "status": AgentStatus.ERROR.value,
            "record_id": "70211",
            "record_ref": "freee://manual_journals/70211",
            "entry_id": "70211",
            "account_title": "confidential acquisition accrual",
            "balance": "1234567",
            "intent": "lookup_entry",
            "confirmation": _RELEASED_TEXT,
            "freee_payload": '{"journal_id":"70211"}',
            "result": {"record_id": "70211", "confirmation": _RELEASED_TEXT},
            "error_log": ["CallFreeeApiNode: freee returned HTTP 403"],
            "correlation_id": "pb-error-containment",
            "node_history": [],
            "execution_time": {},
        }
    )
    assert field in errored
    assert not errored[field]


def test_the_compiled_graph_never_routes_an_errored_state_through_post_process():
    """The honest scope of the containment above.

    `AgentBaseGraph.route()` sends a terminal ERROR status to `finalize`, so
    `post_process` is not on the error path at all: the branch fixed above is
    source-level defence in depth, reachable by a direct `execute()` (a future
    route change, a direct caller, or this node copied into another template).

    Asserting it here keeps the claim honest and makes it a REGRESSION alarm:
    if a framework upgrade ever routes ERROR through post_process, this test
    fails and the containment stops being merely defensive.
    """

    class _ErroringMain(FunctionNode):
        required_trust_level = TrustLevel.ANONYMOUS

        def execute(self, state):
            return {
                "status": AgentStatus.ERROR.value,
                "intent": "lookup_entry",
                "record_id": "70211",
                "record_ref": "freee://manual_journals/70211",
                "account_title": "confidential acquisition accrual",
                "confirmation": _RELEASED_TEXT,
                "error_log": ["CallFreeeApiNode: freee returned HTTP 403"],
            }

    class _Agent(_StandInAgent):
        main_node_class = _ErroringMain

    agent = _Agent()
    agent.compile()
    result = _invoke(agent, "errored")

    assert result["status"] == AgentStatus.ERROR.value
    assert "PostProcessNode" not in (result.get("node_history") or [])
    # And nothing released ships anyway: the inner graph publishes no `result`
    # on an error, so the framework's `formatted_output or result` projection
    # has nothing to fall back onto.
    assert not result["output"]
    assert _RELEASED_TEXT not in repr(result)


# -- what the caller learns from a non-success return -------------------------
#
# Closed-set labels only. error_log is node-authored text that can embed an
# upstream response body (an API error), identifiers, names; it is the
# INTERNAL channel - the state reducer appends to it, the audit trail needs it
# - and it must reach no key and no value of the invoke result. The sentinel
# below is that kind of line, seeded by the stand-in main so it is in state
# when post_process runs.


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. Assembled at
    runtime (no credential-shaped literal is committed) and chosen to match no
    credential detector - text no redactor would catch."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


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


class _MainSeedingErrorLog(FunctionNode):
    """Succeeds WITHOUT record evidence - so the outer gate refuses - and leaves
    an upstream-shaped line in error_log for the reducer to carry into the
    state post_process reads."""

    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state):
        return {
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_entry",
            "record_id": "",
            "record_ref": "",
            "account_title": "confidential acquisition accrual",
            "confirmation": _RELEASED_TEXT,
            "result": {"confirmation": _RELEASED_TEXT},
            "error_log": [_sentinel()],
        }


def test_gate_refusal_on_the_compiled_graph_publishes_none_of_error_log():
    """The LIVE non-success path: the inner workflow succeeded, the outer gate
    refused. error_log holds the upstream-shaped line and the gate's own
    finding; the invoke result carries the reason code and nothing of either."""

    class _Agent(_StandInAgent):
        main_node_class = _MainSeedingErrorLog

    agent = _Agent()
    agent.compile()
    result = _invoke(agent, "gate-sentinel")

    assert result["status"] == AgentStatus.ERROR.value
    assert result["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
    assert "error_log" not in result
    assert _leaked(result) == [], _leaked(result)
    rendered = repr(result)
    assert "output gate" not in rendered
    assert _RELEASED_TEXT not in rendered


def test_inner_error_on_the_compiled_graph_publishes_none_of_error_log():
    """The other non-success path on the real route: an inner ERROR goes
    straight to finalize with `output` withheld outright, and the
    upstream-shaped line in error_log reaches no key and no value of the
    result."""

    class _ErroringMain(FunctionNode):
        required_trust_level = TrustLevel.ANONYMOUS

        def execute(self, state):
            # Mirrors the real FreeeWorkflowGraphNode on an inner error: no
            # `result` is written (error_strategy="propagate" raises before
            # merge_output), only the status and the log.
            return {
                "status": AgentStatus.ERROR.value,
                "intent": "lookup_entry",
                "error_log": [_sentinel()],
            }

    class _Agent(_StandInAgent):
        main_node_class = _ErroringMain

    agent = _Agent()
    agent.compile()
    result = _invoke(agent, "inner-error-sentinel")

    assert result["status"] == AgentStatus.ERROR.value
    assert not result["output"]
    assert "error_log" not in result
    assert _leaked(result) == [], _leaked(result)
