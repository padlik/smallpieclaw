"""Preview-first legacy ``[[models]]`` migration (task 10.1).

Every decision of the ratified migration table is exercised through the pure
preview API and the ``main(argv)`` CLI. Inputs are local TOML text or files
under ``tmp_path``; nothing is resolved, sent or written.
"""

from __future__ import annotations

import logging
import hashlib
import json
import os
from dataclasses import replace
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
from model_registry import CatalogDocument, ModelMetadata, ModelRecord, ProviderState, SupplementRecord
from model_settings import ModelSettings
from xdg import XDGPaths, xdg_paths

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


def test_apply_without_agent_name_refuses_without_writes(tmp_path: Path) -> None:
    """Explicit apply needs an agent identity before selecting protected targets."""
    config = tmp_path / "config.toml"
    config.write_text(_config(_entry("a")))
    before = _tree(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(config), "--apply"])

    assert caught.value.code == 2
    assert _tree(tmp_path) == before


# Task 10.2 — apply/recovery tests. All paths resolve inside temporary XDG roots.

_MANIFEST = "models-migration.json"
_BACKUP_SUFFIX = ".migration.bak"


def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> XDGPaths:
    """Keep all agent paths (including PID and state) inside this one sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for variable, bucket in (
        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / bucket))
    paths = xdg_paths("migration-test")
    paths.config_home.mkdir(parents=True)
    paths.runtime_dir.mkdir(parents=True)
    return paths


def _legacy_file(paths: XDGPaths, *, entry: str | None = None, agent: str = "") -> bytes:
    """Create one credential-free legacy config and return its exact bytes."""
    content = _config(entry or _entry("a"), agent=agent).encode()
    paths.config_file.write_bytes(content)
    return content


def _existing_catalog(paths: XDGPaths, *, selection: str | None = "saved/model") -> CatalogDocument:
    """Real managed settings with provenance, supplements and operator selection."""
    document = CatalogDocument(
        1, 7, selection,
        (ProviderState("saved", "anthropic", "2026-01-01T00:00:00Z", "stale", "rate_limited"),),
        (ModelRecord("saved/model", "saved", "model", "Operator model",
                     ModelMetadata(context_tokens=4000, sources=(("context_tokens", "provider"),))),),
        (SupplementRecord("anthropic", "other", "2026-01-01T00:00:00Z",
                          ModelMetadata(vision=True, sources=(("vision", "models.dev"),))),),
    )
    ModelSettings(paths.models_config_file).write(document)
    return document


def _run_apply(paths: XDGPaths, *extra: str) -> int:
    """Invoke the real CLI and return its exit code without swallowing failures."""
    with pytest.raises(SystemExit) as caught:
        main(["--agent-name", "migration-test", *extra])
    return int(caught.value.code)


def _manifest(paths: XDGPaths) -> dict:
    """Read the proposed versioned recovery record from the isolated config home."""
    return json.loads((paths.config_home / _MANIFEST).read_text())


def test_preview_with_agent_name_remains_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming a target without --apply still performs no write."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    before = _tree(tmp_path)

    assert _run_apply(paths, "--config", str(paths.config_file)) == 0
    assert _tree(tmp_path) == before


def test_apply_rejects_config_outside_agent_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An alternate --config cannot redirect a protected write."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    alternate = tmp_path / "other.toml"
    alternate.write_text(_config(_entry("other")))
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply", "--config", str(alternate)) != 0
    assert _tree(tmp_path) == before


def test_recover_is_mutually_exclusive_with_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single invocation cannot both apply and roll back."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply", "--recover") == 2
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("entry", [
    _entry("a", api_key='"env:API_KEY"'),
    _entry("a", provider="ollama", api_key=None, base_url='"http://localhost:11434"'),
    _entry("a", model="gpt-x") + _entry("a", model="gpt-y"),
], ids=["credential-choice", "local-ollama", "duplicate-account"])
def test_incomplete_preview_refuses_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    """Unresolved migration choices must leave all targets and backups absent."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths, entry=entry)
    _existing_catalog(paths)
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before


def test_unverified_candidate_refuses_before_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded edit that cannot prove parsed preservation is never applied."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    paths.config_file.write_text('agent.default_model = "gpt-x"\n\n' + _entry("a"))
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("settings", ["invalid-toml", "invalid-schema"])
def test_corrupt_managed_settings_refuse_without_preservation_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: str,
) -> None:
    """A corrupt catalog is not 'repaired' or backed up on a refused apply."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    paths.models_config_file.write_text("[invalid\n" if settings == "invalid-toml" else "schema_version = 999\n")
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before


def test_unreadable_managed_settings_refuse_without_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read error is not mistaken for absent managed settings."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    _existing_catalog(paths)
    before = _tree(tmp_path)
    original_read = Path.read_bytes

    def refuse_settings_read(path: Path) -> bytes:
        """Fail only the real settings-file read boundary."""
        if path == paths.models_config_file:
            raise PermissionError("injected settings read error")
        return original_read(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_bytes", refuse_settings_read)
        assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before


def test_invalid_staged_config_refuses_before_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural validation rejects a provider type unknown to the new schema."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    paths.config_file.write_text('[providers.invalid]\ntype = "unknown"\n\n' + _config(_entry("a")))
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before


def test_apply_preserves_existing_operator_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A remembered operator choice wins over the demonstrable legacy default."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    original = _existing_catalog(paths)

    assert _run_apply(paths, "--apply") == 0
    loaded = ModelSettings(paths.models_config_file, read_only=True).load()
    assert loaded.status == "valid" and loaded.document == original


def test_apply_seeds_missing_selection_without_losing_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeding changes only selection and revision, not inventory/provenance."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    original = _existing_catalog(paths, selection=None)

    assert _run_apply(paths, "--apply") == 0
    loaded = ModelSettings(paths.models_config_file, read_only=True).load()
    assert loaded.status == "valid"
    assert loaded.document == replace(original, default_model="a/gpt-x", revision=8)


def test_apply_seeds_absent_settings_with_empty_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A canonical default without discovered models survives real schema reload."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)

    assert _run_apply(paths, "--apply") == 0
    loaded = ModelSettings(paths.models_config_file, read_only=True).load()
    assert loaded.status == "valid"
    assert loaded.document == CatalogDocument(1, 1, "a/gpt-x", (), (), ())


def test_apply_without_seed_leaves_absent_settings_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No demonstrable default means no invented managed document."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths, agent='default_model = "missing"')

    assert _run_apply(paths, "--apply") == 0
    assert not paths.models_config_file.exists()


def test_apply_backs_up_exact_originals_before_target_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both byte-exact backups exist before the first protected target changes."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before = _legacy_file(paths)
    _existing_catalog(paths)
    settings_before = paths.models_config_file.read_bytes()
    replace_file = os.replace
    observed: list[Path] = []

    def inspect_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Inspect the real filesystem at the atomic replace boundary."""
        destination = Path(target)
        if destination in {paths.config_file, paths.models_config_file}:
            assert paths.config_file.with_name(paths.config_file.name + _BACKUP_SUFFIX).read_bytes() == config_before
            assert paths.models_config_file.with_name(paths.models_config_file.name + _BACKUP_SUFFIX).read_bytes() == settings_before
            observed.append(destination)
        replace_file(source, target)

    monkeypatch.setattr(os, "replace", inspect_replace)
    assert _run_apply(paths, "--apply") == 0
    assert observed and paths.config_file in observed
    record = _manifest(paths) if (paths.config_home / _MANIFEST).exists() else None
    assert record is None  # success clears only the active manifest, not the backups


def test_apply_retains_unrelated_explicit_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing provider/embedding env: and sec: refs remain unrelated human data."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    original = (
        '[providers.existing]\ntype = "openai"\napi_key = "sec:CHAT_TOKEN"\n\n'
        + _config(_entry("a")).replace(
            'api_key = "env:EMBED_KEY"', 'api_key = "env:EMBED_TOKEN"',
        )
    )
    paths.config_file.write_text(original)

    assert _run_apply(paths, "--apply") == 0
    parsed = _toml.loads(paths.config_file.read_text())
    assert parsed["providers"]["existing"]["api_key"] == "sec:CHAT_TOKEN"
    assert parsed["embeddings"]["api_key"] == "env:EMBED_TOKEN"


def test_apply_does_not_touch_unrelated_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration writes only config/settings and its bounded recovery artifacts."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    protected = {}
    for relative in ("scheduler.toml", "jobs/history.jsonl", "prompts.jsonl", "logs/agent.log",
                     "memory.json", "vectors/data.bin", "conversations/1.json"):
        target = paths.state_home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"operator-owned\x00\xff")
        protected[target] = target.read_bytes()

    assert _run_apply(paths, "--apply") == 0
    assert {path: path.read_bytes() for path in protected} == protected


def test_repeating_successful_apply_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second apply does not transform the same config or increment revision."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    assert _run_apply(paths, "--apply") == 0
    after_first = (paths.config_file.read_bytes(), paths.models_config_file.read_bytes())

    assert _run_apply(paths, "--apply") == 0
    assert (paths.config_file.read_bytes(), paths.models_config_file.read_bytes()) == after_first


def test_live_daemon_lock_refuses_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running daemon excludes migration before backups or target edits."""
    from process_lock import PidFileLock

    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    _existing_catalog(paths)
    with PidFileLock(str(paths.pid_file)):
        before = _tree(tmp_path)
        assert _run_apply(paths, "--apply") != 0
        assert _tree(tmp_path) == before


def test_backup_failure_keeps_both_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed durable backup cannot proceed to the first target replacement."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before = _legacy_file(paths)
    _existing_catalog(paths)
    settings_before = paths.models_config_file.read_bytes()
    real_replace = os.replace

    def refuse_backup(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Fail only the backup's filesystem replace, not application logic."""
        if str(target).endswith(_BACKUP_SUFFIX):
            raise OSError("injected backup I/O failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", refuse_backup)
    assert _run_apply(paths, "--apply") != 0
    assert paths.config_file.read_bytes() == config_before
    assert paths.models_config_file.read_bytes() == settings_before


def test_failure_after_config_replace_restores_only_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O error on settings replacement auto-rolls back config by hash."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before = _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    settings_before = paths.models_config_file.read_bytes()
    real_replace = os.replace
    saw_config = False

    def fail_settings(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Inject one target write failure after the config has been replaced."""
        nonlocal saw_config
        if Path(target) == paths.config_file:
            saw_config = True
        if Path(target) == paths.models_config_file and saw_config:
            raise OSError("injected settings replacement failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_settings)
    assert _run_apply(paths, "--apply") != 0
    assert saw_config
    assert paths.config_file.read_bytes() == config_before
    assert paths.models_config_file.read_bytes() == settings_before


def test_operator_edit_during_failure_window_is_not_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit between the two target writes wins over automatic rollback."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    edited = b"# operator edit after migration wrote config\n"
    real_replace = os.replace
    saw_config = False

    def fail_after_edit(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Simulate an external edit followed by settings I/O failure."""
        nonlocal saw_config
        if Path(target) == paths.config_file:
            saw_config = True
        if Path(target) == paths.models_config_file and saw_config:
            paths.config_file.write_bytes(edited)
            raise OSError("injected settings I/O failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_after_edit)
    assert _run_apply(paths, "--apply") != 0
    assert paths.config_file.read_bytes() == edited
    assert (paths.config_home / _MANIFEST).exists()  # unresolved action retained


def _crash_after_config(paths: XDGPaths, monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, bytes]:
    """Raise BaseException after config replacement, bypassing in-process rollback."""
    config_before = _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    settings_before = paths.models_config_file.read_bytes()
    real_replace = os.replace

    class SimulatedPowerLoss(BaseException):
        """A crash not caught by the normal OSError rollback path."""

    def crash(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Leave a completed config write with an active recovery manifest."""
        real_replace(source, target)
        if Path(target) == paths.config_file:
            raise SimulatedPowerLoss

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", crash)
        with pytest.raises(SimulatedPowerLoss):
            _run_apply(paths, "--apply")
    return config_before, settings_before


def test_crash_manifest_records_bound_hashes_and_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash leaves a private, versioned record that binds both actual targets."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before, settings_before = _crash_after_config(paths, monkeypatch)

    record = _manifest(paths)
    assert record["version"] == 1 and record["agent_name"] == "migration-test"
    assert record["phase"] in {"writing_config", "config_written"}
    for key, path, original in (
        ("config", paths.config_file, config_before),
        ("settings", paths.models_config_file, settings_before),
    ):
        item = record["targets"][key]
        assert item["path"] == str(path.resolve())
        assert item["original_sha256"] == hashlib.sha256(original).hexdigest()
        assert Path(item["backup_path"]).read_bytes() == original
        assert item["backup_sha256"] == hashlib.sha256(original).hexdigest()
        assert len(item["candidate_sha256"]) == 64
    assert paths.config_file.read_bytes() != config_before
    assert paths.models_config_file.read_bytes() == settings_before
    assert (paths.config_home / _MANIFEST).stat().st_mode & 0o077 == 0


def test_pending_manifest_blocks_new_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupted transaction requires explicit recovery before new apply."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_config(paths, monkeypatch)
    before = _tree(tmp_path)

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before


def test_recover_restores_candidate_hash_from_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery restores only a target still matching this transaction's candidate."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before, settings_before = _crash_after_config(paths, monkeypatch)

    assert _run_apply(paths, "--recover") == 0
    assert paths.config_file.read_bytes() == config_before
    assert paths.models_config_file.read_bytes() == settings_before
    assert not (paths.config_home / _MANIFEST).exists()


def test_recover_leaves_operator_edit_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A target matching neither original nor candidate remains an unresolved edit."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_config(paths, monkeypatch)
    changed = b"# operator changed this target\n"
    paths.config_file.write_bytes(changed)

    assert _run_apply(paths, "--recover") != 0
    assert paths.config_file.read_bytes() == changed
    assert (paths.config_home / _MANIFEST).exists()


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_recover_refuses_unverified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    """A missing or altered backup cannot be used to restore a candidate."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_config(paths, monkeypatch)
    backup = Path(_manifest(paths)["targets"]["config"]["backup_path"])
    if damage == "missing":
        backup.unlink()
    else:
        backup.write_bytes(b"changed backup")
    candidate = paths.config_file.read_bytes()

    assert _run_apply(paths, "--recover") != 0
    assert paths.config_file.read_bytes() == candidate
    assert (paths.config_home / _MANIFEST).exists()


@pytest.mark.parametrize("damage", ["bad-json", "version", "agent", "path", "hash"])
def test_recover_refuses_untrusted_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    """Malformed or mismatched recovery instructions never change targets."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_config(paths, monkeypatch)
    manifest = paths.config_home / _MANIFEST
    record = _manifest(paths)
    if damage == "version":
        record["version"] = 999
    elif damage == "agent":
        record["agent_name"] = "someone-else"
    elif damage == "path":
        record["targets"]["config"]["path"] = str(tmp_path / "other.toml")
    elif damage == "hash":
        record["targets"]["config"]["candidate_sha256"] = "not-a-sha256"
    manifest.write_text("{" if damage == "bad-json" else json.dumps(record))
    config_before = paths.config_file.read_bytes()
    settings_before = paths.models_config_file.read_bytes()

    assert _run_apply(paths, "--recover") != 0
    assert paths.config_file.read_bytes() == config_before
    assert paths.models_config_file.read_bytes() == settings_before
    assert manifest.exists()


def test_recover_after_success_does_not_undo_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully applied result must not be rolled back on a repeated recovery."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    assert _run_apply(paths, "--apply") == 0
    saved = paths.models_config_file.read_bytes()

    assert _run_apply(paths, "--recover") == 0
    assert paths.models_config_file.read_bytes() == saved
    assert _run_apply(paths, "--recover") == 0


def test_recover_treats_original_hash_as_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A target already restored to original needs no destructive rewrite."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    original, _ = _crash_after_config(paths, monkeypatch)
    paths.config_file.write_bytes(original)

    assert _run_apply(paths, "--recover") == 0
    assert paths.config_file.read_bytes() == original
    assert not (paths.config_home / _MANIFEST).exists()


def _crash_after_new_settings(paths: XDGPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupt after writing a previously absent settings target."""
    _legacy_file(paths)
    real_replace = os.replace

    class SimulatedPowerLoss(BaseException):
        """Crash not caught by normal I/O rollback."""

    def interrupt(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Leave the own settings candidate on disk for recovery."""
        real_replace(source, target)
        if Path(target) == paths.models_config_file:
            raise SimulatedPowerLoss

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", interrupt)
        with pytest.raises(SimulatedPowerLoss):
            _run_apply(paths, "--apply")


def test_recover_removes_only_own_new_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Originally absent settings can be removed if they still match candidate."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_new_settings(paths, monkeypatch)
    assert paths.models_config_file.exists()
    assert _manifest(paths)["targets"]["settings"]["original_sha256"] is None

    assert _run_apply(paths, "--recover") == 0
    assert not paths.models_config_file.exists()


def test_recover_preserves_operator_edit_of_new_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator edits to a newly created settings target prevent removal."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_new_settings(paths, monkeypatch)
    edit = b"operator-edited settings\n"
    paths.models_config_file.write_bytes(edit)

    assert _run_apply(paths, "--recover") != 0
    assert paths.models_config_file.read_bytes() == edit
    assert (paths.config_home / _MANIFEST).exists()


def test_manifest_write_failure_precedes_target_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No target may change when durable manifest publication fails."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before = _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    settings_before = paths.models_config_file.read_bytes()
    real_replace = os.replace

    def refuse_manifest(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Fail only an atomic manifest write at the filesystem boundary."""
        if Path(target) == paths.config_home / _MANIFEST:
            raise OSError("injected manifest persistence failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", refuse_manifest)
    assert _run_apply(paths, "--apply") != 0
    assert paths.config_file.read_bytes() == config_before
    assert paths.models_config_file.read_bytes() == settings_before


def test_completed_phase_crash_does_not_undo_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed manifest left by a cleanup crash never triggers rollback."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    real_unlink = os.unlink

    class SimulatedPowerLoss(BaseException):
        """Crash at active-manifest cleanup, not during target replacement."""

    def interrupt_cleanup(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        """Leave a completed-phase manifest for explicit recovery."""
        if Path(path) == paths.config_home / _MANIFEST:
            raise SimulatedPowerLoss
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "unlink", interrupt_cleanup)
        with pytest.raises(SimulatedPowerLoss):
            _run_apply(paths, "--apply")
    assert _manifest(paths)["phase"] == "completed"
    after = (paths.config_file.read_bytes(), paths.models_config_file.read_bytes())

    assert _run_apply(paths, "--recover") == 0
    assert (paths.config_file.read_bytes(), paths.models_config_file.read_bytes()) == after


def test_initial_fsync_failure_keeps_targets_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Durability failure in preparation cannot silently proceed to writes."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_before = _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    settings_before = paths.models_config_file.read_bytes()

    def refuse_fsync(_fd: int) -> None:
        """Simulate a failed filesystem durability boundary."""
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", refuse_fsync)
    assert _run_apply(paths, "--apply") != 0
    assert paths.config_file.read_bytes() == config_before
    assert paths.models_config_file.read_bytes() == settings_before


# ---------------------------------------------------------------------------
# Second review — completed-publication stranding and symlink safety
# ---------------------------------------------------------------------------


def _strand_between_completed_publication_and_rollback(
    paths: XDGPaths, monkeypatch: pytest.MonkeyPatch,
) -> tuple[bytes, bytes]:
    """Interrupt an apply after the completed-manifest rename, mid-rollback.

    The manifest's directory fsync fails right after the ``completed`` rename,
    and the automatic rollback then fails once the first target (config) is
    restored — leaving a stranded recoverable state: an on-disk ``completed``
    manifest beside mixed targets. Returns the (config, settings) originals.
    """
    config_original = _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    settings_original = paths.models_config_file.read_bytes()
    real_replace, real_fsync = os.replace, os.fsync
    state = {"completed_seen": False, "fsync_failed": False}

    def fail_manifest_fsync_then_settings_restore(
        source: str | os.PathLike[str], target: str | os.PathLike[str],
    ) -> None:
        """Let the completed rename pass, then fail the settings restore."""
        if Path(target) == paths.config_home / _MANIFEST:
            state["completed_seen"] = b'"completed"' in Path(source).read_bytes()
        if state["completed_seen"] and Path(target) == paths.models_config_file:
            raise OSError("injected settings restore failure")
        real_replace(source, target)

    def fail_first_dir_fsync(fd: int) -> None:
        """Fail only the first fsync after the completed rename (its dir fsync)."""
        if state["completed_seen"] and not state["fsync_failed"]:
            state["fsync_failed"] = True
            raise OSError("injected manifest directory fsync failure")
        real_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", fail_manifest_fsync_then_settings_restore)
        scoped.setattr(os, "fsync", fail_first_dir_fsync)
        assert _run_apply(paths, "--apply") != 0
    assert state["completed_seen"] and state["fsync_failed"]
    return config_original, settings_original


def test_interrupted_rollback_beside_completed_manifest_keeps_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stranded rollback keeps the manifest and the mixed state it describes."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_original, settings_original = _strand_between_completed_publication_and_rollback(
        paths, monkeypatch,
    )

    assert (paths.config_home / _MANIFEST).exists()
    assert paths.config_file.read_bytes() == config_original
    settings_now = paths.models_config_file.read_bytes()
    assert settings_now != settings_original  # the migration's own result, unrestored
    assert paths.config_file.with_name(paths.config_file.name + _BACKUP_SUFFIX).exists()
    assert paths.models_config_file.with_name(paths.models_config_file.name + _BACKUP_SUFFIX).exists()


def test_recover_refuses_mixed_state_beside_completed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Recover must not trust a completed record beside mixed target state."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_original, _ = _strand_between_completed_publication_and_rollback(paths, monkeypatch)
    settings_stranded = paths.models_config_file.read_bytes()
    caplog.set_level(logging.WARNING, logger="migrate_models")

    assert _run_apply(paths, "--recover") != 0
    assert paths.config_file.read_bytes() == config_original
    assert paths.models_config_file.read_bytes() == settings_stranded
    assert (paths.config_home / _MANIFEST).exists()
    messages = [r.getMessage() for r in caplog.records if r.name == "migrate_models"]
    assert any("unresolved" in message.lower() for message in messages)


def test_recover_treats_all_candidate_completed_manifest_as_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed manifest whose targets all verify as the migration's result
    is a committed transaction; recovery only clears the record, never undoes."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    config_original = _legacy_file(paths)
    _existing_catalog(paths, selection=None)
    settings_original = paths.models_config_file.read_bytes()
    assert _run_apply(paths, "--apply") == 0
    applied = (paths.config_file.read_bytes(), paths.models_config_file.read_bytes())

    def bound_item(path: Path, original: bytes) -> dict:
        """The manifest entry the interrupted cleanup would have left behind."""
        return {
            "path": str(path.resolve()),
            "original_sha256": hashlib.sha256(original).hexdigest(),
            "backup_path": str(path.with_name(path.name + _BACKUP_SUFFIX)),
            "backup_sha256": hashlib.sha256(original).hexdigest(),
            "candidate_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    # Simulate a crash after the completed manifest was durably published.
    (paths.config_home / _MANIFEST).write_text(json.dumps({
        "version": 1, "agent_name": "migration-test", "phase": "completed",
        "targets": {
            "config": bound_item(paths.config_file, config_original),
            "settings": bound_item(paths.models_config_file, settings_original),
        },
    }))

    assert _run_apply(paths, "--recover") == 0
    assert (paths.config_file.read_bytes(), paths.models_config_file.read_bytes()) == applied
    assert (paths.config_home / _MANIFEST).exists() is False


@pytest.mark.parametrize("leaf", ["config", "settings"])
def test_recover_refuses_symlinked_target_leaf_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leaf: str,
) -> None:
    """A target leaf replaced by a symlink never matches its recorded binding."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _crash_after_config(paths, monkeypatch)
    target = paths.config_file if leaf == "config" else paths.models_config_file
    preserved = target.read_bytes()
    real = tmp_path / "elsewhere.toml"
    real.write_bytes(preserved)
    target.unlink()
    target.symlink_to(real)

    assert _run_apply(paths, "--recover") != 0
    assert target.is_symlink() and target.read_bytes() == preserved
    assert real.read_bytes() == preserved
    assert (paths.config_home / _MANIFEST).exists()


def _symlink_warning(caplog: pytest.LogCaptureFixture) -> bool:
    """Whether the migrator named a symlink in its refusal."""
    return any(
        "symlink" in record.getMessage().lower()
        for record in caplog.records if record.name == "migrate_models"
    )


def test_apply_refuses_symlinked_config_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A symlinked config leaf is refused before backups, manifest or targets."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    real = tmp_path / "real-config.toml"
    real.write_text(_config(_entry("a")))
    paths.config_file.symlink_to(real)
    before = _tree(tmp_path)
    caplog.set_level(logging.WARNING, logger="migrate_models")

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before
    assert paths.config_file.is_symlink()
    assert _symlink_warning(caplog)


def test_apply_refuses_symlinked_settings_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A symlinked managed-settings leaf is refused before any write."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    _legacy_file(paths)
    _existing_catalog(paths)
    real = tmp_path / "real-models-config.toml"
    real.write_bytes(paths.models_config_file.read_bytes())
    paths.models_config_file.unlink()
    paths.models_config_file.symlink_to(real)
    before = _tree(tmp_path)
    caplog.set_level(logging.WARNING, logger="migrate_models")

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before
    assert paths.models_config_file.is_symlink()
    assert _symlink_warning(caplog)


@pytest.mark.parametrize("leaf", ["config", "settings"])
def test_apply_refuses_dangling_symlink_target_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, leaf: str,
) -> None:
    """A dangling symlink leaf is a symlink refusal, not an absent file."""
    paths = _isolated_paths(tmp_path, monkeypatch)
    if leaf == "settings":
        _legacy_file(paths)
    target = paths.config_file if leaf == "config" else paths.models_config_file
    target.symlink_to(tmp_path / f"missing-{leaf}.toml")
    before = _tree(tmp_path)
    caplog.set_level(logging.WARNING, logger="migrate_models")

    assert _run_apply(paths, "--apply") != 0
    assert _tree(tmp_path) == before
    assert target.is_symlink()
    assert _symlink_warning(caplog)
