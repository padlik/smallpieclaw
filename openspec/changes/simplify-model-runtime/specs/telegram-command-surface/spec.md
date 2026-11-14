# telegram-command-surface Delta

## Purpose

Baseline: canonical `openspec/specs/telegram-command-surface/spec.md` as already changed by the pending predecessor delta `openspec/changes/provider-model-catalog/specs/telegram-command-surface/spec.md` (implementation baseline `a57ab26`), which modifies `Public Telegram command discovery excludes removed and hidden commands` and adds the `/models` requirement modified below. This delta MUST be synchronized only after that predecessor delta. It makes `/models` an advisory listing over the disposable cache with deliberate persisted selection. Command discovery, help, the hidden `/compress` command, `/reset`, `/agents`, `/status`, `/stop`, `/prompts`, `/resume`, and `/jobs` contracts are unchanged; manual compression outcomes are specified in `per-model-context-window`.

## MODIFIED Requirements

### Requirement: `/models` lists, refreshes, and selects discovered models

Authorized `/models` SHALL list about 20 cached candidates simply, offer a refresh of every configured provider, and show per-provider fresh/stale status, the active selection, and whether it is saved or an unsaved automatic choice, including an active or saved selector that is not in the cache. Displayed capabilities and limits SHALL be labelled advisory and SHALL NOT be presented as budgets or as a promise of callability. Refresh SHALL update only the cache. Candidate buttons SHALL use opaque callbacks no longer than 64 bytes and reject expired or unknown tokens. Selection SHALL be made only through these candidate buttons; no typed `/models` selection command SHALL exist, and `/models` arguments SHALL NOT select a model. A button selection SHALL need no active Main selection and SHALL validate only the captured selector's non-empty first-slash shape and configured connection, never cache membership or upstream existence; an unexpired token SHALL NOT be rejected merely because a refresh removed its candidate from the cache. It SHALL atomically persist the preference before publishing the active selection or reporting success. When the preference is corrupt or invalid, `/models` SHALL say so without showing file content, SHALL NOT fall back to an automatic selection, and a deliberate button selection SHALL replace the preference without restart; with an empty cache the operator refreshes until candidates exist (or waits for providers to recover) before selecting, and an existing valid saved selector remains active and displayed whether cached or not.

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
- **THEN** the response states that no candidates are cached and offers refresh only; it names no typed selection command
- **AND** unrelated commands remain available

#### Scenario: Typed select text is not a selection route
- **GIVEN** a saved active selection with known preference bytes, or an automatic active selection with no preference file
- **WHEN** the authorized operator sends `/models select openai-prod/gpt-new`
- **THEN** the catalog is shown as for `/models` and no selection is made
- **AND** neither the preference (bytes or absence) nor the active selection change

#### Scenario: Corrupt preference with an empty cache recovers after a successful refresh
- **GIVEN** `model-selection.toml` is corrupt, the cache has no candidates, and no automatic selection was made
- **WHEN** the operator refreshes and a provider returns candidates, then selects one with its button
- **THEN** the corrupt preference is atomically replaced before the selection is published and active without a restart
- **AND** until then `/models` reports the corrupt preference without file content, offers refresh only, and refresh updates only the cache and never writes or repairs the preference

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

#### Scenario: Expired or unknown selection token is rejected
- **GIVEN** a selection callback token expired or is unknown
- **WHEN** the callback is used
- **THEN** active selection and preference are unchanged
- **AND** the operator is asked to reopen `/models`
- **AND** an unexpired token whose candidate was later removed from the cache is not rejected for that reason

#### Scenario: Selection persistence fails
- **GIVEN** a candidate button is chosen but the preference cannot be persisted
- **WHEN** selection is attempted
- **THEN** the old active selection remains
- **AND** prior preference bytes remain unchanged and no success is reported

#### Scenario: Selecting replaces a corrupt preference
- **GIVEN** `/models` reports the saved preference as corrupt
- **WHEN** the operator selects a candidate
- **THEN** the preference is atomically replaced and the selection is active without restart
