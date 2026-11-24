# ADR Review Manifest

- Status: completed
- Review date: 2026-11-24

## Review Summary

ADR review completed for this change. All 48 existing numbered ADRs were inspected, including their supersession declarations; highest existing number was 0048. The user selected the repository's concise ADR format for this proposal only; no global skill preferences were changed.

## In-Force ADRs Reviewed

Complete pre-change in-force inventory (partial supersessions retain only their unsuperseded clauses): ADR-0003, ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0009, ADR-0012, ADR-0013, ADR-0014, ADR-0015, ADR-0016, ADR-0019, ADR-0020, ADR-0021, ADR-0022, ADR-0023, ADR-0025, ADR-0026, ADR-0027, ADR-0028, ADR-0029, ADR-0030, ADR-0031, ADR-0032, ADR-0033, ADR-0034, ADR-0035, ADR-0036, ADR-0037, ADR-0038, ADR-0039, ADR-0040, ADR-0041, ADR-0042, ADR-0043, ADR-0044, ADR-0045, ADR-0046, ADR-0047 and ADR-0048.

Relevant constraints within that inventory:

- ADR-0021, as partially superseded by ADR-0048: atomic durable recovery and remaining live-card lifecycle.
- ADR-0048: explicit eligibility and safe terminal failures.
- ADR-0035: one logical-request attempt state, no refill or completed-tool replay.
- ADR-0041 and remaining ADR-0036 construction/ownership constraints: single admission and immutable per-execution target; no catalog authority or model fallback.
- ADR-0033: fresh execution/turn ownership independent of trace reuse.
- ADR-0027: standing-consent ledger and sensitive-tool boundaries.
- Remaining native protocol surface of ADR-0009 as partially superseded by ADR-0035/0047: existing native-first serialization and local conversion.
- ADR-0019/0028: storage placement; unchanged.

ADR-0024 is fully superseded by ADR-0027, not a live constraint. Other superseded historical model/graph policies are not reinstated. Unrelated in-force decisions remain unchanged.

## New Durable ADRs Created

- `adr/0049-use-durable-recovery-with-active-model-readmission.md`: durable recovery as an explicit newly admitted execution; partial supersession of ADR-0021 and ADR-0048 only.
