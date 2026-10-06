# PB-6: Backbone invoke-order + external-trust boundary (CMN-C2-277).
#
# A real outer-graph invoke exercising the fixed 5-node backbone
# (initialize -> pre_process -> main[inner freee workflow] -> post_process -> finalize).
#
# The SINGLE external trust gate lives on the outer backbone pre_process
# (required_trust_level = VERIFIED_EXTERNAL). GraphNode.execute() passes the
# caller's InvocationContext into the inner subgraph UNCHANGED (no trust
# elevation), so the inner freee call (CallFreeeApiNode) is ANONYMOUS and
# runs under the caller's already-gated context. Two trust levels are asserted:
#
#   * VERIFIED_EXTERNAL (a real external caller - never for_internal()): passes
#     the pre_process gate, so the full backbone runs IN ORDER and the record
#     evidence surfaces in result["output"] (status success). The payload is
#     byte-equal to deploy/invoke_payload.json's "input" - this is exactly the
#     first-invoke path used for deployment sign-off.
#   * ANONYMOUS (an under-trusted caller): denied at the pre_process gate before
#     the inner freee call can run -> status=error, no record evidence.
#
# The freee call is served by the deterministic NETWORK-FREE stub transport
# (no live company, no secret needed - the node runs on the documented stub
# placeholder). Record evidence is asserted by presence (mask-robust: the
# framework/FinalizeNode may mask raw identifiers), never by whole-repr.

import json
import pathlib

import pytest

try:
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import FreeeAccountingEntryAgent

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_MAIN_SLOT_NODE = "FreeeWorkflowGraphNode"

# Byte-equal to deploy/invoke_payload.json "input" (asserted below - never retyped drift).
_VALID_PAYLOAD = "Look up journal entry number 4021 and summarize the entry on file."
_SESSION_ID = "signoff-001"

_PAYLOAD_PATH = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"

_EXPECTED_HISTORY = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,
    "PostProcessNode",
    "FinalizeNode",
]


def _build_agent():
    agent = FreeeAccountingEntryAgent()
    agent.compile()
    return agent


class TestBackboneInvokeOrder:
    def test_valid_payload_is_byte_equal_to_deploy_payload(self):
        """PB-6 uses the deployment sign-off request verbatim."""
        deployed = json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))
        assert deployed["input"] == _VALID_PAYLOAD
        assert deployed["session_id"] == _SESSION_ID

    def test_verified_external_runs_full_backbone_in_order(self):
        """External (VERIFIED_EXTERNAL) caller: the exact 5-node backbone runs
        in order and record evidence surfaces in result["output"]."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert result.get("trace_id") and result.get("correlation_id")
        assert result.get("node_history", []) == _EXPECTED_HISTORY
        # Record evidence + confirmation surface in result["output"] (presence,
        # mask-robust - never the raw identifier value).
        out = result.get("output") or {}
        assert out.get("record_id") or out.get("record_ref")
        assert out.get("confirmation")
        assert out.get("intent") == "lookup_entry"

    def test_under_trusted_caller_is_denied(self):
        """Under-trusted (ANONYMOUS) caller: ANONYMOUS < VERIFIED_EXTERNAL, so
        the pre_process gate refuses the request before the inner freee call
        can run - a well-formed error surface (gated, not crashed) with no
        record evidence. Note: the main slot NAME still appears in node_history
        because BaseNode.__call__ short-circuits on the already-errored state
        (its subgraph never executes); post_process is skipped by route()."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-anon", caller_trust_level=TrustLevel.ANONYMOUS),
        )
        assert result["status"] == AgentStatus.ERROR.value, f"result={result!r}"
        assert "PostProcessNode" not in result.get("node_history", [])
        assert not result.get("output")

    def test_empty_input_surfaces_error_not_crash(self):
        agent = _build_agent()
        result = agent.invoke(
            user_input="   ",
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.ERROR.value
