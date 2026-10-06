"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_entry / create_entry /
check_balance. A deterministic keyword heuristic is the always-available
baseline (docs/02_design.md, "Implementation Note - LLM synthesis"), so the
template runs and tests without a live LLM. When the three Azure OpenAI
secrets are provisioned, an LLM classification pass overrides the heuristic
when it returns a valid, well-formed result - any failure (missing secret,
API error, malformed response, an intent outside the closed set) silently
keeps the heuristic result. Low-confidence / unknown heuristic output falls
back to the read-only "lookup_entry" default with a note - never a write.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

_VALID_INTENTS = ("lookup_entry", "create_entry", "check_balance")

# Deterministic keyword signals (checked in priority order, the write first so
# a "book the entry then show it" style request classifies as the write).
_KEYWORDS = (
    (
        "create_entry",
        (
            "create",
            "register",
            "book a",
            "book the",
            "post a journal",
            "post an entry",
            "add a journal",
            "add an entry",
            "new entry",
            "new journal",
            "記帳",
            "計上",
            "起票",
            "登録",
            "作成",
            "追加",
        ),
    ),
    ("check_balance", ("balance", "how much", "trial balance", "残高", "試算表")),
    (
        "lookup_entry",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "summarize",
            "list the",
            "entry for",
            "entry of",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)

_SYSTEM_PROMPT = (
    "You are an accounting-request classifier. Given a (possibly Japanese or "
    "English) natural-language accounting request, classify it into exactly "
    'one of three intents: "lookup_entry" (read an existing journal entry), '
    '"create_entry" (book/post/register a NEW journal entry), or '
    '"check_balance" (read an account balance or trial balance). Respond with '
    'a single JSON object only, no prose, no markdown fences: {"intent": '
    '"lookup_entry"|"create_entry"|"check_balance"}. If the request is '
    'ambiguous or you are not confident, respond with "lookup_entry" (the '
    "safe, read-only default) rather than guessing a write intent."
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a freee accounting operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        # `llm` is a test-double seam only - register_nodes() never passes one
        # in production. The real client is built fresh per invocation in
        # _classify_via_llm() from ctx.secrets (api_key + azure_endpoint +
        # azure_deployment, all three declared secrets - none of it lives in
        # config/config.yaml), not cached on self: node instances are
        # constructed once (registry LRU cache, shared across every
        # invocation) before any request's secrets are provisioned, and
        # caching one caller's client would leave it visible to the next
        # caller.
        self._llm = llm

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        heuristic_intent = self._classify_via_keywords(text)
        note: list[str] = []
        if heuristic_intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_entry (read-only)"]
            heuristic_intent = "lookup_entry"

        llm_intent = self._classify_via_llm(text, state)
        intent = llm_intent if llm_intent is not None else heuristic_intent

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note) and llm_intent is None, "llm_inferred": llm_intent is not None},
            state,
        )

        result: "dict[str, Any]" = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note and llm_intent is None:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"

    def _classify_via_llm(self, text: str, state: "dict[str, Any]") -> "str | None":
        """LLM-based intent classification. Returns a valid intent, or None on
        any failure (missing secret, API error, malformed/non-JSON response,
        an intent outside the closed set) - caller keeps the heuristic result.
        Never raises."""
        try:
            llm = self._llm
            if llm is None:
                ctx = InvocationContext.from_state(state)
                llm = AzureOpenAIClient(
                    {
                        "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                        "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                        "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
                    }
                )
            response = llm.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ]
            )
            parsed = extract_json_object(response.get("content", ""))
            intent = parsed.get("intent")
            if isinstance(intent, str) and intent in _VALID_INTENTS:
                return intent
            return None
        except Exception:
            return None
