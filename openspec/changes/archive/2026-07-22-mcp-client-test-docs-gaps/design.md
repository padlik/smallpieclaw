## Context

`mcp_client.py` is a 513-line SDK-based MCP transport layer with 55 existing tests. A coverage
audit found 6 gaps: one dead public method, untested error-propagation paths in `_session_runner`,
two untested pagination guards, three minor data-conversion edge cases, and an incomplete module
docstring with no config schema. The implementation is correct; the tests and docs have not kept
up.

No in-force ADRs (0001–0011) constrain this change. ADR-0008 established the builtin-tools facade
pattern, which is unrelated. MCP transport does not live in `builtin_tools/`.

## Goals / Non-Goals

**Goals:**
- Remove `MCPManager.last_error()` dead method
- Add ~10–12 tests that lock in existing correct behavior against future regression
- Fix the module docstring: three transports, full public API, config schema, `MCPManager` class
  threading model
- Update `openspec/specs/mcp-transport/spec.md` with missing Gherkin scenarios

**Non-Goals:**
- Behavior changes to `mcp_client.py`
- Integration tests against real MCP subprocesses
- Moving tests to a separate file
- Changing any other module

## Decisions

### D1: Append tests to `test_mcp_client.py`, not a new file

**Decision:** All new tests go into `tests/test_mcp_client.py`.

**Rationale:** Project convention is one test file per module. The file grows from 897 to ~1000
lines — manageable. Splitting would require grepping two files when debugging MCP issues.

**Alternative considered:** `test_mcp_client_error_paths.py` — rejected for reasons above.

### D2: Config schema in module docstring only

**Decision:** Document the server config dict schema inline in the `mcp_client.py` module header,
not in a separate file.

**Rationale:** The schema is consumed in the same file. Externalizing it creates a second source
of truth that drifts. Developers reading `mcp_client.py` find the answer immediately.

**Alternative considered:** `docs/mcp-config.md` — rejected (drift risk).

### D3: Gap 2 is a regression test, not a code fix

**Decision:** `list_tools()` and `initialize()` failure paths are tested as-is; no code changes.

**Rationale:** Source-confirmed: exceptions from `_run_session()` propagate to the outer
`except Exception` in `_session_runner` (line 235), which calls
`self._ready_future.set_exception(exc)`. `connect()` catches this and returns `connected=False`
promptly — no hang. Tests confirm the path works; they do not introduce a fix.

**Alternative considered:** Adding explicit `try/except` inside `_run_session` around
`list_tools()` — rejected (redundant; outer handler already covers it).

### D4: Dead code removal is task 1

**Decision:** `MCPManager.last_error()` is deleted before adding new tests.

**Rationale:** No test references the method (confirmed by grep). All `.last_error` references
in tests are attribute accesses on `_SdkClientWrapper`. Removing it first avoids any possibility
of the new tests inadvertently exercising a method that should be gone.

**Alternative considered:** Add `last_error` to `vulture_whitelist.py` to suppress the warning —
rejected. Dead code should be deleted, not hidden. A whitelist entry would preserve the naming
confusion between the `_SdkClientWrapper.last_error` attribute and the `MCPManager.last_error()`
method.

## Component Diagram

MCP client internals — annotated with test coverage state after this change:

```
  tests/test_mcp_client.py
  ┌────────────────────────────────────────────────────────────┐
  │  TestToolOutcome           ██████ fully covered             │
  │  TestSdkResultToOutcome    ███████░ + resource=None [NEW]   │
  │  TestSdkToolsToRegistry    ██████░ + desc truncation [NEW]  │
  │                                                             │
  │  TestSdkClientWrapper                                       │
  │    connect()           ████████ + init/list_tools fail [NEW]│
  │                               + unknown transport [NEW]     │
  │                               + _MAX_TOOL_PAGES [NEW]       │
  │    call_tool()         ████████ fully covered               │
  │    close()             ████████ fully covered               │
  │                                                             │
  │  TestMCPManager                                             │
  │    call_tool()         ████████ fully covered               │
  │    set_enabled()       ██████░ + already-connected [NEW]    │
  │    list_servers()      ██████░ + transport label [NEW]      │
  │    get_server_info()   ██████░ + off/error status [NEW]     │
  │    _start_loop()       ██████░ + idempotency [NEW]          │
  │    last_error()        ░░░░░░░ DELETED                      │
  └────────────────────────────────────────────────────────────┘
                │
                │ tests
                ▼
  mcp_client.py
  ┌────────────────────────────────────────────────────────────┐
  │  MCPManager                                                 │
  │  ├── _start_loop() / _stop_loop()  (daemon thread)         │
  │  ├── connect_all()                                         │
  │  ├── _connect_server()             ──┐                      │
  │  ├── call_tool()                     │ creates              │
  │  ├── get_tools()                     │                      │
  │  ├── set_enabled()                   │                      │
  │  ├── list_servers()                  │                      │
  │  ├── get_server_info()               │                      │
  │  ├── has_tool()                      │                      │
  │  ├── close_all()                     │                      │
  │  └── last_error()   ← DELETED ───────┘                     │
  │                                     │                      │
  │  _SdkClientWrapper  ←───────────────┘                      │
  │  ├── connect()                                             │
  │  │   └── _session_runner() [async]                         │
  │  │       ├── stdio_client / streamablehttp_client /        │
  │  │       │   sse_client                                    │
  │  │       └── _run_session() [async]                        │
  │  │           ├── session.initialize()   ← tested [NEW]     │
  │  │           └── session.list_tools()   ← tested [NEW]     │
  │  ├── call_tool()                                           │
  │  ├── close()                                               │
  │  └── _drain_queue()                                        │
  │                                                             │
  │  Helpers                                                    │
  │  ├── _tool_outcome()                                        │
  │  ├── _sdk_result_to_outcome()   ← resource=None [NEW]      │
  │  └── _sdk_tools_to_registry()  ← desc truncation [NEW]     │
  └────────────────────────────────────────────────────────────┘
                │
                │ SDK calls
                ▼
  mcp Python SDK (ClientSession, stdio_client, streamablehttp_client, sse_client)
```

**Diagram notes:**
- This is a component-level view of one container (`mcp_client.py`).
- Context and container levels add no value here — the MCP client is a single bounded module.
- `[NEW]` marks paths added by this change. Everything else is unchanged.

## Risks / Trade-offs

- **`_session_runner` outer handler is a wide catch** → Mitigation: new tests confirm specific
  exception types reach the ready future. The handler is intentionally wide for daemon resilience
  (documented project convention).
- **File size ~1000 lines** → Mitigation: acceptable by project convention; flagged during
  grilling. Future refactor (split file) is a separate decision.
- **Docstring drift** → Mitigation: the config schema is the only free-form section; it mirrors
  code that rarely changes. No automated sync needed for this scope.

## Migration Plan

1. Delete `MCPManager.last_error()` — no callers, no migration needed.
2. Add tests to `test_mcp_client.py` — `make test` confirms green.
3. Update module docstring and `MCPManager` class docstring — no runtime effect.
4. Update `openspec/specs/mcp-transport/spec.md` — planning artifact only.
5. Run `make check` (ruff + vulture + pytest) — after deleting the method, confirm vulture
   produces no new warnings. The `_SdkClientWrapper.last_error` attribute shares the name, so
   vulture's global name-matching should suppress any warning; check `vulture_whitelist.py` if
   an unexpected warning does appear.

**Rollback:** Any change is trivially reversible via git revert. No data, no migration, no
deployment needed.

## Open Questions

- None. All decisions resolved during explore + grilling phases.
