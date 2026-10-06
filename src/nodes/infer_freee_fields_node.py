"""AgentCore Platform v1.0 - inner workflow Step 3: InferFreeeFields.

Assembles a validated freee accounting REST API request body for the
classified intent from three sources, in this priority order: (1) the
structured journal fields the caller supplied through `input_context`
(already validated and bounded by PreProcessNode, and carried across the
graph boundary by src/graph/context_bridge.py) - always wins; (2) the
journal-entry number, account title and "Key: value" fields extracted from
the (redacted) request text via deterministic regex - the always-available
baseline; (3) when the three Azure OpenAI secrets are provisioned, an LLM
extraction pass that ONLY fills fields the regex baseline found nothing for
(regex wins on conflict - see "LLM extraction" below for why). The
journal-entry number is taken only from an explicit number in the text or
the caller-supplied entry_hint - an unresolved number is left empty rather
than invented (risk mitigation: never touch the wrong journal entry; the
executor surfaces the miss as status=error). A create request assembles
journal lines only when the debit account, credit account, and a positive
amount are ALL explicit - a malformed or guessed entry is never booked. This
holds for every source: an LLM-extracted amount goes through the identical
finite/bounded validation as a regex-extracted one before it can contribute
to a booked entry.

LLM extraction (docs/02_design.md, "Implementation Note - LLM synthesis"):
unlike ClassifyIntentNode (where an LLM result overrides the heuristic), here
the regex baseline is deliberately kept authoritative on any conflict and the
LLM only fills gaps the regex found nothing for. This node's output can
trigger a real write to an external accounting system, so a fuzzy/paraphrased
LLM extraction is never allowed to silently replace an explicit, literal
regex match - it only extends coverage to phrasings the regex cannot catch.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

from src.schemas.state import from_json, to_json
from src.services.security import AMOUNT_MAX, AMOUNT_MIN, finite_int_in_range

# A freee manual-journal id: numeric identifier.
_ID_SHAPE_RE = re.compile(r"^\d{1,10}$")
# Explicit journal-entry number mention in the request text, EN or JA
# ("journal entry number 4021" / "entry #4021" / "仕訳番号 4021").
_ID_IN_TEXT_RE = re.compile(
    r"(?:journal\s+entry|journal|entry|voucher|transaction)\s+(?:no\.?|number|id)?\s*[:#]?\s*(\d{1,10})\b"
    r"|(?:仕訳番号|伝票番号|取引番号)\s*[:：#]?\s*(\d{1,10})",
    re.IGNORECASE,
)
# Quoted account title: account "Foo" / balance of "Foo". Curly quotes as \u
# escapes so the source stays pure ASCII (push-safe).
_ACCOUNT_QUOTED_RE = re.compile(r'(?:account|of|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" journal-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Inline amount mention ("¥5,000" / "5,000 yen" / "5000円").
_AMOUNT_IN_TEXT_RE = re.compile(r"(?:¥|\bJPY)\s*([0-9][0-9,]{0,14})|([0-9][0-9,]{0,14})\s*(?:yen|円)", re.IGNORECASE)
# Inline issue date (ISO form).
_DATE_IN_TEXT_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
# Keys that are the id / dedicated journal fields, not free-form extras.
_ID_KEYS = (
    "entry",
    "entry id",
    "entry number",
    "journal",
    "journal id",
    "journal number",
    "id",
    "仕訳番号",
    "伝票番号",
)
_ACCOUNT_KEYS = ("account", "account title", "勘定科目")
_DEBIT_KEYS = ("debit", "debit account", "借方")
_CREDIT_KEYS = ("credit", "credit account", "貸方")
_AMOUNT_KEYS = ("amount", "金額")
_DATE_KEYS = ("date", "issue date", "日付", "発生日")

# Canonical LLM-extraction keys -> the field key each is merged into `fields`
# under (the first alias in each _*_KEYS tuple above).
_LLM_FIELD_KEYS = ("entry", "account", "debit", "credit", "amount", "date")

_SYSTEM_PROMPT = (
    "You are an accounting-field extractor. Given a (possibly Japanese or "
    "English) natural-language accounting request, extract ONLY the fields "
    "explicitly stated in the text. Respond with a single JSON object only, "
    "no prose, no markdown fences, containing only the keys that are "
    'explicitly present: {"entry": string journal/entry number, "account": '
    'string account title, "debit": string debit account title, "credit": '
    'string credit account title, "amount": string numeric amount (digits '
    'only, no currency symbol or separators), "date": string ISO date '
    "YYYY-MM-DD}. Omit any key whose value is not explicitly stated - never "
    "guess, infer, or invent a value that is not in the text."
)


class InferFreeeFieldsNode(FunctionNode):
    """Extract entities and assemble the freee accounting API request body."""

    # Inner domain node - derives fields from already-validated data; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        # `llm` is a test-double seam only - register_nodes() never passes one
        # in production. The real client is built fresh per invocation in
        # _infer_fields_via_llm() from ctx.secrets, not cached on self (see
        # the module docstring / ClassifyIntentNode for why).
        self._llm = llm

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_entry") or "lookup_entry"
        entry_hint = state.get("entry_hint", "") or ""
        # Caller-supplied journal fields: already validated and bounded by
        # PreProcessNode, so they are trusted over anything inferred from text.
        caller = from_json(state.get("caller_journal"), {}) or {}
        if not isinstance(caller, dict):
            caller = {}

        if not text.strip() and not caller:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferFreeeFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        llm_fields = self._infer_fields_via_llm(text, state)
        llm_inferred = False
        if llm_fields:
            # Appended AFTER the regex fields: regex wins on conflict (first
            # match in _field_value's scan), LLM only fills keys the regex
            # found nothing for. See module docstring "LLM extraction".
            fields = fields + llm_fields
            llm_inferred = True

        entry_id = self._resolve_id(text, entry_hint, fields)
        account_title = str(caller.get("account", "")) or self._resolve_account(text, fields)

        if intent == "create_entry":
            payload = self._build_manual_journal(text, fields, caller)
            if not account_title:
                # Label the record with the explicit debit account when present.
                account_title = str(caller.get("debit", "")) or self._field_value(fields, _DEBIT_KEYS)
        elif intent == "check_balance":
            account = account_title
            if not account and entry_hint and not _ID_SHAPE_RE.match(entry_hint.strip()):
                account = entry_hint.strip()
            payload = {"account": account}
            account_title = account
        else:  # lookup_entry (read-only default)
            payload = {"journal_id": entry_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_freee_fields_complete",
            {
                "intent": intent,
                "has_entry_id": bool(entry_id),
                "n_fields": len(fields),
                "has_caller_journal": bool(caller),
                "llm_inferred": llm_inferred,
            },
            state,
        )

        return {
            "entry_id": entry_id,
            "account_title": account_title,
            "freee_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_id(self, text: str, entry_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit number only: text mention > id-shaped hint > 'Entry:' field. Never invented."""
        m = _ID_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = entry_hint.strip()
        if hint and _ID_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _ID_KEYS and _ID_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_account(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _ACCOUNT_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:100]
        return self._field_value(fields, _ACCOUNT_KEYS)

    def _field_value(self, fields: "list[tuple[str, str]]", keys: "tuple[str, ...]") -> str:
        for key, value in fields:
            if key.strip().lower() in keys:
                return value.strip()[:100]
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] journal fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    def _resolve_amount(self, text: str, fields: "list[tuple[str, str]]") -> int:
        """Explicit in-range amount only (KV line or inline mention); 0 = unresolved.

        Every value goes through the finite+bounded parser, so a non-finite or
        absurd magnitude is refused (0 = unresolved -> no journal lines are
        assembled) rather than posted.
        """
        raw = self._field_value(fields, _AMOUNT_KEYS)
        if not raw:
            m = _AMOUNT_IN_TEXT_RE.search(text)
            if m:
                raw = m.group(1) or m.group(2) or ""
        raw = raw.replace(",", "").replace("¥", "").strip()
        return finite_int_in_range(raw, AMOUNT_MIN, AMOUNT_MAX) or 0

    def _resolve_date(self, text: str, fields: "list[tuple[str, str]]") -> str:
        raw = self._field_value(fields, _DATE_KEYS)
        if raw:
            in_field = _DATE_IN_TEXT_RE.search(raw)
            if in_field:
                return in_field.group(1)
        m = _DATE_IN_TEXT_RE.search(text)
        return m.group(1) if m else ""

    # -- LLM extraction ---------------------------------------------------------

    def _infer_fields_via_llm(self, text: str, state: "dict[str, Any]") -> "list[tuple[str, str]] | None":
        """LLM-based field extraction. Returns [(key, value), ...] for only the
        canonical keys the LLM explicitly found, or None on any failure
        (missing secret, API error, malformed/non-JSON response, no text) -
        caller keeps the regex-only baseline. Never raises."""
        if not text.strip():
            return None
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
            if not isinstance(parsed, dict):
                return None
            result: list[tuple[str, str]] = []
            for key in _LLM_FIELD_KEYS:
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    result.append((key, value.strip()))
            return result or None
        except Exception:
            return None

    # -- payload assembly (freee manual_journals request shape) ----------------

    def _build_manual_journal(
        self, text: str, fields: "list[tuple[str, str]]", caller: "dict[str, Any]"
    ) -> "dict[str, Any]":
        """Assemble {"manual_journal": {...}} - journal lines ONLY when the debit
        account, credit account, and an in-range amount are all explicit.

        Caller-supplied fields win over text inference (regex or LLM): they
        were validated against explicit bounds at the entry point. Neither
        source can invent a missing field.
        """
        debit = str(caller.get("debit", "")) or self._field_value(fields, _DEBIT_KEYS)
        credit = str(caller.get("credit", "")) or self._field_value(fields, _CREDIT_KEYS)
        caller_amount = caller.get("amount")
        amount = caller_amount if isinstance(caller_amount, int) else self._resolve_amount(text, fields)
        journal: "dict[str, Any]" = {}
        issue_date = str(caller.get("issue_date", "")) or self._resolve_date(text, fields)
        if issue_date:
            journal["issue_date"] = issue_date
        details: "list[dict[str, Any]]" = []
        if debit and credit and amount > 0:
            # v1 carries account titles as labels; live integration resolves
            # account_item_id via /api/1/account_items (docs/02, "Limitation").
            details = [
                {"entry_side": "debit", "account_title": debit, "amount": amount},
                {"entry_side": "credit", "account_title": credit, "amount": amount},
            ]
        journal["details"] = details
        return {"manual_journal": journal}
