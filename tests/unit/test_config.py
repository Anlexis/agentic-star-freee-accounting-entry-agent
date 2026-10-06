# CMN-C2-277 - Unit tests: manifest + runtime config sanity.
#
# Two files, two jobs:
#   config/agent.yaml  - the FLAT registry manifest (every key at root level;
#                        no `agent:` block). Identity, entry point, trust and
#                        the compile-time `requires` gates live here.
#   config/config.yaml - the runtime parameters the graph is constructed with
#                        (max_retry, timeout_s) plus the `freee` integration
#                        section forwarded to the inner graph.
#
# The freee integration token is read with ctx.secrets.get() (optional) and
# never ctx.secrets.require(), and the default transport runs without a
# credential — declaring FREEE_TOKEN here would fail the agent at compile time
# wherever a live transport + provisioned token don't arrive together, so it
# must stay out of `requires.secrets`. The three Azure OpenAI secrets ARE
# ctx.secrets.require()d (ClassifyIntentNode / InferFreeeFieldsNode's optional
# LLM enhancement, docs/02_design.md "Implementation Note — LLM synthesis"),
# so they belong in `requires.secrets` — any failure there degrades to the
# deterministic baseline rather than raising, so declaring them does not risk
# an unprovisioned-secret compile failure the way FREEE_TOKEN would.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_manifest_identity():
    data = _load(_MANIFEST_PATH)
    assert data["id"] == "CMN-C2-277"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["namespace"] == "cmn"


def test_manifest_is_flat():
    """No nested `agent:` block: the registry reads every key at root level."""
    data = _load(_MANIFEST_PATH)
    assert "agent" not in data


def test_manifest_entry_point():
    data = _load(_MANIFEST_PATH)
    assert data["class"] == "src.graph.graph.FreeeAccountingEntryAgent"


def test_manifest_security():
    data = _load(_MANIFEST_PATH)
    # Agent-level entry trust, enforced by the outer backbone pre_process gate;
    # inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_only_the_llm_secrets():
    """`requires.secrets` names exactly the optional-LLM Azure OpenAI keys.

    FREEE_TOKEN must NOT appear here — it is read with ctx.secrets.get()
    (optional) and never ctx.secrets.require(); the default transport runs
    without a credential. The three Azure OpenAI secrets ARE
    ctx.secrets.require()d by ClassifyIntentNode / InferFreeeFieldsNode's
    optional LLM enhancement — any failure there degrades to the
    deterministic heuristic/regex baseline rather than raising.
    """
    data = _load(_MANIFEST_PATH)
    assert set(data["requires"]["secrets"]) == {
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    }
    assert data["requires"]["extras"] == ["openai"]


def test_runtime_config_freee_integration_section():
    data = _load(_RUNTIME_PATH)
    # Forwarded to the inner graph by FreeeWorkflowGraphNode._parent_config().
    assert data["freee"]["base_url"] == "https://api.freee.co.jp/api/1"
    # Live freee endpoints need company_id (query param); declared, blank until go-live.
    assert "company_id" in data["freee"]


def test_runtime_config_scalars():
    data = _load(_RUNTIME_PATH)
    assert isinstance(data["max_retry"], int)
    assert isinstance(data["timeout_s"], int)
