# Use TOML for agent-scoped vault files

## Status

Accepted, supersedes ADR-0002

## Date

2026-07-02

## Supersedes

ADR-0002: Use agent-scoped vault for centralized secret storage

## Context and Problem Statement

ADR-0002 introduced a single agent-scoped vault referenced through `sec:` keys, but specified a JSON file. JSON requires quoted property names, which makes vault entries look like string literals rather than clear configuration keys. The vault format is new and has not had an official deployment, so there is no compatibility requirement to keep JSON support.

The vault should remain simple, readable, and dependency-light, and the project should avoid custom parsers for configuration formats.

## Considered Options

- **Keep JSON** — Uses the standard library, but requires quoted keys and does not meet the desired operator experience.
- **Use JSON5** — Allows unquoted keys, but adds a parser dependency and a larger syntax surface than needed.
- **Use a custom relaxed JSON parser** — Can support exactly the desired syntax, but adds maintenance and correctness risk.
- **Use TOML** — Supports visible unquoted keys natively, matches the main configuration style, and uses standard `tomllib` / existing `tomli` support.

## Decision Outcome

Chosen option: "Use TOML", because it provides human-friendly visible keys without a custom parser or new broad configuration language. The vault file is a flat TOML table where every top-level key maps to a string value.

Example:

```toml
OPENAI_API_KEY = "sk-..."
BOT_TOKEN      = "123456:ABC"
OLLAMA_HOST    = "http://localhost:11434"
```

### Consequences

- Good, because operators manage secrets in one TOML file instead of many `*_file` references.
- Good, because keys are visible as configuration keys without quotes.
- Good, because the implementation uses a real TOML parser instead of a custom parser.
- Good, because the vault format matches the rest of the project's TOML configuration.
- Bad, because JSON vault files are not supported.
- Bad, because all values must be TOML strings; nested tables, arrays, numbers, and booleans are rejected.
