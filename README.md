# freee Accounting Entry Agent

AI agent for posting accounting entries in freee, built with Agentic Star.

> **Category**: Cat 2 (domain-specific multi-step pipeline)
> **Industry**: Common
> **Template ID**: CMN-C2-277

## Overview

Turns a plain-language bookkeeping request into a journal-entry action against the
[freee](https://www.freee.co.jp/) accounting API: it looks up an existing journal entry, books a
new one, or reports an account's closing balance, and returns a confirmation naming the record it
touched. Requests arrive as free text ("Look up journal entry number 4021", "記帳: 旅費交通費
5,000円"), optionally with structured fields supplied alongside them, and the pipeline classifies
the intent, extracts the journal fields, assembles the API request body, calls freee, and formats
the confirmation.

Two safety properties are built in rather than bolted on. **Nothing is invented**: a journal entry
is only booked when the debit account, the credit account and a positive amount are all explicit,
and an unresolved entry number is left empty and reported rather than guessed. **A vague request
never writes**: low-confidence classification falls back to the read-only lookup.

The bundled freee transport is a deterministic, network-free stub, so the pipeline runs and tests
end-to-end out of the box without a live freee company. A real deployment injects `post` / `get`
transports at client construction; the request and response shapes already follow the documented
freee endpoints, so no pipeline change is needed.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent
fails at graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design specification and test specification
```

See [`docs/02_design.md`](docs/02_design.md) for the architecture, the caller-data contract
and the output contract, and [`docs/03_test_spec.md`](docs/03_test_spec.md) for what the test
suite covers and why.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

