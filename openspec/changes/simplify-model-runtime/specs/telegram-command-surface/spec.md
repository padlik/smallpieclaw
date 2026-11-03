# telegram-command-surface Delta

## Purpose

Baseline: canonical `openspec/specs/telegram-command-surface/spec.md` as already changed by the pending predecessor delta `openspec/changes/provider-model-catalog/specs/telegram-command-surface/spec.md` (implementation baseline `a57ab26`), which modifies `Public Telegram command discovery excludes removed and hidden commands` and adds the `/models` requirement modified below. This delta MUST be synchronized only after that predecessor delta. It makes `/models` an advisory listing over the disposable cache with deliberate persisted selection. Command discovery, help, the hidden `/compress` command, `/reset`, `/agents`, `/status`, `/stop`, `/prompts`, `/resume`, and `/jobs` contracts are unchanged; manual compression outcomes are specified in `per-model-context-window`.

## MODIFIED Requirements

### Requirement: `/models` lists, refreshes, and selects discovered models

Authorized `/models` SHALL list about 20 cached candidates simply, offer a refresh of every configured provider, and show per-provider fresh/stale status, the active selection, and whether it is saved or an unsaved automatic choice, including an active or saved selector that is not in the cache. Displayed capabilities and limits SHALL be labelled advisory and SHALL NOT be presented as budgets or as a promise of callability. Refresh SHALL update only the cache. Candidate buttons SHALL use opaque callbacks no longer than 64 bytes and reject expired or unknown tokens. The textual `/models select <connection-id>/<upstream-id>` command SHALL also accept deliberate selectors without cached candidates or an active Main selection; it SHALL validate only first-slash non-empty selector shape and the configured connection, never cache membership or upstream existence. Both selection routes SHALL atomically persist the preference before publishing the active selection or reporting success. When the preference is corrupt or invalid, `/models` SHALL say so without showing file content, and a deliberate selection by either route SHALL replace it without restart.

#### Scenario: `/models` presents a concise catalog
- **GIVEN** a cache contains about 20 candidates with fresh and stale providers
- **WHEN** an authorized user invokes `/models`
- **THEN** the list shows the candidates, provider freshness, and the active selection marked saved or automatic
- **AND** displayed metadata is labelled advisory
- **AND** no general pagination framework is introduced

#### Scenario: Uncached active selection is shown
- **GIVEN** the saved selection is not among the cached candidates
- **WHEN** an authorized user invokes `/models`
- **THEN** the selection is shown as active and saved without being marked unavailable

#### Scenario: Empty cache leaves commands usable
- **GIVEN** the cache has no candidates and every provider failed
- **WHEN** an authorized user invokes `/models`
- **THEN** the response states that no candidates are cached and offers refresh and `/models select <connection-id>/<upstream-id>` for direct selection
- **AND** unrelated commands remain available

#### Scenario: Textual selection recovers without candidates or main selection
- **GIVEN** the cache is empty, no Main model is active, and connection `openai-prod` is configured
- **WHEN** the authorized operator sends `/models select openai-prod/gpt-new`
- **THEN** the uncached selector is accepted without discovery or an upstream membership check
- **AND** `model-selection.toml` is atomically replaced before `openai-prod/gpt-new` becomes active and success is reported

#### Scenario: Textual selection rejects malformed or unknown connection
- **GIVEN** the preference and active selection have their previous values
- **WHEN** the authorized operator sends `/models select openai-prod/` or `/models select staging/gpt-x` with no `staging` connection
- **THEN** the command reports an actionable malformed-shape or unknown-connection error before provider I/O
- **AND** neither the preference nor active selection changes

#### Scenario: Textual selection recovers a corrupt preference
- **GIVEN** `model-selection.toml` is corrupt and the cache has no candidates
- **WHEN** the authorized operator sends `/models select openai-prod/gpt-x` with `openai-prod` configured
- **THEN** the corrupt preference is atomically replaced before the selection is published without restarting
- **AND** no cached candidate or active Main selection is required

#### Scenario: Refresh failure preserves stale inventory
- **GIVEN** a provider refresh fails
- **WHEN** `/models` displays the catalog
- **THEN** its last-good candidates remain selectable and marked stale
- **AND** the failure does not disable unrelated commands or change the active selection

#### Scenario: Callback payload does not include model identity
- **GIVEN** a canonical model ID is longer than Telegram's callback limit
- **WHEN** the selection button is rendered
- **THEN** it carries only an opaque token of at most 64 bytes
- **AND** the token resolves to the selector server-side

#### Scenario: Expired or removed selection token is rejected
- **GIVEN** a selection callback token expired or is unknown
- **WHEN** the callback is used
- **THEN** active selection and preference are unchanged
- **AND** the operator is asked to reopen `/models`
- **AND** an unexpired token whose candidate was later removed from the cache is not rejected for that reason

#### Scenario: Selection persistence fails
- **GIVEN** a candidate button or textual selector is chosen but the preference cannot be persisted
- **WHEN** selection is attempted
- **THEN** the old active selection remains
- **AND** prior preference bytes remain unchanged and no success is reported

#### Scenario: Selecting replaces a corrupt preference
- **GIVEN** `/models` reports the saved preference as corrupt
- **WHEN** the operator selects a candidate
- **THEN** the preference is atomically replaced and the selection is active without restart
