"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# outer state fields or the caller's input_context, so the validated journal
# data collected by PreProcessNode (the entry hint and the structured journal
# fields) would never reach the inner workflow on its own. The sanctioned
# subclass hooks bridge it:
#
#   FreeeWorkflowGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       -> set_caller_journal_context({"entry_hint": ..., "journal": ...})
#   FreeeWorkflowGraph._extra_initial_state()    [runs INSIDE subgraph.invoke]
#       -> seeds {"entry_hint": ..., "caller_journal": <JSON>}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - the hint already passed the bounded, inert shape check and
# every journal field passed its own bounds - never the raw request body.
#
# The alternative, smuggling the data inside the validated_input JSON, is not
# reliable here: the framework masks that field at node boundaries, and real
# accounting data trips the masking heuristics - a Title Case account title
# ("Travel Expenses") is rewritten to "[MASKED]" and a long journal-entry
# number is rewritten as a digit run, so the freee call would carry corrupted
# caller data. This channel is not masked.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's journal data.

from contextvars import ContextVar
from typing import Any

_CALLER_JOURNAL_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar(
    "cmn_c2_277_caller_journal_context", default=None
)


def set_caller_journal_context(journal_context: "dict[str, Any] | None") -> None:
    """Stash the validated caller journal data for the imminent inner-graph invoke."""
    _CALLER_JOURNAL_CONTEXT.set(dict(journal_context) if journal_context else {})


def get_caller_journal_context() -> "dict[str, Any]":
    """Read (without consuming) the stashed journal data; {} when none was set."""
    return _CALLER_JOURNAL_CONTEXT.get() or {}
