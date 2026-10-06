"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import FreeeAccountingEntryAgent

app = FastAPI(title="Agent")


def _load_runtime_config() -> "dict[str, Any]":
    """Load config/config.yaml — the runtime parameters (max_retry, timeout_s, freee).

    The graph consumes these through its constructor (the framework run loop
    reads max_retry from the graph config); a missing or unreadable file
    degrades to the framework defaults rather than failing the boot.
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


agent = FreeeAccountingEntryAgent(config=_load_runtime_config())
agent.compile()
# namespace/agent_name match the manifest's `namespace` / `name` values.
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="FreeeAccountingEntryAgent"))


# Adapter-level size cap on the caller-supplied context (bytes of its JSON
# serialization). Field-level validation happens in the pre_process node; this
# cap only stops oversized envelopes at the door.
_INPUT_CONTEXT_MAX_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Optional caller-supplied context: an explicit target journal entry
    # (entry_id / entry_hint / journal_id) and/or structured journal fields
    # (journal.account / debit / credit / amount / issue_date). Validated
    # field-by-field by the pre_process node — malformed values are refused
    # without being echoed.
    input_context: "dict[str, Any]" = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> "dict[str, Any]":
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    if len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8")) > _INPUT_CONTEXT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="input_context too large.")
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # the platform auth middleware) — a deployment-level caller credential,
    # not an agent secret, so ctx.secrets does not apply (no InvocationContext
    # exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return cast("dict[str, Any]", agent.invoke(req.input, ctx=ctx, input_context=req.input_context))


@app.get("/health")
def health() -> "dict[str, str]":
    return {"status": "ok", "agent": "FreeeAccountingEntryAgent"}
