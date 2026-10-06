# CMN-C2-277 - Unit tests: inner FreeeWorkflowGraph (BaseGraph) contract.
# The compiled outer path is exercised end-to-end by
# tests/proof_of_boundary/test_pb_invoke_order.py and
# tests/proof_of_boundary/test_pb_invoke_endpoint.py; this module unit-checks
# the inner graph's identity, config forwarding, the caller-context bridge
# seeding, routing, the output contract, and a direct inner invoke on the
# network-free stub.

import inspect

import pytest

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_journal_context
from src.graph.domain_workflow_graph import FreeeWorkflowGraph
from src.schemas.state import State, from_json


@pytest.fixture(autouse=True)
def _clear_bridge():
    """The bridge is per-task state; clear it so tests cannot leak into each other."""
    set_caller_journal_context(None)
    yield
    set_caller_journal_context(None)


def _graph(config=None):
    return FreeeWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "freee_accounting_entry_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_freee_config_as_json():
    g = _graph({"configurable": {"freee": {"base_url": "https://freee.example.test/api/1"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict.
    assert isinstance(extra["freee_config"], str)
    assert from_json(extra["freee_config"], {}) == {"base_url": "https://freee.example.test/api/1"}


def test_extra_initial_state_carries_the_call_deadline():
    g = _graph({"configurable": {"freee": {"base_url": "https://freee.example.test/api/1"}, "timeout_s": 12}})
    assert from_json(g._extra_initial_state()["freee_config"], {})["timeout_s"] == 12


def test_extra_initial_state_empty_without_config_or_caller_data():
    assert _graph()._extra_initial_state() == {}


def test_extra_initial_state_seeds_the_caller_journal_from_the_bridge():
    """GraphNode does not forward input_context, so the validated caller data
    reaches the inner graph only through the bridge."""
    set_caller_journal_context({"entry_hint": "4021", "journal": {"debit": "Travel Expenses"}})
    extra = _graph()._extra_initial_state()
    assert extra["entry_hint"] == "4021"
    assert from_json(extra["caller_journal"], {}) == {"debit": "Travel Expenses"}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_route_is_annotated_with_this_graphs_own_state():
    """Regression guard on a silent-failure class.

    LangGraph reads a conditional path callable's annotation as its input
    schema and projects away every field the annotation does not declare, so a
    base-state annotation here would hand route() a state with the domain
    fields missing - and a unit suite that calls route() with a plain dict
    would never notice.
    """
    annotation = inspect.signature(FreeeWorkflowGraph.route).parameters["state"].annotation
    resolved = State if annotation in (State, "State") else annotation
    assert resolved is State


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "4021", "record_ref": "freee://manual_journals/4021", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_entry",
            "entry_id": "4021",
            "record_id": "4021",
            "record_ref": "freee://manual_journals/4021",
            "account_title": "supplies",
            "balance": "",
            "confirmation": "ok",
            "freee_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_entry"
    assert out["record_ref"] == "freee://manual_journals/4021"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "4021", "record_ref": "freee://manual_journals/4021", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"freee": {"base_url": "https://api.freee.co.jp/api/1"}}})
    g.compile()
    result = g.invoke(user_input="Look up journal entry number 4021 and summarize the entry on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "4021"
    assert result["record_ref"] == "freee://manual_journals/4021"
    assert result["intent"] == "lookup_entry"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferFreeeFieldsNode",
        "CallFreeeApiNode",
        "ConfirmNode",
    ]
