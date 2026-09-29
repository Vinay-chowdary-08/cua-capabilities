# Contributing / project conventions

- Python 3.11+, full type hints, Pydantic v2 for data crossing module boundaries.
- Never log raw input values, page text, or screenshots without `cua.safety.redact`.
- The `cua.replay` package must never import `cua.agent` or any LLM client.
- Surface-specific code lives only in `cua/surface/*`. Replay and artifact code talk to the Surface protocol.
- No blind sleeps for waiting; wait on conditions with explicit timeouts.
- Every new module gets pytest tests. Prefer small functions and explicit errors.
- Use enums / `Literal` types for statuses, never free strings.
