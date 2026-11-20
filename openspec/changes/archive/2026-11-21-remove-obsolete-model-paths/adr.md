# ADR Review Manifest

- Status: completed
- Review date: 2026-11-19

## Review Summary

ADR review completed for this change. Two narrow durable decisions diverge from in-force ADRs and were recorded as new repository-level records; accepted ADR files were not edited. Highest prior ID was 0045; new IDs are 0046 and 0047 (no collisions).

## In-Force ADRs Reviewed

Highest prior ID: 0045. Relevant records reviewed in the supersession graph:

- ADR-0040 (advisory cache, deliberate preference; notice clause now partially superseded by ADR-0046) as amended by ADR-0045 (list-button selection).
- ADR-0009 (native-first tool calling; alias compatibility clause now partially superseded by ADR-0047) as bounded by ADR-0035 (shared retry state, error-triggered JSON fallback removed).
- ADR-0042 (independent auxiliary/graph inference; already superseded the catalog-dependent graph chain, so graph cleanup needs no new record), ADR-0022 and ADR-0043 (context monitor, input-count compaction; unchanged, empty-response curl diagnostics are separate).
- ADR-0044 (retire model compatibility without conversion), ADR-0020/0034/0036/0041 (retained single-model and pinning rules), ADR-0019/0028 (XDG layout; only the retired path is dropped).

## New Durable ADRs Created

- `adr/0046-ignore-retired-combined-model-settings-file.md`
- `adr/0047-use-canonical-chat-methods-without-fallback-aliases.md`
