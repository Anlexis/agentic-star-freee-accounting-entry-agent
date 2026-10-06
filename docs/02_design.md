# Template Design Specification — CMN-C2-277 freee Accounting Entry Agent

## Position in AgentCore Architecture

| Item | Value |
|------|-------|
| Agent class | `FreeeAccountingEntryAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 (multi-step domain workflow, tool-calling) |
| Composition | outer 5-node backbone; the domain pipeline is encapsulated in a `GraphNode` (`main` slot) wrapping an inner `BaseGraph` (`src/graph/domain_workflow_graph.py`) |
| Pipeline shape | classify intent → extract journal fields → build a freee request → call the tool → format the confirmation. No retrieval, no autonomous loop. |

**Three-layer separation**

- State: flat TypedDict `State(AgentState)` — no Pydantic (checkpoint serialization is msgpack-based).
- Node: framework inheritance; only `execute(self, state) -> dict` is overridden.
- Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()` is not
  overridden on the outer graph — backbone wiring belongs to the framework).

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | own the caller-data contract: validate `input_context` field-by-field, screen for injection content, sanitize and serialize the request into `validated_input` (JSON) | user_input, input_context | validated_input, entry_hint, caller_journal | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner freee workflow subgraph | validated_input, entry_hint, caller_journal | result, intent, entry_id, record_id, record_ref, account_title, balance, confirmation, freee_payload | GraphNode (caller ctx forwarded unchanged) | FreeeWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; enforce the output contract via the module-level `_security_gate_output()` | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline steps map 1:1
to inner nodes. **Every inner domain node declares `required_trust_level = TrustLevel.ANONYMOUS`** —
the caller's `InvocationContext` is forwarded into the subgraph unchanged, so the single external
trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, entry_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_entry / create_entry / check_balance (optional Azure OpenAI override when configured, see "Implementation Note — LLM synthesis"); low-confidence -> lookup_entry (read-only default — never a write) | intent | ANONYMOUS |
| infer_freee_fields | 3 InferFreeeFields | assemble the freee request body from the validated caller fields first, then from journal-entry number / account title / debit-credit-amount extracted from the request text via regex (optional Azure OpenAI gap-fill when configured, regex wins on conflict — see "Implementation Note"); an unresolved journal-entry number is left empty (never invented) | account_title, entry_id, freee_payload | ANONYMOUS |
| call_freee_api | 4 CallFreeeApi | GET /manual_journals/{id} (lookup) / POST /manual_journals (create) / GET /reports/trial_bs (balance) via `FreeeClient`; token via ctx.secrets; deadline enforced; 4xx/5xx -> status=error | record_id, record_ref, entry_id, account_title, balance | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference (+ balance) into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / FreeeWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_freee_fields
              -> call_freee_api -> confirm -> END
```

The request travels as a JSON string: `pre_process` serializes `{"text", "entry_hint"}` into
`validated_input`, `FreeeWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back. The **structured** caller fields take a
different route — see "Caller-data contract" below.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (fields are absent until their producer node
writes them). Dict/list payloads are stored as JSON strings (`Optional[str]`) via the module
helpers `to_json` / `from_json`, used by every producer and consumer, because checkpoint
serialization does not accept nested containers.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| entry_hint | NotRequired[str] | caller-supplied journal-entry number / account fragment; never inferred | pre_process / inner graph seeding |
| caller_journal | NotRequired[Optional[str]] | JSON — the validated structured journal fields supplied through `input_context` | pre_process / inner graph seeding |
| entry_id | NotRequired[str] | resolved freee manual-journal (仕訳) id | infer_freee_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| account_title | NotRequired[str] | account title (勘定科目) / record label | infer_freee_fields / call_freee_api |
| freee_payload | NotRequired[Optional[str]] | JSON — assembled freee accounting REST API request body | infer_freee_fields |
| freee_config | NotRequired[Optional[str]] | JSON — the `freee:` runtime section plus the call deadline | inner graph |
| record_id | NotRequired[str] | manual-journal id / account label returned by freee | call_freee_api |
| record_ref | NotRequired[str] | human-readable record reference (`freee://manual_journals/<id>` / `freee://accounts/<name>`) | call_freee_api |
| balance | NotRequired[str] | closing balance returned for a check_balance request | call_freee_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from `AgentState` and are
**not** re-declared.

**State constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the freee token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration (`config/agent.yaml` + `config/config.yaml`)

Two files, two jobs:

| File | Contents | Read by |
|------|----------|---------|
| `config/agent.yaml` | the FLAT registry manifest — every key at root level, no `agent:` block. Identity, entry point, `required_trust_level`, and the compile-time `requires` gates. | the platform registry |
| `config/config.yaml` | runtime parameters: `max_retry`, `timeout_s`, and the `freee:` integration section. | the graph constructor; `FreeeWorkflowGraphNode._parent_config()` |

Nodes take **no constructor arguments** (nodes are no-arg; configuration never rides on node
instances). `FreeeWorkflowGraphNode._parent_config()` loads `config/config.yaml` and forwards the
`freee:` section, `llm:` when declared, and `timeout_s` to the inner graph under
`config["configurable"]` — never `{}`. The inner graph's `_extra_initial_state()` merges the
deadline into the freee settings and injects them into State as the JSON `freee_config` field,
where `CallFreeeApiNode.execute(state, config=None)` reads them (an explicit
`config["configurable"]["freee"]` override is also honoured for direct/unit invocation).

`max_retry` is consumed by the outer graph itself: the framework run loop reads it from the config
passed to the graph constructor, which `src/api/server.py` supplies. `timeout_s` is the deadline
`CallFreeeApiNode` enforces on the freee call.

**`requires.secrets` is deliberately empty.** The integration token is read with
`ctx.secrets.get("FREEE_TOKEN")` — optional by contract — and the default network-free transport
runs without a credential. Declaring a secret the deployment does not provision fails the agent at
compile time, so the declaration is added only when a live transport and a provisioned token
arrive together.

## Caller-data contract

`POST /invoke` accepts `input_context` alongside the request text. `PreProcessNode` owns that
contract: every field is validated against explicit bounds, and a malformed value is refused with
an error naming the FIELD, never echoing the value.

| Field | Shape | Bound |
|-------|-------|-------|
| `entry_id` / `entry_hint` / `journal_id` | string (or integer) — first present wins | inert charset, ≤ 64 characters |
| `journal.account` / `journal.debit` / `journal.credit` | string | inert charset, ≤ 100 characters |
| `journal.amount` | number or numeric string | finite whole number, 1 … 1,000,000,000,000 |
| `journal.issue_date` | string | explicit ISO date `YYYY-MM-DD` |

An unsupported field inside `journal` is refused (silently dropping a misspelled field would book
an entry missing data the caller supplied); the refusal names the container, not the key, because
a field name is caller-controlled text too. Absent caller data is **not** an error — the pipeline
degrades to inferring what it can from the request text.

**The inert charset** (letters in any script, digits, underscore, space, hyphen, parentheses, the
Japanese middle dot) is a rendering constraint, not a language one: every one of these values is
echoed back inside the confirmation, so free text there would be caller-controlled output
injection. Japanese account titles such as 旅費交通費 and 売掛金（未収） pass unchanged.

**Every caller-controlled number goes through a finite+bounded parser.** `NaN` and `Infinity`
parse cleanly via `float()` and arrive intact through raw JSON, and every comparison against `NaN`
is False — so an unchecked value would silently disable the exact bound it must satisfy. The
parser rejects booleans, non-numerics, non-finite values, non-integral floats, and out-of-range
magnitudes, and fails closed.

### Why the structured fields do not travel in the request text

The framework's PII masking rewrites `user_input` / `validated_input` at every node boundary, and
real accounting data trips its heuristics: a Title Case account title ("Travel Expenses") becomes
`[MASKED]` before any node reads it, so a journal assembled from the text channel would carry
corrupted data. Structured caller data therefore travels through `input_context`, which is not
masked.

`GraphNode.execute()` invokes the subgraph as `subgraph.invoke(user_input, session_id=..., ctx=...)`
and does not forward `input_context` or outer state, so the validated fields cross the
outer→inner boundary through `src/graph/context_bridge.py`: the outer node's `extract_input()`
stashes them in a `ContextVar` immediately before the subgraph call, and the inner graph's
`_extra_initial_state()` seeds them into the inner state. A ContextVar keeps the hand-off correct
per thread/task, so concurrent invocations in one process cannot see each other's data.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner domain node —
  **including the write-capable `CallFreeeApiNode`** — declares `TrustLevel.ANONYMOUS`.
  `GraphNode.execute()` forwards the caller's `InvocationContext` into the subgraph **unchanged**
  (no elevation), and `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary is therefore enforced
  exactly once, at `pre_process`. Agent-level default trust `VERIFIED_EXTERNAL` is declared in
  `config/agent.yaml`. `src/api/server.py` enforces the standalone entry-point Bearer-token auth
  boundary (`INVOKE_AUTH_TOKEN` → VERIFIED_EXTERNAL elevation).
- **Injection screen (template-owned)** — `PreProcessNode` refuses chat-template control tokens
  (`<|…|>`, `[INST]`, `<<SYS>>`, forged role tags) and instruction/role-override phrasing. The
  template owns this guarantee itself rather than relying on any upstream gate being active: where
  such a gate is absent or configured off, an inherited refusal becomes a SUCCESS. Two details
  matter:
  - **Both channels, both representations.** The request text and every string in `input_context` —
    keys included, at any depth, after JSON parsing — are screened RAW and again after the markup
    strip. A markup strip is not a refusal and can make an attack *harder* to see: it swallows
    `<|im_start|>` whole and forwards the directive that followed it as ordinary prose, and it can
    splice `ig<b>nore all rules` back into a matchable phrase.
  - **Text is normalized first** (URL-decoding, NFKC, zero-width strip) so escaped or homoglyph
    variants do not slip past the patterns.
  Refusals name the pattern TYPE only; hostile content is never echoed. Phrase patterns are
  anchored to high-confidence forms so real bookkeeping language ("please disregard the earlier
  draft entry", "it will act as a clearing entry") is unaffected — an over-eager screen blocks the
  work the template exists to do.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a deterministic (regex, not LLM)
  scan for email addresses and access-token-like strings (`eyJ…`, `secret_…`, `sk-…`) and redacts
  them before any logging. An accounting request legitimately names partners and accounts, so this
  is flag-and-redact for safe logging, not a hard reject; the only deterministic auto-reject is the
  empty/non-request guard.
- **Secrets** — the integration token is read via `ctx.secrets.get("FREEE_TOKEN")`
  (`InvocationContext.from_state(state)`), never `os.environ`, never stored in State. A missing
  token is tolerated **only** while the deterministic network-free transport is active (no live
  call is made); with a live transport injected, a missing token is a hard `status=error`.
- **Error surface** — an error names the FIELD or the condition, never the caller value that
  produced it, and never a transport exception's text. Both are attacker- or upstream-controlled
  strings that would otherwise be echoed into the caller-facing envelope.

## Output contract

The caller-facing `formatted_output` is shaped by `PostProcessNode` and enforced by the
module-level `_security_gate_output()` in `src/nodes/post_process_node.py`. It is deliberately not
an instance method and not a framework `_extra_` hook — the framework gate methods are `@final` on
`FunctionNode` and the real runtime auto-wraps `_extra_` hooks, so domain checks live in a
module-level helper invoked inline.

The gate enforces two properties:

1. **No unevidenced success.** A SUCCESS response must carry `record_id` or `record_ref` — a
   confirmation without the record it refers to would misrepresent the freee action's outcome.
2. **No credential-shaped string anywhere in the output**, including strings nested inside
   mappings and lists. The assembled freee request body is a nested structure, so a scan of
   top-level strings only would miss a value riding one level down. Violations name the field
   PATH, never the value.

**On EVERY error return the node CLEARS every output-bearing state field.** Returning an error
status is not containment on its own: the base graph shapes its response as
`formatted_output OR result` with **no status check**, so a gate that merely raised — or that
returned an error without clearing — would still hand the caller the un-gated answer inside the
error envelope. `result`, `confirmation`, `record_id`, `record_ref`, `entry_id`, `account_title`,
`balance`, `freee_payload` and `intent` are all cleared, on the gate-violation path and the
pre-existing inner-workflow error path alike.

**No error envelope names the record, and none carries error text.** Both non-success returns go
through the one module-level `_contain(reason, new_errors=None)` helper: a constant reason code
(`freee_workflow_failed` / `output_withheld_by_gate` — the module's `ERROR_REASONS`) and nothing
else. Nothing read out of the record, nothing read out of `error_log`, none of the gate's own
violation entries. `record_id`/`record_ref` are this agent's **write evidence** — property 1 above
refuses a SUCCESS that lacks them — so returning them under an ERROR status would tell a caller
being informed of failure that a journal entry was nonetheless created or read, and which one;
freee is an accounting system, so the entry id, the account title and the balance are a customer's
bookkeeping. Omitting a field from one envelope is not clearing it: the clearing is what stops a
checkpoint or a downstream reader recovering it. The envelope is deliberately **truthy** — an
empty/falsy replacement would activate the `OR result` fallback it exists to prevent.

**The error reasons are not part of that envelope.** `error_log` is node-authored text: a reason
that interpolated the journal-entry number would put the record evidence back by another key, and
a reason that echoed the freee error body would ship unbounded third-party text — identifiers,
names, an arbitrary response body — and truncating it, stripping paths out of it or redacting
credential shapes from it is not a closed set. So it is not projected to the caller at all.
`error_log` stays the internal channel: the state reducer appends to it (post_process re-emits none
of the inner entries, which would duplicate every line), the audit trail needs it, and the
framework's own credential scan runs over every node result — which is why a gate violation names
the offending PATH only, and a credential-shaped mapping KEY is withheld from that label
(`<withheld>`) rather than quoted into `error_log`, where the scan would raise and replace the
cleared delta with a bare error. The reasons are still kept closed-set where they are written —
`CallFreeeApiNode` reports the HTTP **status** rather than the freee error body, and a fixed phrase
rather than the transport exception text, pinned by `TestErrorReasonsAreClosedSet` in
`tests/unit/test_call_freee_api_node.py` — for the audit trail's sake, not because they ship.

**Reachability of the error branch.** `AgentBaseGraph.route()` sends a terminal ERROR status to
`finalize`, so `post_process` is not on the compiled error path at all, and `BaseNode.__call__`
short-circuits on an incoming errored state before `execute()` runs. The containment above is
therefore **source-level defence in depth**, reachable by a direct `execute()` — not a live
end-to-end leak. `tests/proof_of_boundary/test_pb_output_containment.py` asserts the routing fact
itself, so a framework upgrade that ever routed ERROR through `post_process` fails a test rather
than silently promoting the branch to live.

**No rounding grid applies here.** Some templates in this family round monetary aggregates in an
external report to a fixed grid so raw line items cannot be reconstructed. That invariant is wrong
for this agent: the figures it reports are a specific journal amount or an account's closing
balance that the caller explicitly asked for, and rounding them would make the answer incorrect
for the bookkeeping purpose it exists to serve. The two properties above are the invariant this
template enforces instead.

## Audit

Every node's `execute()` emits exactly one positional
`emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its SUCCESS path (intent /
presence signals only — never request text, journal content, or credentials). `__call__()` is
never overridden. The output gate additionally emits its own event when it REFUSES: a block that
leaves no trace is indistinguishable from a request that was never made. Event names (documented
for operations):

| Node | Event |
|------|-------|
| pre_process | `pre_process_complete` |
| validate_input | `validate_input_complete` |
| classify_intent | `classify_intent_complete` |
| infer_freee_fields | `infer_freee_fields_complete` |
| call_freee_api | `call_freee_api_complete` |
| confirm | `confirm_complete` |
| post_process | `post_process_complete` |
| post_process (gate refusal) | `post_process_gate_blocked` |
| post_process (contained error return) | `post_process_error_contained` — closed-set reason code + error COUNT only, never record content |

## Implementation Note — LLM synthesis

Intent classification (`ClassifyIntentNode`) and field extraction (`InferFreeeFieldsNode`) each
have a **deterministic baseline that is always available and always correct on its own**: a keyword
heuristic for intent, regex/line-structure extraction for fields. An **optional** LLM pass (Azure
OpenAI, via `shared.services.llm.azure_openai_client.AzureOpenAIClient`) runs alongside each
baseline when the three secrets below are provisioned; any failure — missing secret, API error,
malformed/non-JSON response, an intent outside the closed 3-way set — is caught and silently
discarded, so the node always falls back to its deterministic result. The template still runs and
tests fully without a live LLM (every unit test either injects a test-double `llm=` or runs with
none configured, exercising the fallback path — no real network call anywhere in the suite).

The two nodes use the LLM asymmetrically, both times for financial-write safety, not by omission:

- **`ClassifyIntentNode`** — the LLM result **overrides** the heuristic when it returns a valid
  intent. Misclassifying intent alone cannot cause a write: `InferFreeeFieldsNode` still refuses to
  assemble journal lines unless the debit account, credit account, and a positive amount are ALL
  explicit, regardless of which path chose the intent.
- **`InferFreeeFieldsNode`** — the regex baseline stays authoritative on any conflict; the LLM only
  fills a field the regex found **nothing** for. This node's output can trigger a real external
  write, so a fuzzy/paraphrased LLM extraction is never allowed to silently replace an explicit,
  literal regex match — it only extends coverage to phrasings the regex cannot catch. Every
  LLM-supplied value (amount in particular) goes through the identical validation as a
  regex-extracted one (`finite_int_in_range`, the debit+credit+amount-all-explicit gate) before it
  can contribute to a booked entry — an LLM source never bypasses that gate.

**Secrets** (declared in `config/agent.yaml` `requires.secrets`, resolved via
`ctx.secrets.require(...)` inside each node's `execute()` — never `config/config.yaml`):
`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`. `AZURE_OPENAI_ENDPOINT`
must be the bare resource endpoint (`https://<resource>.services.ai.azure.com`, no `/openai` path
segment) — `AzureOpenAIClient.__init__` rejects a value containing one. `requires.extras: [openai]`
is also declared (the wheel install already includes this extra via `AGENTCORE_WHEEL_SPEC`).

Natural-language balance summaries (mentioned as a third follow-up candidate in an earlier revision
of this note) are **not** implemented — no existing node produces a summary today; adding one would
be a new capability, not a swap of an existing deterministic step, and was left out of this pass as
a separate scope decision.

## Limitation — freee client (documented)

`src/services/freee_client.py` ships a **deterministic, network-free stub** as its default
transport: it returns the documented freee accounting API response shapes (a `manual_journal`
object for lookups and creates — with a synthetic id echo derived from the request; a
`trial_bs.balances` list for balance checks) so the pipeline is runnable and testable without a
live freee company or the `requests` package. It does **not** perform a live freee call — the
principle is to document the limitation rather than fake the call.

To go live, inject real `post`/`get` transports at construction; the method contracts and payload
shapes follow the documented freee accounting REST API (`/api/1/manual_journals`,
`/api/1/reports/trial_bs`), so no business-logic change is required. Three go-live notes: a live
call requires the `company_id` query parameter (accepted by the client constructor, unused by the
stub); account titles are carried as labels, and resolving `account_item_id` via
`/api/1/account_items` is part of the live follow-up; and a live transport should set its own
socket timeout, so that the configured deadline cuts a hung connection rather than only discarding
a late result.

## Framework Utilization

### Shared Components Used
- [x] `InvocationContext` — read in `CallFreeeApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallFreeeApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("FREEE_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error) are not re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `FreeeWorkflowGraph` (`BaseGraph`) via `FreeeWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `FreeeWorkflowGraphNode._parent_config()` loads `config/config.yaml` and
  forwards `{freee, llm (if declared), timeout_s}` under `config["configurable"]` to the subgraph.
- **Caller-data forwarding**: `src/graph/context_bridge.py` (`extract_input()` stashes,
  `_extra_initial_state()` seeds) — the subgraph invocation does not carry `input_context`.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as `SubgraphError`;
  per-step `status=error` + `error_log` for API/validation failures (no silent pass).

**Conditional routing note.** `FreeeWorkflowGraph.route()` is annotated with this graph's OWN
`State`, not the framework base state. The graph engine reads a conditional path callable's
annotation as its input schema and projects away every field the annotation does not declare, so a
base-state annotation would hand the router a state with the domain fields missing — and a unit
suite that calls `route()` with a plain dict would never notice. The inner topology is linear
today, so `route()` is only reachable if `add_conditional_edges()` is wired to it; the annotation
is correct in advance rather than after the branch silently stops firing.

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-SDK import anywhere
- [x] `src/services/freee_client.py` and `src/services/security.py` have no framework imports
      (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat single main node | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; 5 domain steps live in the inner graph |
| LLM dependency | LLM client from the start | deterministic, LLM as documented follow-up | deterministic | template runs/tests without a live LLM; no dead prompt/config reads |
| freee client | live `requests` call | injectable transport + documented stub default | injectable + stub default | no live network in the shipped template; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + config forwarding via `_parent_config()` → `configurable` → state | no-arg nodes | nodes are no-arg (ctor args raise TypeError at graph build); the config files stay the single source |
| Caller data channel | inside the request text | validated `input_context` + ContextVar bridge | `input_context` + bridge | the text channel is PII-masked at every node boundary and corrupts real account titles |
| Write target | infer journal lines from text freely | explicit debit/credit/amount only; unresolved left empty | explicit only | never book a malformed or guessed journal entry; unresolved fields → status=error, not invented |
| Default intent | create_entry | lookup_entry | lookup_entry | low-confidence classification must never default to a write |
| Output invariant | fixed rounding grid on monetary values | record-evidence + credential containment | evidence + containment | the reported figures are exact bookkeeping values the caller asked for; rounding them would make the answer wrong |
