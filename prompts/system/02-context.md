---
section: context
order: 2
required: true
mode: all
variables:
  - memory
  - past_results
  - graph_context_section
  - strategies
---

{{models_section}}

PERSISTENT MEMORY (facts about this system):
{{memory}}

NOTE: Never store model names, providers, or API configuration in persistent memory — that
information is always injected fresh above and any memory entry about it will be stale.

RELEVANT PAST RESULTS:
{{past_results}}

{{graph_context_section}}

LEARNED STRATEGIES (preferred approaches for similar tasks):
{{strategies}}