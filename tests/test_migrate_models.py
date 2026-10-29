"""Preview-first legacy ``[[models]]`` migration (task 10.1).

Every decision of the ratified migration table is exercised through the pure
preview API and the ``main(argv)`` CLI. Inputs are local TOML text or files
under ``tmp_path``; nothing is resolved, sent or written.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

try:
    import tomli as _toml  # type: ignore[import]
except ImportError:
    import tomllib as _toml  # type: ignore[no-redef]  # Python 3.11+

import migrate_models
from migrate_models import (
    collect_connections,
    main,
    map_entry,
    preview_migration,
    render_preview,
    resolve_effective_default,
)

_TUNING_MARKER = "QZX-tuning-value-7731"
_LITERAL_KEY = "sk-literalsecret0123456789"

_UNRELATED_TAIL = """
[scheduler]
enabled = true
timezone = "UTC"   # operator comment

[shell]
allowed_commands = [
  "ls",
  "cat",
]

[nsjail]
enabled = false
mounts = { tmp = "/tmp", data = "/srv/data" }

[embeddings]
provider = "openai"
model = "text-embedding-3-small"
api_key = "env:EMBED_KEY"
"""


def _entry(
    name: str,
    *,
    provider: str = "openai",
    api_key: str | None = None,
    base_url: str | None = '"https://api.openai.com/v1"',
    model: str | None = "gpt-x",
    extra: str = "",
) -> str:
    """One legacy ``[[models]]`` table (values are TOML literals)."""
    lines = ["[[models]]", f'name = "{name}"', f'provider = "{provider}"']
    if api_key is not None:
        lines.append(f"api_key = {api_key}")
    if base_url is not None:
        lines.append(f"base_url = {base_url}")
    if model is not None:
        lines.append(f'model = "{model}"')
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n\n"


def _config(*entries: str, agent: str = "") -> str:
    """A legacy config: unrelated sections around the ``[[models]]`` tables."""
    return (
        '[telegram]\nbot_token = "env:TELEGRAM_TOKEN"\nallowed_user_ids = [1, 2]\n\n'
        + "".join(entries)
        + f"[agent]\nmax_iterations = 12\nstep_notify_interval = 3\n{agent}\n"
        + _UNRELATED_TAIL
    )


def _candidate(preview: migrate_models.MigrationPreview) -> dict:
    """Parse the candidate configuration text independently of the module."""
    assert preview.candidate_config_text is not None
    return _toml.loads(preview.candidate_config_text)


def _kinds(preview: migrate_models.MigrationPreview) -> list[str]:
    """Kinds of the stopped operator choices."""
    return [stop["kind"] for stop in preview.stopped_choices]


# ---------------------------------------------------------------------------
# Connection mapping
# ---------------------------------------------------------------------------

def test_entry_maps_to_one_provider_connection() -> None:
    """An entry becomes one ``[providers.<id>]`` with only connection fields."""
    preview = preview_migration(_config(_entry(
        "OpenAI Main", extra="request_timeout = 45",
    )))

    assert _candidate(preview)["providers"] == {"openai-main": {
        "type": "openai", "base_url": "https://api.openai.com/v1", "request_timeout": 45,
    }}
    assert preview.proposed_mappings == (
        {"entry": "OpenAI Main", "provider_id": "openai-main", "type": "openai",
         "canonical_model": "openai-main/gpt-x"},
    )
    assert "[providers.openai-main]" in preview.candidate_config_text
    assert preview.stopped_choices == () and preview.complete is True


def test_model_field_is_not_written_to_candidate() -> None:
    """The legacy model list is removed: the catalog is app-managed."""
    candidate = _candidate(preview_migration(_config(_entry("a"), _entry("b", model="gpt-y"))))

    assert "models" not in candidate
    assert all("model" not in fields for fields in candidate["providers"].values())


def test_shared_connection_identity_merges_entries() -> None:
    """Entries with one (provider, base_url, api_key) tuple share one connection."""
    preview = preview_migration(_config(_entry("a", model="gpt-x"), _entry("b", model="gpt-y")))

    assert list(_candidate(preview)["providers"]) == ["a"]
    assert [m["canonical_model"] for m in preview.proposed_mappings] == ["a/gpt-x", "a/gpt-y"]
    assert preview.stopped_choices == ()


def test_provider_id_collision_gets_deterministic_suffix() -> None:
    """Distinct accounts whose names sanitize alike get stable ``-2`` suffixes."""
    text = _config(
        _entry("Main", base_url='"https://one.example/v1"'),
        _entry("main!", base_url='"https://two.example/v1"'),
    )

    first, second = preview_migration(text), preview_migration(text)

    assert list(_candidate(first)["providers"]) == ["main", "main-2"]
    assert [m["provider_id"] for m in first.proposed_mappings] == ["main", "main-2"]
    assert first == second


def test_credential_references_remain_in_original_without_promotion(tmp_path: Path) -> None:
    """Legacy references survive byte-for-byte in source, never in connections."""
    config = tmp_path / "config.toml"
    original = _config(
        _entry("env-entry", api_key='"env:API_KEY"'),
        _entry("sec-entry", provider="anthropic", api_key='"sec:key"',
               base_url='"https://api.anthropic.com"'),
        _entry("placeholder-entry", provider="google", api_key='"YOUR_API_KEY_HERE"',
               base_url=None),
    ).encode()
    config.write_bytes(original)

    preview = preview_migration(config.read_text())

    providers = _candidate(preview)["providers"]
    assert all("api_key" not in fields for fields in providers.values())
    assert all(plan.api_key is None for plan in preview.connections)
    assert all(raw.encode() in config.read_bytes() for raw in (
        'api_key = "env:API_KEY"', 'api_key = "sec:key"', 'api_key = "YOUR_API_KEY_HERE"',
    ))
    assert config.read_bytes() == original
    assert [(r["provider_id"], r["kind"], r["reference"]) for r in preview.unresolved_references] == [
        ("env-entry", "env", "env:API_KEY"),
        ("sec-entry", "sec", "sec:key"),
        ("placeholder-entry", "placeholder", None),
    ]
    assert preview.complete is False
    assert preview.candidate_default_model is None
    assert any("credential" in stop["message"].lower() for stop in preview.stopped_choices)


def test_literal_credential_is_not_promoted_or_printed() -> None:
    """Literal per-model credentials require a choice and cannot reach report or plan."""
    preview = preview_migration(_config(_entry("lit", api_key=f'"{_LITERAL_KEY}"')))

    assert "api_key" not in _candidate(preview)["providers"]["lit"]
    assert _LITERAL_KEY not in repr(preview.connections)
    assert _LITERAL_KEY not in render_preview(preview)
    assert preview.complete is False and preview.candidate_default_model is None
    assert any("credential" in stop["message"].lower() for stop in preview.stopped_choices)


@pytest.mark.parametrize(
    ("key", "literal"),
    [
        ("max_tokens", "2048"), ("temperature", "0.2"), ("top_p", "0.9"),
        ("vision", "true"), ("aliases", '["fast"]'), ("max_retries", "5"),
        ("retry_delay", "3"),
    ],
)
def test_tuning_and_retry_fields_are_discarded_and_reported(key: str, literal: str) -> None:
    """Tuning/retry/capability fields are dropped, reported by key and reason only."""
    preview = preview_migration(_config(_entry("a", extra=f"{key} = {literal}")))

    (discarded,) = [d for d in preview.discarded_values if d["key"] == f"models.a.{key}"]
    assert set(discarded) == {"key", "reason"} and discarded["reason"]
    assert key not in _candidate(preview)["providers"]["a"]


def test_discarded_reporting_never_echoes_values() -> None:
    """Discarded and unrecognised values never appear in the report text."""
    preview = preview_migration(_config(_entry(
        "a", extra=f'temperature = 0.5\nnotes = "{_TUNING_MARKER}"\nsystem_prompt = "{_TUNING_MARKER}"',
    )))

    keys = {d["key"] for d in preview.discarded_values}
    assert {"models.a.temperature", "models.a.notes", "models.a.system_prompt"} <= keys
    assert _TUNING_MARKER not in repr(preview.discarded_values)
    assert _TUNING_MARKER not in render_preview(preview)
    assert _TUNING_MARKER not in preview.candidate_config_text


def test_unsupported_entry_stops_for_operator_resolution() -> None:
    """An unsupported provider type is not mapped and is reported for resolution."""
    preview = preview_migration(_config(_entry("weird", provider="mystery"), _entry("ok")))

    assert _kinds(preview) == ["unsupported_entry"]
    assert preview.stopped_choices[0]["entries"] == ["weird"]
    assert list(_candidate(preview)["providers"]) == ["ok"]
    assert preview.complete is False


# ---------------------------------------------------------------------------
# Local Ollama
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("api_key", "base_url", "model"),
    [
        ('""', '"https://ollama.com"', "llama3"),
        (None, None, "llama3"),
        ('"env:OLLAMA_KEY"', '"http://localhost:11434"', "llama3"),
        ('"env:OLLAMA_KEY"', '"http://192.168.1.20:11434"', "qwen:cloud"),
    ],
    ids=["empty-key", "no-key-default-url", "localhost", "lan-with-cloud-suffix"],
)
def test_local_ollama_entry_stops_for_explicit_cloud_choice(
    api_key: str | None, base_url: str | None, model: str,
) -> None:
    """Local Ollama needs an explicit cloud replacement; a ``:cloud`` suffix is never
    taken as one. The entry is excluded and its original stays for the operator."""
    preview = preview_migration(_config(
        _entry("local", provider="ollama", api_key=api_key, base_url=base_url, model=model),
        _entry("other"),
    ))

    (stop,) = [stop for stop in preview.stopped_choices if stop["kind"] == "local_ollama"]
    assert stop["kind"] == "local_ollama" and stop["entries"] == ["local"]
    assert "https://ollama.com" in stop["message"]
    assert list(_candidate(preview)["providers"]) == ["other"]
    assert [m["entry"] for m in preview.proposed_mappings] == ["other"]
    assert any("original" in note.lower() for note in preview.notes)


def test_cloud_ollama_identity_is_proposed_without_credential_promotion() -> None:
    """The cloud identity is valid, but its per-model key needs operator choice."""
    preview = preview_migration(_config(_entry(
        "cloud", provider="ollama", api_key='"env:OLLAMA_KEY"', base_url='"https://ollama.com"',
        model="gpt-oss:120b",
    )))

    assert "local_ollama" not in _kinds(preview)
    assert _candidate(preview)["providers"]["cloud"]["type"] == "ollama"
    assert "api_key" not in _candidate(preview)["providers"]["cloud"]
    assert preview.complete is False and preview.candidate_default_model is None


def test_cloud_ollama_url_with_userinfo_requires_operator_choice() -> None:
    """A credential embedded in the cloud URL is not a valid cloud connection."""
    preview = preview_migration(_config(_entry(
        "cloud", provider="ollama", api_key='"env:OLLAMA_KEY"',
        base_url='"https://user:password@ollama.com"',
    )))

    assert "local_ollama" in _kinds(preview)
    assert "cloud" not in _candidate(preview).get("providers", {})
    assert preview.candidate_default_model is None and preview.complete is False


# ---------------------------------------------------------------------------
# Default seeding and ambiguity
# ---------------------------------------------------------------------------

def test_explicit_default_seeds_its_canonical_model() -> None:
    """``agent.default_model`` matching one mapped entry seeds its canonical ID."""
    preview = preview_migration(_config(
        _entry("a", model="gpt-x"),
        _entry("b", base_url='"https://other.example/v1"', model="gpt-y"),
        agent='default_model = "gpt-y"',
    ))

    assert preview.candidate_default_model == "b/gpt-y"


def test_explicit_default_with_legacy_key_is_not_applicable() -> None:
    """The old default cannot seed a selection until its credential is chosen."""
    preview = preview_migration(_config(
        _entry("a", api_key='"env:OLD_KEY"'), agent='default_model = "gpt-x"',
    ))

    assert preview.candidate_default_model is None
    assert preview.complete is False
    assert any("credential" in stop["message"].lower() for stop in preview.stopped_choices)


def test_default_matching_merged_connection_seeds() -> None:
    """Two matching entries on one connection are not ambiguous."""
    preview = preview_migration(_config(
        _entry("a", model="gpt-x"), _entry("b", model="gpt-x"), agent='default_model = "gpt-x"',
    ))

    assert preview.candidate_default_model == "a/gpt-x" and preview.stopped_choices == ()


def test_first_entry_seeds_when_no_default_configured() -> None:
    """Without a default, the old startup used the first entry: that is seeded."""
    preview = preview_migration(_config(
        _entry("first", model="gpt-first"),
        _entry("second", base_url='"https://other.example/v1"', model="gpt-second"),
    ))

    assert preview.candidate_default_model == "first/gpt-first"


def test_stopped_first_entry_infers_no_default() -> None:
    """When the first entry is not unambiguously mapped no other entry is inferred."""
    preview = preview_migration(_config(
        _entry("local", provider="ollama", api_key=None, base_url=None, model="llama3"),
        _entry("second", model="gpt-second"),
    ))

    assert preview.candidate_default_model is None


def test_default_matching_different_accounts_stops_without_seed() -> None:
    """A default matching entries on different accounts asks for a choice."""
    preview = preview_migration(_config(
        _entry("work", api_key='"env:WORK_KEY"', model="gpt-x"),
        _entry("home", api_key='"env:HOME_KEY"', model="gpt-x"),
        agent='default_model = "gpt-x"',
    ))

    (stop,) = [stop for stop in preview.stopped_choices if stop["reason"] == "default_model_ambiguous"]
    assert stop["kind"] == "ambiguous_account"
    assert stop["entries"] == ["work", "home"] and stop["candidates"] == ["home", "work"]
    assert preview.candidate_default_model is None
    assert preview.complete is False


def test_unknown_default_seeds_nothing() -> None:
    """A default naming no entry's model seeds nothing and infers nothing."""
    preview = preview_migration(_config(_entry("a"), agent='default_model = "missing-model"'))

    assert preview.candidate_default_model is None
    assert any("missing-model" in note for note in preview.notes)


def test_duplicate_entry_names_stop_without_mapping() -> None:
    """Identical entry names are ambiguous: neither is mapped or seeded."""
    preview = preview_migration(_config(
        _entry("same", model="gpt-x"),
        _entry("same", base_url='"https://other.example/v1"', model="gpt-y"),
        _entry("unique", base_url='"https://third.example/v1"', model="gpt-z"),
    ))

    assert _kinds(preview) == ["ambiguous_account"]
    assert preview.stopped_choices[0]["entries"] == ["same"]
    assert list(_candidate(preview)["providers"]) == ["unique"]
    assert preview.candidate_default_model is None


def test_pure_steps_compose_into_the_preview() -> None:
    """The exposed mapping, grouping and default steps agree with the preview."""
    text = _config(_entry("a", model="gpt-x"), _entry("b", model="gpt-y"), agent='default_model = "gpt-y"')
    entries = _toml.loads(text)["models"]

    mappings = [map_entry(entry, index) for index, entry in enumerate(entries)]
    connections = collect_connections(mappings)
    default = resolve_effective_default({"default_model": "gpt-y"}, mappings, connections.entry_ids)

    assert list(connections.connections) == ["a"]
    assert connections.entry_ids == {0: "a", 1: "a"}
    assert default.candidate == "a/gpt-y" == preview_migration(text).candidate_default_model


# ---------------------------------------------------------------------------
# Obsolete selectors and jobs
# ---------------------------------------------------------------------------

def test_obsolete_agent_selectors_are_dropped_and_reported() -> None:
    """default/background/fallback selectors leave the candidate and are reported;
    background is resolved for reporting only."""
    preview = preview_migration(_config(
        _entry("a", model="gpt-x"), _entry("b", model="gpt-y"),
        agent='default_model = "gpt-x"\nbackground_model = "gpt-y"\nfallback_models = [\n  "gpt-y",\n]',
    ))

    agent = _candidate(preview)["agent"]
    assert agent == {"max_iterations": 12, "step_notify_interval": 3}
    reasons = {d["key"]: d["reason"] for d in preview.discarded_values}
    assert {"agent.default_model", "agent.background_model", "agent.fallback_models"} <= set(reasons)
    assert "a/gpt-y" in reasons["agent.background_model"]
    assert preview.candidate_default_model == "a/gpt-x"


def test_singular_fallback_model_is_stripped_and_reported() -> None:
    """The old singular fallback selector must not survive into the new config."""
    preview = preview_migration(_config(_entry("a"), agent='fallback_model = "gpt-y"'))

    assert "fallback_model" not in _candidate(preview)["agent"]
    assert any(d["key"] == "agent.fallback_model" and d["reason"]
               for d in preview.discarded_values)
    assert preview.preservation["verified"] is True and preview.complete is True


def test_per_job_fallback_models_are_reported_and_jobs_untouched(tmp_path: Path) -> None:
    """Per-job fallbacks are reported only; the scheduler file is read, never rewritten."""
    config = tmp_path / "config.toml"
    config.write_text(_config(_entry("a")))
    jobs = tmp_path / "scheduler.toml"
    jobs_text = (
        '[jobs.nightly]\nschedule = "cron"\ncron = "0 2 * * *"\ntask = "t"\n'
        'fallback_models = ["gpt-y"]\n\n[jobs.plain]\nschedule = "cron"\ncron = "0 3 * * *"\ntask = "u"\n'
    )
    jobs.write_text(jobs_text)

    preview = preview_migration(config.read_text(), jobs_text)
    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config), "--scheduler-config", str(jobs)])

    assert caught.value.code == 0
    (discarded,) = [d for d in preview.discarded_values if d["key"].startswith("jobs.")]
    assert discarded["key"] == "jobs.nightly.fallback_models"
    assert "converter" in discarded["reason"]
    assert jobs.read_text() == jobs_text


# ---------------------------------------------------------------------------
# Unrelated sections
# ---------------------------------------------------------------------------

def test_unrelated_sections_are_preserved_by_parsed_comparison() -> None:
    """Every unrelated table parses identically in the candidate; the preview
    records the same verification."""
    text = _config(_entry("a"), agent='default_model = "gpt-x"')
    original = _toml.loads(text)

    preview = preview_migration(text)
    candidate = _candidate(preview)

    for section in ("telegram", "scheduler", "shell", "nsjail", "embeddings"):
        assert candidate[section] == original[section], section
    assert candidate["agent"] == {k: v for k, v in original["agent"].items() if k != "default_model"}
    assert set(candidate) == set(original) - {"models"} | {"providers"}
    assert preview.preservation["verified"] is True
    assert preview.preservation["differing_sections"] == []
    assert {"telegram", "scheduler", "shell", "nsjail", "embeddings", "agent"} <= set(
        preview.preservation["compared_sections"]
    )


def test_unverifiable_edit_withholds_candidate() -> None:
    """When the bounded edit cannot prove unrelated values unchanged, no candidate."""
    text = 'agent.default_model = "gpt-x"\n\n' + _entry("a") + '[telegram]\nbot_token = "t"\n'

    preview = preview_migration(text)

    assert preview.preservation["verified"] is False
    assert preview.candidate_config_text is None
    assert preview.complete is False


def test_multiline_string_resembling_model_header_fails_closed() -> None:
    """A quoted header is never silently deleted as an actual model table."""
    text = (
        '[telegram]\nmessage = """\n[[models]]\nnot a table\n"""\n\n'
        + _entry("a") + '[agent]\nmax_iterations = 12\n'
    )
    preview = preview_migration(text)

    assert preview.candidate_config_text is None
    assert preview.preservation["verified"] is False
    assert preview.complete is False


def test_existing_provider_connections_are_kept_and_ids_avoid_them() -> None:
    """Pre-existing ``[providers.*]`` tables are preserved and never overwritten."""
    text = '[providers.a]\ntype = "anthropic"\napi_key = "env:EXISTING"\n\n' + _config(_entry("a"))

    providers = _candidate(preview_migration(text))["providers"]

    assert providers["a"] == {"type": "anthropic", "api_key": "env:EXISTING"}
    assert providers["a-2"] == {"type": "openai", "base_url": "https://api.openai.com/v1"}


def test_config_without_legacy_models_has_nothing_to_migrate() -> None:
    """An already-migrated configuration produces no candidate and no stops."""
    preview = preview_migration('[providers.a]\ntype = "openai"\napi_key = "env:K"\n')

    assert preview.candidate_config_text is None
    assert preview.proposed_mappings == () and preview.stopped_choices == ()
    assert any("nothing to migrate" in note for note in preview.notes)


def test_selector_only_config_strips_all_obsolete_agent_selectors() -> None:
    """An input with no legacy models still needs its rejected selectors removed."""
    text = (
        '[agent]\nmax_iterations = 12\ndefault_model = "old"\n'
        'background_model = "old"\nfallback_model = "old"\n'
        'fallback_models = ["old"]\n\n[telegram]\nallowed_user_ids = [1, 2]\n'
    )
    preview = preview_migration(text)

    candidate = _candidate(preview)
    assert candidate["agent"] == {"max_iterations": 12}
    assert candidate["telegram"] == _toml.loads(text)["telegram"]
    assert {d["key"] for d in preview.discarded_values} >= {
        "agent.default_model", "agent.background_model", "agent.fallback_model",
        "agent.fallback_models",
    }
    assert preview.preservation["verified"] is True
    assert not any("nothing to migrate" in note for note in preview.notes)


# ---------------------------------------------------------------------------
# CLI and immutability
# ---------------------------------------------------------------------------

def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under *root* with its bytes (directories as None)."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def test_preview_leaves_original_files_unchanged(tmp_path: Path) -> None:
    """The CLI preview reads the config and creates or changes no file."""
    config = tmp_path / "config.toml"
    config.write_text(_config(_entry("a"), _entry("local", provider="ollama", api_key=None, base_url=None)))
    before = _tree(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config)])

    assert caught.value.code == 0
    assert _tree(tmp_path) == before


def test_main_prints_report_with_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The preview exits 0 and prints mappings, stops, discards and the candidate."""
    config = tmp_path / "config.toml"
    config.write_text(_config(
        _entry("a", api_key=f'"{_LITERAL_KEY}"', extra="temperature = 0.3"),
        _entry("local", provider="ollama", api_key=None, base_url=None),
    ))

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config)])

    out = capsys.readouterr().out
    assert caught.value.code == 0
    for fragment in ("PREVIEW", "[providers.a]", "local_ollama", "models.a.temperature", "Candidate configuration"):
        assert fragment in out, fragment
    assert _LITERAL_KEY not in out


def test_rerun_preview_produces_the_same_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Running the preview twice on unchanged input gives the identical report."""
    config = tmp_path / "config.toml"
    config.write_text(_config(_entry("a"), _entry("b", api_key='"env:B"'), agent='default_model = "gpt-x"'))

    outputs = []
    for _ in range(2):
        with pytest.raises(SystemExit):
            main(["--config", str(config)])
        outputs.append(capsys.readouterr().out)

    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("state", ["missing", "directory", "invalid-toml", "invalid-utf8"])
def test_unreadable_or_invalid_config_fails_actionably(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, state: str,
) -> None:
    """A config that cannot be read or parsed exits nonzero naming the file."""
    config = tmp_path / "config.toml"
    if state == "directory":
        config.mkdir()
    elif state == "invalid-toml":
        config.write_text(f'[[models]]\nname = "a\napi_key = "{_LITERAL_KEY}"\n')
    elif state == "invalid-utf8":
        config.write_bytes(b"\xff\xfe not utf-8")
    caplog.set_level(logging.ERROR, logger="migrate_models")

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config)])

    assert caught.value.code == 1
    messages = [r.getMessage() for r in caplog.records if r.name == "migrate_models"]
    assert len(messages) == 1 and str(config) in messages[0]
    assert _LITERAL_KEY not in messages[0]


def test_apply_flag_is_rejected_as_unknown(tmp_path: Path) -> None:
    """``--apply`` is not available in the preview-only tool."""
    config = tmp_path / "config.toml"
    config.write_text(_config(_entry("a")))
    before = _tree(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config), "--apply"])

    assert caught.value.code == 2
    assert _tree(tmp_path) == before
