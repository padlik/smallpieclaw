# ADR Review Manifest

- Status: completed
- Review date: 2026-11-20

## Review Summary

ADR review completed for this change. All 47 prior non-companion ADRs were read and supersession links inspected. Highest prior sequence: 0047. Accepted records are immutable; none was edited.

## In-Force ADRs Reviewed

- Entirely superseded historical records: 0001, 0002, 0010, 0011, 0017, 0018, 0024.
- All other prior records remain in force in whole or in their unsuperseded portions: 0003–0009, 0012–0016, 0019–0023, 0025–0047.
- Relevant constraints: ADR-0005/0007 supervision/construction; ADR-0014/0019 atomic persistence/storage; ADR-0021 recovery; ADR-0023 lifecycle logging; ADR-0027/0033 confirmation/ownership; ADR-0035 logical retry state; ADR-0036/0041 immutable admission; ADR-0043 unchanged compaction; ADR-0044/0046 non-destructive legacy retirement.

## New Durable ADRs Created

- `adr/0048-limit-llm-recovery-to-actionable-interactive-failures.md`: partially supersedes ADR-0021's checkpoint/card eligibility and actionable visibility; retains useful durable recovery.
