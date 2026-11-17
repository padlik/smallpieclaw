"""Documentation and example validation for task 11.1.

The shipped ``README.md``, ``config.toml.example`` and ``scheduler.toml.example``
must describe the current model/runtime behaviour and their TOML examples must
load through the production parsers. These tests check facts that have a
production source of truth (parsers, allowed fields) and that retired wording
is gone; they do not freeze prose beyond a small set of required topic markers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

try:
    import tomli as _toml  # type: ignore[import]
except ImportError:
    import tomllib as _toml  # type: ignore[no-redef]  # Python 3.11+

import scheduler
from config_schema import AgentConfig, ConfigError, parse_config

_ROOT = Path(__file__).resolve().parent.parent
_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_CONFIG_EXAMPLE = (_ROOT / "config.toml.example").read_text(encoding="utf-8")
_SCHEDULER_EXAMPLE = _ROOT / "scheduler.toml.example"

_CONNECTION_FIELDS = {"type", "api_key", "base_url", "request_timeout"}
_JOB_FIELDS = {
    "enabled", "schedule", "cron", "run_at", "task", "notify", "model",
    "preserve_context", "context_max_messages", "overlap_policy",
}
# Wording of retired behaviour that must not survive in any shipped document.
_STALE = (
    "85%",
    "85 %",
    "not changed by this step",
    "Not yet changed (tasks 6.x)",
    "can reject requests locally",
    "later context-policy change",
    "/models select",
    "override background_model",
    "Without an injected catalog",
    "provider model catalog is injected",
    "never changes those old files",
    "tasks and sub-agents are refused",
)
# Topic markers each current behaviour must keep in the README (whitespace-normalised).
_README_TOPICS = (
    "Selection is by buttons only",
    "Refresh first",
    "no typed selection command",
    "active but unsaved",
    "Upgrading manually from the old model configuration",
    "Rollback",
    "context-length rejection",
    "never read, parsed, converted",
)


def _normalised(text: str) -> str:
    """Collapse whitespace so wrapped prose compares equal."""
    return " ".join(text.split())


def _readme_toml_blocks() -> list[dict[str, Any]]:
    """Every fenced ``toml`` block of the README, parsed as plain TOML."""
    blocks = re.findall(r"```toml\n(.*?)```", _README, flags=re.DOTALL)
    return [_toml.loads(block) for block in blocks]


@pytest.mark.parametrize("stale", _STALE)
def test_no_retired_wording_in_shipped_documents(stale: str) -> None:
    """Retired budget/typed-selection/background-model wording is gone everywhere."""
    for name, text in (
        ("README.md", _README),
        ("config.toml.example", _CONFIG_EXAMPLE),
        ("scheduler.toml.example", _SCHEDULER_EXAMPLE.read_text(encoding="utf-8")),
    ):
        assert stale not in _normalised(text), f"{name} still contains {stale!r}"


@pytest.mark.parametrize("topic", _README_TOPICS)
def test_readme_documents_each_current_topic(topic: str) -> None:
    """The README keeps a marker for every behaviour task 11.1 documents."""
    assert topic in _normalised(_README)


def test_readme_toml_examples_use_only_current_fields() -> None:
    """README provider, embedding, extraction and job examples match the production schema."""
    blocks = _readme_toml_blocks()
    providers = [b["providers"] for b in blocks if "providers" in b]
    assert providers, "README shows no [providers.<id>] example"
    for table in providers:
        for connection in table.values():
            assert set(connection) <= _CONNECTION_FIELDS
    embeddings = [b["embeddings"] for b in blocks if "embeddings" in b]
    assert embeddings and all(set(e) == {"provider", "base_url", "api_key", "model"} for e in embeddings)
    graph = [b["graph_memory"] for b in blocks if "graph_memory" in b]
    assert graph and all(
        {"extraction_provider", "extraction_base_url", "extraction_api_key", "extraction_model"} <= set(g)
        for g in graph
    )
    for block in blocks:
        for tag, job in block.get("jobs", {}).items():
            assert set(job) <= _JOB_FIELDS, tag
            assert scheduler._classify_schedule(job)[3] is None, tag
    agent_keys = {key for block in blocks for key in block.get("agent", {})}
    assert "compaction_threshold_tokens" in agent_keys
    assert agent_keys <= set(AgentConfig.__dataclass_fields__) | {"required_output_cap_tokens"}


def test_readme_command_table_keeps_compress_hidden_and_stop_listed() -> None:
    """``/compress`` stays a hidden diagnostic, ``/stop`` stays a normal command."""
    rows = re.findall(r"^\| `(/[a-z_]+)[^`]*` \|", _README, flags=re.MULTILINE)
    assert "/compress" not in rows and "/stop" in rows
    assert "`/show_ctx`, `/show_env`, `/memory`, `/compress`" in _README


def test_readme_models_row_describes_button_selection() -> None:
    """The ``/models`` command row names Refresh and button selection, not a typed switch."""
    row = next(line for line in _README.splitlines() if line.startswith("| `/models` |"))
    assert "Refresh" in row and "button" in row.lower()


def test_scheduler_example_parses_and_every_job_is_current() -> None:
    """The scheduler example loads through the production reader with no config errors."""
    result = scheduler._read_scheduler_file(str(_SCHEDULER_EXAMPLE))
    assert result.status == "valid" and result.document is not None
    jobs = scheduler._job_tables(result.document)
    assert jobs
    for tag, job in jobs.items():
        assert set(job) <= _JOB_FIELDS, tag
        assert scheduler._classify_schedule(job)[3] is None, tag


def test_scheduler_example_documents_strict_selectors_and_inert_json() -> None:
    """The scheduler example explains explicit selectors and the inert legacy job file."""
    text = _normalised(_SCHEDULER_EXAMPLE.read_text(encoding="utf-8"))
    assert "<connection-id>/<upstream-id>" in text
    assert "inherit" in text and "never falls back" in text
    assert "scheduler_jobs.json" in text and "never read" in text


def test_config_example_documents_selection_and_inert_legacy_files(tmp_path: Path) -> None:
    """The config example explains button selection and inert files and still parses."""
    text = _normalised(_CONFIG_EXAMPLE)
    for marker in ("buttons only", "model-selection.toml", "models-config.toml", "scheduler_jobs.json"):
        assert marker in text, marker
    vault = tmp_path / "secrets.toml"
    vault.write_text('TELEGRAM_BOT_TOKEN = "example-test-token"\n', encoding="utf-8")
    cfg = parse_config(_toml.loads(_CONFIG_EXAMPLE), vault_file=str(vault), agent_name="piclaw")
    assert cfg.agent.compaction_threshold_tokens == 60000
    assert cfg.agent.required_output_cap_tokens == 1024


# ---------------------------------------------------------------------------
# Semantic distinctions (Oracle FAIL follow-up): each claim below is tied to a
# production function that executes it or to a canonical production test that
# must still exist. They are documentation regressions, not a behavioural proof.
# ---------------------------------------------------------------------------

def _section(heading: str) -> str:
    """README text from *heading* to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = _README.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = re.match(r"^(#+) ", lines[i])
        if match and len(match.group(1)) <= level:
            end = i
            break
    return "\n".join(lines[start:end])


def _defines(test_file: str, name: str) -> bool:
    """True when the canonical production test *name* is still defined in *test_file*."""
    return f"def {name}(" in (_ROOT / "tests" / test_file).read_text(encoding="utf-8")


def test_startup_errors_are_chat_config_and_job_errors_are_contained(minimal_agent_raw: dict[str, Any]) -> None:
    """``config.toml`` obsolete keys stop startup; scheduler job fields never do."""
    startup = _normalised(_section("#### `config.toml`: startup errors"))
    jobs = _normalised(_section("#### `scheduler.toml`: contained per job"))
    assert not re.search(r"\b(daily|interval|hours|minutes|scheduler\.toml)\b", startup)
    assert "do not stop the daemon" in jobs and "every other job keeps running" in jobs
    assert "config_error" in jobs and "stops startup" not in jobs and "fails at startup" not in jobs
    # Production: an obsolete [agent] key raises at config parse (startup) ...
    minimal_agent_raw["agent"]["fallback_models"] = ["x"]
    with pytest.raises(ConfigError, match=r"agent\.fallback_models"):
        parse_config(minimal_agent_raw)
    # ... whereas an obsolete job field is only a per-job config_error, never an exception.
    for field in ({"schedule": "daily", "time": "09:00"}, {"schedule": "interval", "hours": 2}):
        assert scheduler._classify_schedule(dict(field))[3] is not None
    assert _defines("test_obsolete_scheduler_fields.py", "test_obsolete_scheduling_job_does_not_stop_siblings_from_scheduling")
    assert _defines("test_config_schema.py", "test_agent_key_is_rejected_by_presence_with_replacement")


def test_migration_steps_remove_legacy_time_fields_because_leftovers_invalidate_the_job() -> None:
    """Converting to cron must delete ``time``/``hours``/``minutes``; production rejects leftovers."""
    step = _normalised(_section("### Upgrading manually from the old model configuration"))
    assert "delete the old `time`, `hours` and `minutes` fields" in step
    assert "even next to `cron`" in step
    clean = {"schedule": "cron", "cron": "0 9 * * *"}
    assert scheduler._classify_schedule(dict(clean))[3] is None
    for leftover in ({"time": "09:00"}, {"hours": 2}, {"minutes": 5}):
        assert scheduler._classify_schedule({**clean, **leftover})[3] is not None, leftover


def test_explicit_selectors_need_no_main_but_inheriting_requests_do() -> None:
    """The README restricts the no-Main refusal to requests that inherit the Main selection."""
    text = _normalised(_README)
    assert "tasks and sub-agents are refused until" not in text
    assert "an explicit `<connection-id>/<upstream-id>` selector still runs without an active Main, even with an invalid preference file" in text
    assert "Only requests that omit the selector inherit the active Main selection and are refused while there is none" in text
    assert "ordinary tasks and inheriting requests are refused" in text and "explicit selectors are unaffected" in text
    for test_file, name in (
        ("test_run_pin_root_4_2.py", "test_explicit_uncached_target_runs_without_main_for_plan_and_diagnostic"),
        ("test_run_pin_root_4_2.py", "test_implicit_requests_without_main_fail_locally_in_every_scope"),
        ("test_model_registry.py", "test_explicit_selector_is_independent_of_corrupt_preference_and_never_touches_it"),
        ("test_profile_admission_matrix.py", "test_spawn_explicit_b_without_main_completes_on_its_own_pin"),
    ):
        assert _defines(test_file, name), name


def test_rollback_untouched_claim_names_only_the_retired_files() -> None:
    """Rollback does not claim ``scheduler.toml`` is untouched: normal job changes rewrite it."""
    rollback = _normalised(_section("### Upgrading manually from the old model configuration").split("**Rollback:**", 1)[1])
    assert "does not read, change or delete the retired `models-config.toml` and `scheduler_jobs.json`" in rollback
    assert "still rewrites `scheduler.toml`" in rollback


@pytest.fixture
def minimal_agent_raw() -> dict[str, Any]:
    """A raw config that parses as-is, so one obsolete key is the only failure."""
    return {
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1], "security_mode": "allowlist"},
        "agent": {"compaction_threshold_tokens": 60000},
        "providers": {"openai-main": {"type": "openai", "api_key": "fake-key"}},
        "embeddings": {"provider": "openai", "base_url": "https://embed.example.test/v1", "api_key": "fake", "model": "m"},
        "paths": {},
    }
