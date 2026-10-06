"""AgentCore Platform v1.0 - CMN-C2-277 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in FreeeWorkflowGraphNode (`main`
slot), which wraps the inner FreeeWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import Any, ClassVar

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import set_caller_journal_context
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State, from_json

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
# config/agent.yaml is the static registry manifest (flat, no `agent:` block);
# runtime parameters (max_retry, timeout_s) and the `freee` integration section
# live in config/config.yaml, which is what the graph reads at run time.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class FreeeWorkflowGraphNode(GraphNode):
    """Wraps the inner freee workflow graph; assigned to the `main` slot.

    No constructor arguments (nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads the runtime config.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "Any":
        from src.graph.domain_workflow_graph import FreeeWorkflowGraph

        return FreeeWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # Hand the validated caller journal data across the graph boundary (set
        # before subgraph.invoke; the inner _extra_initial_state() reads it).
        # The data must not ride only inside the validated_input JSON: the
        # framework's PII mask rewrites that field at node boundaries and real
        # accounting data (a Title Case account title such as "Travel
        # Expenses") trips the masking heuristics - see context_bridge.py.
        set_caller_journal_context(
            {
                "entry_hint": str(state.get("entry_hint") or ""),
                "journal": from_json(state.get("caller_journal"), {}) or {},
            }
        )
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "entry_id": sub_result.get("entry_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "account_title": sub_result.get("account_title", ""),
            "balance": sub_result.get("balance", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "freee_payload": sub_result.get("freee_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Loads config/config.yaml and forwards the `freee:` integration section,
        an optional `llm:` section (non-secret LLM tuning params only - the
        Azure OpenAI credentials themselves come from ctx.secrets, never
        config/config.yaml, see docs/02 "Implementation Note") and the
        freee-call deadline `timeout_s` - never an empty {} (an empty
        _parent_config() would silently make every declared setting dead).
        `max_retry` is not re-forwarded: it is consumed by the outer graph
        itself, which the framework run loop reads from the config passed to
        the graph constructor.
        """
        try:
            runtime = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            # Missing/unreadable config: inner nodes fall back to safe
            # defaults (documented network-free stub client).
            runtime = {}
        configurable: "dict[str, Any]" = {}
        for key in ("freee", "llm"):
            if key in runtime:
                configurable[key] = runtime[key]
        if "timeout_s" in runtime:
            configurable["timeout_s"] = runtime["timeout_s"]
        return {"configurable": configurable}


class FreeeAccountingEntryAgent(AgentBaseGraph):
    """CMN-C2-277 outer graph - freee Accounting Entry Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in FreeeWorkflowGraphNode (`main` slot); freee
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_277"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = FreeeWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
