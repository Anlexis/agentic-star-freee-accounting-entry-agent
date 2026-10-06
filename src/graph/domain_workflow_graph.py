"""AgentCore Platform v1.0 - inner freee workflow graph (Cat 2 domain workflow).

Instantiated by FreeeWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_freee_fields
          -> call_freee_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    freee     - runtime integration section (base_url, company_id, ...);
                injected into State as the JSON `freee_config` field via
                _extra_initial_state() so the no-arg nodes can read it
    llm       - optional non-secret LLM tuning params only. ClassifyIntentNode
                / InferFreeeFieldsNode build their AzureOpenAIClient directly
                from ctx.secrets (docs/02, "Implementation Note") - not from
                this section, which is currently unused by either node
    timeout_s - the freee-call deadline, merged into `freee_config` and
                enforced by CallFreeeApiNode

The caller's validated journal data does not travel in config: GraphNode does
not forward input_context to the subgraph, so it crosses the boundary through
src/graph/context_bridge.py and is seeded here by _extra_initial_state().

Nodes are registered WITHOUT constructor arguments (nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_journal_context
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_freee_fields_node import InferFreeeFieldsNode
from src.nodes.call_freee_api_node import CallFreeeApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class FreeeWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "freee_accounting_entry_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the freee section is optional (the client
        # falls back to the documented default base_url + the network-free
        # stub transport), and a missing/unusable setting is handled at
        # CallFreeeApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_freee_fields"] = InferFreeeFieldsNode()
        self._nodes["call_freee_api"] = CallFreeeApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_freee_fields")
        self._sg.add_edge("infer_freee_fields", "call_freee_api")
        self._sg.add_edge("call_freee_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        #
        # The annotation is this graph's OWN State, not the framework base
        # state: LangGraph reads a path callable's annotation as its input
        # schema and PROJECTS AWAY every field the annotation does not declare,
        # so a base-state annotation would hand this method a state with the
        # domain fields missing and the branch decision would be made on
        # absent data.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Two things are seeded here, both of which the inner nodes cannot
        # reach any other way:
        #
        #   1. the `freee` settings section (arriving under
        #      config["configurable"] from _parent_config()), merged with the
        #      freee-call deadline, as a JSON string (msgpack-safe) so the
        #      no-arg CallFreeeApiNode can read it via freee_config;
        #   2. the caller's VALIDATED journal data, handed across the graph
        #      boundary by context_bridge.py because GraphNode.execute() does
        #      not forward input_context into the subgraph.
        configurable = self.config.get("configurable") or {}
        extra: "dict[str, Any]" = {}

        settings = dict(configurable.get("freee") or {})
        timeout_s = configurable.get("timeout_s")
        if timeout_s is not None:
            settings["timeout_s"] = timeout_s
        if settings:
            extra["freee_config"] = to_json(settings)

        caller = get_caller_journal_context()
        entry_hint = caller.get("entry_hint") or ""
        journal = caller.get("journal") or {}
        if entry_hint:
            extra["entry_hint"] = entry_hint
        if journal:
            extra["caller_journal"] = to_json(journal)
        return extra

    def get_output(self, state: State) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "entry_id": state.get("entry_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "account_title": state.get("account_title", ""),
            "balance": state.get("balance", ""),
            "confirmation": state.get("confirmation", ""),
            "freee_payload": state.get("freee_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
