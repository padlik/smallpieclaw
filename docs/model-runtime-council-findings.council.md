# Council Notes: model-runtime-council-findings

## Author Summary

Three independent council seats examined runtime recovery, catalog complexity
and cross-system contracts. A council synthesis reconciled their source-review
findings into the accompanying report.

## Reviewer Challenges

- Alpha identified unusable non-retryable checkpoints, duplicate summary retry
  budgets and obsolete runtime plumbing.
- Beta grounded catalog removals in current specifications and challenged
  removing revisions, concurrency safeguards or compact selection rendering.
- Gamma identified headless retry waits and emphasized that durable resume
  preserves completed tool results; also raised runner cleanup and WAL risks.

## Resolutions

- Accepted: prioritize obsolete graph preparation, persistence, discovery
  descriptors and legacy model switching, subject to compatibility checks.
- Accepted: replace impossible or noninteractive recovery with safe failures.
- Accepted: recommend one-shot automatic summarization while preserving the
  current run-local disable semantics.
- Rejected: treating durable recovery as merely retrying one call or removing
  its ownership and anti-replay protections.
- Deferred: provenance removal, attempt-ledger redesign and adapter replacement
  pending focused contract and behavior validation.

## Remaining Risks

- No tests were executed; findings are source-review evidence only.
- Only beta reviewed canonical specifications and archived artifacts.
- Public helper consumers, fixture migration and persisted compatibility need
  checking before implementation.
- The two additional safety findings were not attributed to these features.

## Review Sessions

- Alpha: `ses_e0b9408e3ffe1S5Wxycs2X7QT6`
- Beta: `ses_e0b93e50bffeRMXEnsdbaVKl5L`
- Gamma: `ses_e0b93c2c9ffelbpseC2l5fnuFf`
- Synthesis: `ses_e0b8e7c22ffeDUKhHHMba8Ejn9`
