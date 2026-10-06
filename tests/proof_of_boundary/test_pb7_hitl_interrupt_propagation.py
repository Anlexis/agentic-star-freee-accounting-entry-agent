# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# PB-7 (CONDITIONAL): required only when config/agent.yaml sets
# hitl.enabled: true. CMN-C2-277 is a non-HITL template (no node calls
# interrupt(); config/agent.yaml declares no hitl block), so PB-7 is
# **Auto-waived - non-HITL** per docs/03_test_spec.md and both cases below
# SKIP via the module-level skipif.
#
# Canonical skip-stub form: the bodies are REAL AssertionErrors (never
# `assert True`), so if hitl.enabled is ever flipped on without implementing
# the PB-7 cases, the suite fails loudly instead of silently passing.
#
# Reference: the HITL compliance pattern (hitl_allowed guard) — the stub is
# conditional on this template declaring HITL.

from __future__ import annotations

import pathlib

import pytest

# ---------------------------------------------------------------------------
# Conditional skip - only runs when config/agent.yaml has hitl.enabled: true
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/agent.yaml declares hitl.enabled: true."""
    if not _CONFIG_PATH.exists():
        return False
    try:
        import yaml  # pyyaml - transitive dep of the framework wheel

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception:
        return False
    hitl = (data or {}).get("hitl", {})
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/agent.yaml does not set hitl.enabled: true - PB-7 auto-waived (non-HITL)",
)


def test_pb7_hitl_interrupt_propagates() -> None:
    """PB-7-A: interrupt() raises GraphInterrupt and propagates (not caught by
    the app error boundary); status is NOT set to error.

    CMN-C2-277 has no interrupting node. If hitl.enabled is turned on, this
    stub FAILS (real AssertionError) until the case is implemented: invoke the
    interrupting node via node(state) (never execute()) with hitl_allowed=True
    and assert `with pytest.raises(GraphInterrupt)`.
    """
    raise AssertionError(
        "hitl.enabled is true but PB-7-A is not implemented: add the interrupting "
        "node, drive it via node(state), and assert GraphInterrupt propagates."
    )


def test_pb7_hitl_allowed_false_skips_interrupt() -> None:
    """PB-7-B: the hitl_allowed=False guard prevents interrupt() from firing -
    no GraphInterrupt, no deadlock, and a well-formed result is returned.

    CMN-C2-277 has no interrupting node. If hitl.enabled is turned on, this
    stub FAILS (real AssertionError) until the case is implemented: same
    trigger condition as PB-7-A but hitl_allowed=False; assert node(state)
    returns without raising and carries the expected result field.
    """
    raise AssertionError(
        "hitl.enabled is true but PB-7-B is not implemented: drive the interrupting "
        "node via node(state) with hitl_allowed=False and assert no GraphInterrupt."
    )
