"""Preview-first migration of legacy ``[[models]]`` configuration.

Reads a legacy configuration (and optionally the scheduler job file) and
reports how it maps to ``[providers.<id>]`` connections, without writing any
file, resolving any credential or contacting any network:

- each unambiguous connection identity ``(provider, base_url, api_key)``
  can be proposed as one connection; a legacy per-model ``api_key`` is kept
  byte for byte only in the original source, never transferred to a connection.
  An operator must choose the connection credential before such a proposal is
  complete; references are reported but never resolved;
- the legacy ``model`` field only feeds default resolution (the catalog is
  managed by the app); tuning/retry/capability fields are dropped and reported
  by key and reason, never by value;
- local Ollama entries, ambiguous accounts, duplicate entry names and
  unsupported entries stop for an explicit operator choice;
- only the demonstrable old effective default may seed the remembered
  selection: an exact ``agent.default_model`` match on one connection, or the
  first entry when no default was configured, and no credential choice pending;
- obsolete ``agent`` selectors and per-job ``fallback_models`` are reported
  (jobs are never rewritten; no job converter exists);
- every unrelated section is kept verbatim in the candidate text (including
  any unrelated credential values), verified by a parsed comparison; if that
  cannot be proven no candidate is produced. Incomplete candidates are only
  proposals, not applicable configuration.

Usage::

    python migrate_models.py --config config.toml [--scheduler-config scheduler.toml]
    python migrate_models.py --agent-name NAME            # preview the agent's config
    python migrate_models.py --agent-name NAME --apply    # apply a complete preview
    python migrate_models.py --agent-name NAME --recover  # finish an interrupted apply

Without ``--apply``/``--recover`` nothing is written. ``--apply`` and
``--recover`` require ``--agent-name``, act only on that agent's XDG
``config.toml`` and ``models-config.toml``, and hold the agent's PID lock, so
they refuse while the daemon runs. ``--apply`` refuses (before any write) an
incomplete or unverified preview, a candidate that fails structural config
validation, unreadable or invalid managed settings, a symlinked config or
settings leaf (including a dangling one) and a pending recovery record. It
then stages and validates both results, writes byte-exact
owner-only backups (``<file>.migration.bak``, kept afterwards), records a
recovery manifest (``models-migration.json`` in the config directory) with the
original, backup and candidate SHA-256 of each file, and replaces each file
atomically. An existing remembered selection is never overwritten; the old
effective default is seeded only when none is saved. A failure rolls back
only files still matching this migration's result; anything else is left
untouched and reported. ``--recover`` applies the same rule after a crash: a
completed record whose files do not all match it, or a partially restored
rollback, is reported as unresolved and its evidence is kept, never cleared.
Jobs, history, prompts, logs, memories, vectors and conversations are never
touched.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

try:
    import tomli as _toml  # type: ignore[import]
except ImportError:
    import tomllib as _toml  # type: ignore[no-redef]  # Python 3.11+

from config_schema import _reject_removed_fields, _validate_provider_structure
from exceptions import ConfigError
from model_registry import CatalogDocument
from model_settings import ModelSettings, ModelSettingsError
from process_lock import PidFileLock
from xdg import XDGPaths, xdg_paths

logger = logging.getLogger("migrate_models")

# Connection types accepted by ``[providers.<id>] type`` (config_schema).
_CONNECTION_TYPES = ("openai", "openrouter", "anthropic", "google", "ollama")
_ENTRY_FIELDS = frozenset({"name", "provider", "api_key", "base_url", "request_timeout", "model"})
_TUNING_REASON = "per-model generation tuning is not migrated; requests use provider defaults"
_RETRY_REASON = "per-model retries are removed; configure [agent] max_retries/retry_delay instead"
_DISCARDED_FIELDS = {
    "max_tokens": _TUNING_REASON,
    "temperature": _TUNING_REASON,
    "top_p": _TUNING_REASON,
    "vision": "vision capability comes from provider discovery and is never promoted from configuration",
    "aliases": "aliases are not supported; select models by canonical ID with /models",
    "max_retries": _RETRY_REASON,
    "retry_delay": _RETRY_REASON,
}
_AGENT_SELECTORS = ("default_model", "background_model", "fallback_model", "fallback_models")
_PLACEHOLDER = re.compile(
    r"(?i)(your[-_ ]?(api[-_ ]?)?key|<[^>]*>|^x{3,}$|changeme|change[-_ ]me|replace[-_ ]?me|\.\.\.)"
)
_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\]\[]+?)\s*\]\]?\s*(#.*)?$")
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_LOCAL_OLLAMA_MESSAGE = (
    "local Ollama is not supported: add an explicit cloud connection "
    '(type = "ollama", base_url = "https://ollama.com", api_key) yourself; '
    "a model name ending in :cloud is not treated as one"
)


class MigrationInputError(ValueError):
    """The input could not be read as TOML; the message names no file content."""


@dataclass(frozen=True)
class EntryMapping:
    """Classification of one legacy ``[[models]]`` entry."""

    index: int
    name: str
    provider: str
    api_key: str | None = field(repr=False)
    base_url: str | None
    request_timeout: int | float | None
    model: str | None
    discarded: tuple[dict[str, str], ...] = ()
    stop_kind: str | None = None
    stop_message: str = ""

    @property
    def identity(self) -> tuple[str, str, str]:
        """The connection identity tuple ``(provider, base_url, api_key)``."""
        return (self.provider, self.base_url or "", self.api_key or "")


@dataclass(frozen=True)
class ConnectionPlan:
    """One proposed ``[providers.<id>]`` connection."""

    provider_id: str
    type: str
    api_key: str | None
    base_url: str | None
    request_timeout: int | float | None
    entries: tuple[str, ...]

    def fields(self) -> dict[str, Any]:
        """The connection's TOML fields, omitting absent ones."""
        values = {
            "type": self.type, "api_key": self.api_key,
            "base_url": self.base_url, "request_timeout": self.request_timeout,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class ConnectionSet:
    """Grouped connections plus the entries and findings produced by grouping."""

    connections: dict[str, ConnectionPlan]
    entry_ids: dict[int, str]
    stops: tuple[dict[str, Any], ...] = ()
    discarded: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class DefaultResolution:
    """Outcome of resolving the old effective default and background selector."""

    candidate: str | None
    stops: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    background_reason: str | None = None


@dataclass(frozen=True)
class MigrationPreview:
    """Everything the preview reports; nothing here has been applied."""

    proposed_mappings: tuple[dict[str, Any], ...]
    connections: tuple[ConnectionPlan, ...]
    unresolved_references: tuple[dict[str, Any], ...]
    discarded_values: tuple[dict[str, str], ...]
    stopped_choices: tuple[dict[str, Any], ...]
    candidate_config_text: str | None
    candidate_display_text: str | None
    candidate_default_model: str | None
    preservation: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    complete: bool = False


# ---------------------------------------------------------------------------
# Entry classification
# ---------------------------------------------------------------------------

def _is_cloud_ollama(base_url: str | None) -> bool:
    """Whether *base_url* is the official cloud Ollama origin."""
    if not base_url:
        return False
    try:
        parts = urlsplit(base_url)
        return (
            parts.scheme == "https" and parts.hostname == "ollama.com" and parts.port is None
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment and parts.path in {"", "/", "/api", "/api/"}
        )
    except ValueError:
        return False


def _is_positive_number(value: object) -> bool:
    """Whether *value* is a finite positive int/float (not a bool)."""
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value > 0
    )


def map_entry(entry: object, index: int) -> EntryMapping:
    """Classify one legacy entry: connection fields, discarded keys, or a stop."""
    if not isinstance(entry, dict):
        return EntryMapping(index, f"#{index + 1}", "", None, None, None, None,
                            stop_kind="unsupported_entry", stop_message="the entry is not a table")
    raw_name = entry.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name.strip() else f"#{index + 1}"
    discarded = tuple(
        {"key": f"models.{name}.{key}",
         "reason": _DISCARDED_FIELDS.get(key, "unrecognised legacy field; not migrated")}
        for key in entry
        if key not in _ENTRY_FIELDS
    )

    def unsupported(message: str) -> EntryMapping:
        return EntryMapping(index, name, str(entry.get("provider", "")), None, None, None, None,
                            discarded, "unsupported_entry", message)

    provider = entry.get("provider")
    api_key = entry.get("api_key")
    base_url = entry.get("base_url")
    timeout = entry.get("request_timeout")
    model = entry.get("model")
    if name != raw_name:
        return unsupported("the entry has no name")
    if provider not in _CONNECTION_TYPES:
        return unsupported(f"provider must be one of: {', '.join(_CONNECTION_TYPES)}")
    if api_key is not None and not isinstance(api_key, str):
        return unsupported("api_key must be a string credential or reference")
    if base_url is not None and not isinstance(base_url, str):
        return unsupported("base_url must be a string")
    if timeout is not None and not _is_positive_number(timeout):
        return unsupported("request_timeout must be a finite positive number")
    if model is not None and not isinstance(model, str):
        return unsupported("model must be a string")
    mapping = EntryMapping(index, name, provider, api_key, base_url, timeout, model, discarded)
    if provider == "ollama" and (not (api_key or "").strip() or not _is_cloud_ollama(base_url)):
        return EntryMapping(index, name, provider, api_key, base_url, timeout, model, discarded,
                            "local_ollama", _LOCAL_OLLAMA_MESSAGE)
    return mapping


# ---------------------------------------------------------------------------
# Connection grouping
# ---------------------------------------------------------------------------

def _sanitize_id(name: str, fallback: str) -> str:
    """A provider id derived from a legacy name (lowercase, ``[a-z0-9_-]``)."""
    sanitized = _ID_UNSAFE.sub("-", name.strip()).strip("-_").lower()[:120]
    return sanitized or fallback


def collect_connections(
    mappings: Sequence[EntryMapping], reserved_ids: Sequence[str] = (),
) -> ConnectionSet:
    """Group mapped entries by identity into connections with unique ids.

    Entries sharing a name are an ambiguity stop and are not grouped. Ids come
    from the first entry's name with a deterministic ``-N`` suffix on collision.
    """
    counts: dict[str, int] = {}
    for mapping in mappings:
        counts[mapping.name] = counts.get(mapping.name, 0) + 1
    stops: list[dict[str, Any]] = []
    for name in dict.fromkeys(m.name for m in mappings):
        if counts[name] > 1:
            positions = [f"#{m.index + 1}" for m in mappings if m.name == name]
            stops.append({
                "kind": "ambiguous_account", "reason": "duplicate_entry_name", "entries": [name],
                "candidates": positions,
                "message": f"{len(positions)} entries are named {name!r}; rename them so each maps once",
            })
    groups: dict[tuple[str, str, str], list[EntryMapping]] = {}
    for mapping in mappings:
        if mapping.stop_kind is None and counts[mapping.name] == 1:
            groups.setdefault(mapping.identity, []).append(mapping)

    used = set(reserved_ids)
    connections: dict[str, ConnectionPlan] = {}
    entry_ids: dict[int, str] = {}
    discarded: list[dict[str, str]] = []
    for members in groups.values():
        first = members[0]
        base = _sanitize_id(first.name, first.provider)
        provider_id, suffix = base, 2
        while provider_id in used:
            provider_id, suffix = f"{base}-{suffix}", suffix + 1
        used.add(provider_id)
        for member in members[1:]:
            if member.request_timeout is not None and member.request_timeout != first.request_timeout:
                discarded.append({
                    "key": f"models.{member.name}.request_timeout",
                    "reason": "differs within the merged connection; the first entry's value is kept",
                })
        connections[provider_id] = ConnectionPlan(
            provider_id, first.provider, None, first.base_url, first.request_timeout,
            tuple(m.name for m in members),
        )
        entry_ids.update({m.index: provider_id for m in members})
    return ConnectionSet(connections, entry_ids, tuple(stops), tuple(discarded))


# ---------------------------------------------------------------------------
# Default resolution
# ---------------------------------------------------------------------------

def _resolve_selector(
    value: str, mappings: Sequence[EntryMapping], entry_ids: dict[int, str],
) -> tuple[str, Any]:
    """Resolve a legacy model selector by exact match on entries' ``model``."""
    matches = [m for m in mappings if m.model == value]
    if not matches:
        return "missing", None
    if any(m.index not in entry_ids for m in matches):
        return "unmapped", [m.name for m in matches]
    ids = sorted({entry_ids[m.index] for m in matches})
    if len(ids) > 1:
        return "ambiguous", ([m.name for m in matches], ids)
    return "ok", f"{ids[0]}/{value}"


def resolve_effective_default(
    agent: dict[str, Any], mappings: Sequence[EntryMapping], entry_ids: dict[int, str],
) -> DefaultResolution:
    """Return the old effective default as a canonical ID, or why none is seeded."""
    stops: list[dict[str, Any]] = []
    notes: list[str] = []
    candidate: str | None = None
    if "default_model" in agent:
        value = agent["default_model"]
        if not isinstance(value, str) or not value:
            notes.append("agent.default_model is not a model name; no default is seeded")
        else:
            status, detail = _resolve_selector(value, mappings, entry_ids)
            if status == "ok":
                candidate = detail
            elif status == "ambiguous":
                entries, ids = detail
                stops.append({
                    "kind": "ambiguous_account", "reason": "default_model_ambiguous", "entries": entries,
                    "candidates": ids,
                    "message": f"agent.default_model {value!r} matches entries on different provider "
                               "accounts; choose one with /models after migrating",
                })
            elif status == "unmapped":
                notes.append(f"agent.default_model {value!r} belongs to a stopped entry; no default is seeded")
            else:
                notes.append(f"agent.default_model {value!r} matches no entry; no default is seeded")
    elif mappings:
        first = mappings[0]
        if first.index in entry_ids and first.model:
            candidate = f"{entry_ids[first.index]}/{first.model}"
            notes.append(f"no default was configured: the old startup used the first entry ({first.name!r})")
        else:
            notes.append("no default was configured and the first entry is not unambiguously mapped; "
                         "no default is inferred")

    background_reason = None
    if "background_model" in agent:
        value = agent["background_model"]
        status, detail = (
            _resolve_selector(value, mappings, entry_ids) if isinstance(value, str) else ("missing", None)
        )
        resolved = f"it resolved to {detail}" if status == "ok" else "it did not resolve unambiguously"
        background_reason = f"background selection is removed ({resolved}); sub-agents inherit Main"
    return DefaultResolution(candidate, tuple(stops), tuple(notes), background_reason)


# ---------------------------------------------------------------------------
# Candidate text and verification
# ---------------------------------------------------------------------------

def _bracket_balance(text: str) -> int:
    """Net ``[``/``]`` count outside strings and comments."""
    depth, quote, escaped = 0, "", False
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            break
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    return depth


def _strip_legacy(text: str) -> str:
    """Remove ``[[models]]`` blocks and the obsolete ``[agent]`` selectors line by line."""
    selector = re.compile(r"^\s*(%s)\s*=(.*)$" % "|".join(_AGENT_SELECTORS))
    kept: list[str] = []
    table: str | None = None
    in_models = False
    skip_depth = 0
    for line in text.splitlines(keepends=True):
        if skip_depth > 0:
            skip_depth += _bracket_balance(line)
            continue
        header = _HEADER.match(line) if line.lstrip().startswith("[") else None
        if header:
            table = header.group(2).strip()
            in_models = header.group(1) == "[[" and table == "models"
            if not in_models:
                kept.append(line)
            continue
        if in_models:
            continue
        match = selector.match(line) if table == "agent" else None
        if match:
            skip_depth = max(0, _bracket_balance(match.group(2)))
            continue
        kept.append(line)
    return "".join(kept)


def _toml_string(value: str) -> str:
    """A TOML basic string that parses back to exactly *value*."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _credential_kind(value: str | None) -> str:
    """Classify a credential value without resolving it."""
    if value is None:
        return "absent"
    if not value.strip():
        return "empty"
    if value.startswith("env:"):
        return "env"
    if value.startswith("sec:"):
        return "sec"
    if _PLACEHOLDER.search(value):
        return "placeholder"
    return "literal"


def _render_connection(plan: ConnectionPlan) -> str:
    """The ``[providers.<id>]`` section for *plan*."""
    lines = [f"[providers.{plan.provider_id}]", f"type = {_toml_string(plan.type)}"]
    if plan.base_url is not None:
        lines.append(f"base_url = {_toml_string(plan.base_url)}")
    if plan.request_timeout is not None:
        lines.append(f"request_timeout = {json.dumps(plan.request_timeout)}")
    return "\n".join(lines) + "\n"


def _assemble(stripped: str, connections: Sequence[ConnectionPlan]) -> str:
    """The stripped original followed by the proposed connection sections."""
    text = stripped.rstrip("\n") + "\n" if stripped.strip() else ""
    for plan in connections:
        text += "\n" + _render_connection(plan)
    return text


def _verify_preservation(
    original: dict[str, Any], candidate_text: str, connections: Sequence[ConnectionPlan],
) -> dict[str, Any]:
    """Parse the candidate and compare it with the expected, unrelated-preserving result."""
    expected = copy.deepcopy(original)
    expected.pop("models", None)
    agent = expected.get("agent")
    if isinstance(agent, dict):
        for key in _AGENT_SELECTORS:
            agent.pop(key, None)
    if connections:
        providers = expected.setdefault("providers", {})
        if isinstance(providers, dict):
            providers.update({plan.provider_id: plan.fields() for plan in connections})
    compared = sorted(key for key in expected if key != "providers")
    try:
        candidate = _toml.loads(candidate_text)
    except (_toml.TOMLDecodeError, UnicodeDecodeError):
        return {"verified": False, "compared_sections": compared, "differing_sections": ["<unparseable>"]}
    differing = sorted(
        key for key in set(expected) | set(candidate) if expected.get(key) != candidate.get(key)
    )
    return {"verified": not differing, "compared_sections": compared, "differing_sections": differing}


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def _parse(text: str, label: str) -> dict[str, Any]:
    """Parse TOML text, reporting only a line number on failure."""
    try:
        return _toml.loads(text)
    except _toml.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        where = f"line {line}" if line else "a syntax error"
        raise MigrationInputError(f"{label} is not valid TOML ({where})") from None


def _job_fallbacks(scheduler: dict[str, Any]) -> list[dict[str, str]]:
    """Report per-job ``fallback_models`` without touching the job file."""
    jobs = scheduler.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [
        {"key": f"jobs.{tag}.fallback_models",
         "reason": "ignored with a runtime warning when the job runs; the scheduler file is not "
                   "rewritten and no job converter exists (remove it manually)"}
        for tag, job in jobs.items()
        if isinstance(job, dict) and "fallback_models" in job
    ]


def preview_migration(config_text: str, scheduler_text: str | None = None) -> MigrationPreview:
    """Build the full migration preview from configuration (and job file) text."""
    parsed = _parse(config_text, "configuration")
    scheduler = _parse(scheduler_text, "scheduler configuration") if scheduler_text is not None else {}
    notes = ["Preview only: no file was written; apply a complete preview with --agent-name NAME --apply."]
    job_discards = tuple(_job_fallbacks(scheduler))
    models = parsed.get("models")
    agent = parsed.get("agent") if isinstance(parsed.get("agent"), dict) else {}
    if models is None:
        if not any(key in agent for key in _AGENT_SELECTORS):
            notes.append("no legacy [[models]] entries or obsolete selectors: nothing to migrate")
            return MigrationPreview((), (), (), job_discards, (), None, None, None,
                                    {"verified": True, "compared_sections": [], "differing_sections": []},
                                    tuple(notes), complete=True)
        models = []
    if not isinstance(models, list):
        stop = {"kind": "unsupported_entry", "reason": "unsupported_layout", "entries": ["models"],
                "candidates": [], "message": "models must be [[models]] tables; convert it manually"}
        return MigrationPreview((), (), (), job_discards, (stop,), None, None, None, {}, tuple(notes))

    mappings = [map_entry(entry, index) for index, entry in enumerate(models)]
    existing = parsed.get("providers")
    connection_set = collect_connections(mappings, tuple(existing) if isinstance(existing, dict) else ())
    entry_ids = connection_set.entry_ids
    default = resolve_effective_default(agent, mappings, entry_ids)

    stops: list[dict[str, Any]] = [
        {"kind": m.stop_kind, "reason": m.stop_kind, "entries": [m.name], "candidates": [],
         "message": m.stop_message}
        for m in mappings if m.stop_kind is not None
    ]
    stops.extend(connection_set.stops)
    stops.extend(default.stops)
    for plan in connection_set.connections.values():
        if any(m.api_key is not None for m in mappings if m.index in entry_ids
               and entry_ids[m.index] == plan.provider_id):
            stops.append({
                "kind": "credential_choice", "reason": "legacy_per_model_credential",
                "entries": list(plan.entries), "candidates": [plan.provider_id],
                "message": "legacy per-model credential remains only in the original source; "
                           "choose the connection credential explicitly before using this proposal",
            })

    discarded: list[dict[str, str]] = [d for m in mappings for d in m.discarded]
    discarded.extend(connection_set.discarded)
    if "default_model" in agent:
        discarded.append({"key": "agent.default_model",
                          "reason": "replaced by the remembered Main selection (candidate default model)"})
    if default.background_reason is not None:
        discarded.append({"key": "agent.background_model", "reason": default.background_reason})
    if "fallback_models" in agent:
        discarded.append({"key": "agent.fallback_models",
                          "reason": "model fallback is removed; each run uses only its admitted model"})
    if "fallback_model" in agent:
        discarded.append({"key": "agent.fallback_model",
                          "reason": "model fallback is removed; each run uses only its admitted model"})
    discarded.extend(job_discards)

    plans = tuple(connection_set.connections.values())
    references = tuple(
        {"provider_id": entry_ids.get(m.index, m.name), "entries": [m.name], "field": "api_key",
         "kind": kind, "reference": m.api_key if kind in {"env", "sec"} else None}
        for m in mappings
        for kind in [_credential_kind(m.api_key)]
        if kind in {"env", "sec", "placeholder", "empty"}
    )
    mapped = tuple(
        {"entry": m.name, "provider_id": entry_ids[m.index], "type": m.provider,
         "canonical_model": f"{entry_ids[m.index]}/{m.model}" if m.model else None}
        for m in mappings if m.index in entry_ids
    )

    stripped = _strip_legacy(config_text)
    candidate = _assemble(stripped, plans)
    preservation = _verify_preservation(parsed, candidate, plans)
    display: str | None = candidate
    if not preservation["verified"]:
        notes.append("the bounded edit could not prove unrelated values unchanged; no candidate is produced")
        candidate = display = None
    if stops:
        notes.append("the original configuration is unchanged: stopped identities are excluded from the "
                     "candidate; credential-free identity proposals awaiting a credential choice "
                     "are not applicable")
    notes.extend(default.notes)
    candidate_default = default.candidate
    if any(stop["kind"] == "credential_choice" for stop in stops):
        candidate_default = None
        notes.append("credential choice pending: any proposed default is not yet applicable")
    complete = not stops and candidate is not None
    return MigrationPreview(
        mapped, plans, references, tuple(discarded), tuple(stops), candidate, display,
        candidate_default, preservation, tuple(notes), complete,
    )


def render_preview(preview: MigrationPreview) -> str:
    """Report migration findings; unrelated original tables are shown verbatim."""
    status = "complete" if preview.complete else "requires operator action"
    lines = ["Legacy model configuration migration - PREVIEW (no files written)", f"Status: {status}", ""]
    lines.append("Proposed connections:")
    lines.extend(f"  [providers.{p.provider_id}] type={p.type} entries: {', '.join(p.entries)}"
                 for p in preview.connections)
    if not preview.connections:
        lines.append("  (none)")
    lines.append("Proposed mappings:")
    lines.extend(f"  {m['entry']} -> {m['provider_id']} (model: {m['canonical_model'] or 'none'})"
                 for m in preview.proposed_mappings)
    lines.append(f"Candidate default model: {preview.candidate_default_model or 'none'}")
    lines.append("Unresolved references (preserved in original source, not transferred):")
    for ref in preview.unresolved_references:
        shown = ref["reference"] or f"{ref['kind']} value"
        lines.append(f"  {ref['provider_id']}.api_key: {ref['kind']} ({shown})")
    lines.append("Discarded values:")
    lines.extend(f"  {d['key']}: {d['reason']}" for d in preview.discarded_values)
    lines.append("Stopped - operator choice required:")
    for stop in preview.stopped_choices:
        candidates = f" (candidates: {', '.join(stop['candidates'])})" if stop["candidates"] else ""
        lines.append(f"  [{stop['kind']}] {', '.join(stop['entries'])}: {stop['message']}{candidates}")
    if preview.preservation:
        verified = "verified" if preview.preservation.get("verified") else "NOT verified"
        sections = ", ".join(preview.preservation.get("compared_sections", [])) or "none"
        lines.append(f"Unrelated sections ({sections}): {verified} by parsed comparison")
    lines.append("Notes:")
    lines.extend(f"  {note}" for note in preview.notes)
    if preview.candidate_display_text is not None:
        lines.extend(["", "Candidate configuration:", preview.candidate_display_text.rstrip("\n")])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Apply and recovery (task 10.2)
# ---------------------------------------------------------------------------

_AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MANIFEST_NAME = "models-migration.json"
_MANIFEST_VERSION = 1
_BACKUP_SUFFIX = ".migration.bak"
_PHASES = frozenset({
    "prepared", "writing_config", "config_written", "writing_settings", "settings_written",
    "interrupted_rollback", "completed",
})
_ITEM_KEYS = frozenset({"path", "original_sha256", "backup_path", "backup_sha256", "candidate_sha256"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNCHECKED = object()


class MigrationApplyError(Exception):
    """A safe, actionable apply/recovery refusal or failure (never file content)."""


@dataclass(frozen=True)
class _ApplyPlan:
    """Validated inputs for one apply; nothing has been written yet."""

    config_original: bytes
    config_candidate: bytes
    settings_original: bytes | None
    settings_document: CatalogDocument | None  # None: settings stay as they are
    seeded: str | None


def _sha256(data: bytes | None) -> str | None:
    """Hex SHA-256 of *data*, or None for an absent file."""
    return None if data is None else hashlib.sha256(data).hexdigest()


def _read_optional(path: Path) -> bytes | None:
    """File bytes, or None when the file does not exist (other errors raise)."""
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def _targets(paths: XDGPaths) -> dict[str, Path]:
    """The two protected files, in write order."""
    return {"config": paths.config_file, "settings": paths.models_config_file}


def _backup_path(target: Path) -> Path:
    """The retained backup location of *target*."""
    return target.with_name(target.name + _BACKUP_SUFFIX)


def _fsync_dir(directory: Path) -> None:
    """Make renames and unlinks in *directory* durable."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(path: str | Path) -> None:
    """Remove a temporary file, tolerating its absence."""
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("could not remove temporary file %s", path)


def _require_current(target: Path, expected: str | None) -> None:
    """Refuse to touch *target* unless it still has the expected content."""
    if _sha256(_read_optional(target)) != expected:
        raise MigrationApplyError(f"{target} changed during the migration and was left untouched")


def _write_new(path: Path, data: bytes) -> None:
    """Create an owner-only file with *data*, flushed to disk."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(target: Path, data: bytes, *, expected: object = _UNCHECKED) -> None:
    """Atomically install *data* at *target* (owner-only; file and directory fsynced).

    With *expected* the current content hash is checked immediately before the
    replacement, so an intervening edit is never overwritten.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    pending: str | None = temporary
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not _UNCHECKED:
            _require_current(target, expected if isinstance(expected, str) else None)
        os.replace(temporary, str(target))
        pending = None
        _fsync_dir(target.parent)
    finally:
        if pending is not None:
            _discard(pending)


def _encode(record: dict[str, Any]) -> bytes:
    """Serialize the recovery manifest deterministically."""
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _set_phase(manifest: Path, record: dict[str, Any], phase: str) -> None:
    """Durably record the next transaction phase."""
    record["phase"] = phase
    _publish(manifest, _encode(record))


def _mark_interrupted(manifest: Path, record: dict[str, Any]) -> None:
    """Durably mark the manifest as an interrupted rollback, in place.

    Rewriting the same inode (no rename) keeps every already-open descriptor
    on the authoritative record, so a ``completed`` phase can never remain on
    disk while a rollback is running. A crash mid-rewrite can only leave a
    corrupt record, which ``--recover`` refuses fail-closed.
    """
    record["phase"] = "interrupted_rollback"
    data = _encode(record)
    fd = os.open(str(manifest), os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_real_targets(paths: XDGPaths) -> None:
    """Refuse symlinked protected leaves (including dangling) before any write.

    Backups bind to the lexical XDG path while the manifest binds to the
    resolved one, and replacing a leaf would replace the link itself, so a
    symlinked config or settings file can never be migrated safely.
    """
    for key, target in _targets(paths).items():
        if target.is_symlink():
            raise MigrationApplyError(
                f"the {key} file {target} is a symlink; migration never follows or replaces "
                "symlinks. Replace the link with a real file and rerun."
            )


def _clear_manifest(manifest: Path) -> None:
    """Remove the active manifest once its transaction is resolved."""
    try:
        os.unlink(str(manifest))
    except FileNotFoundError:
        return
    _fsync_dir(manifest.parent)


def _classify(item: dict[str, Any], target: Path) -> str:
    """``original``, ``candidate`` (this migration's result) or ``unresolved``."""
    current = _sha256(_read_optional(target))
    if current == item["original_sha256"]:
        return "original"
    if current == item["candidate_sha256"]:
        return "candidate"
    return "unresolved"


def _verified_backup(item: dict[str, Any]) -> bytes:
    """The backup bytes, only if they still hash to the recorded original."""
    backup = item["backup_path"]
    data = _read_optional(Path(backup)) if backup else None
    if data is None or _sha256(data) != item["original_sha256"] or item["backup_sha256"] != item["original_sha256"]:
        raise MigrationApplyError(
            f"backup {backup} is missing or does not match the recorded original; nothing was restored"
        )
    return data


def _roll_back(record: dict[str, Any], targets: dict[str, Path]) -> list[str]:
    """Return own-result targets to their originals; report the unresolved ones.

    Every needed backup is verified before the first write. Files matching
    neither their original nor this migration's result are left untouched.
    """
    states = {key: _classify(record["targets"][key], target) for key, target in targets.items()}
    restore = [key for key, state in states.items() if state == "candidate"]
    backups = {
        key: _verified_backup(record["targets"][key])
        for key in restore if record["targets"][key]["original_sha256"] is not None
    }
    for key in restore:
        item, target = record["targets"][key], targets[key]
        if key in backups:
            _publish(target, backups[key], expected=item["candidate_sha256"])
        else:
            _require_current(target, item["candidate_sha256"])
            os.unlink(str(target))
            _fsync_dir(target.parent)
        if _sha256(_read_optional(target)) != item["original_sha256"]:
            raise MigrationApplyError(f"{target} did not verify after restoration")
    return [str(targets[key]) for key, state in states.items() if state == "unresolved"]


def _validate_candidate_config(candidate: dict[str, Any]) -> None:
    """Structural validation of the new config; never resolves env:/sec: values."""
    _validate_provider_structure(candidate)
    _reject_removed_fields(candidate)


def _plan_apply(paths: XDGPaths, preview: MigrationPreview, config_original: bytes) -> _ApplyPlan:
    """Validate everything needed for apply without writing anything."""
    if preview.candidate_config_text is None:
        raise MigrationApplyError("the preview produced no candidate configuration")
    candidate = preview.candidate_config_text.encode("utf-8")
    _validate_candidate_config(_toml.loads(preview.candidate_config_text))

    settings_path = paths.models_config_file
    before = _read_optional(settings_path)
    loaded = ModelSettings(settings_path, read_only=True).load()
    if _read_optional(settings_path) != before:
        raise MigrationApplyError(f"managed settings {settings_path} changed while being read; rerun")
    if loaded.status == "corrupt":
        raise MigrationApplyError(
            f"managed settings {settings_path} are not a valid catalog document; "
            "repair or move the file aside, then rerun"
        )
    seed = preview.candidate_default_model
    document: CatalogDocument | None = None
    if loaded.status == "missing":
        if before is not None:
            raise MigrationApplyError(f"managed settings {settings_path} changed while being read; rerun")
        if seed is not None:
            document = CatalogDocument(1, 1, seed, (), ())
    else:
        if before is None or loaded.document is None:
            raise MigrationApplyError(f"managed settings {settings_path} changed while being read; rerun")
        if seed is not None and loaded.document.default_model is None:
            document = replace(loaded.document, default_model=seed, revision=loaded.document.revision + 1)

    for target, original in ((paths.config_file, config_original), (settings_path, before)):
        if original is None:
            continue
        existing = _read_optional(_backup_path(target))
        if existing is not None and existing != original:
            raise MigrationApplyError(
                f"backup {_backup_path(target)} already exists with other content; move it aside and rerun"
            )
    return _ApplyPlan(config_original, candidate, before, document, seed if document is not None else None)


def _stage(plan: _ApplyPlan, stage_dir: Path, targets: dict[str, Path]) -> tuple[dict[str, Path | None], dict[str, str | None]]:
    """Write and re-validate both staged results; return stage paths and candidate hashes."""
    config_stage = stage_dir / targets["config"].name
    _write_new(config_stage, plan.config_candidate)
    staged = _read_optional(config_stage)
    try:
        if staged is None or _toml.loads(staged.decode("utf-8")) != _toml.loads(plan.config_candidate.decode("utf-8")):
            raise MigrationApplyError("the staged configuration did not read back identically")
    except (UnicodeDecodeError, _toml.TOMLDecodeError):
        raise MigrationApplyError("the staged configuration is not valid TOML") from None
    stages: dict[str, Path | None] = {"config": config_stage, "settings": None}
    settings_candidate = plan.settings_original
    if plan.settings_document is not None:
        settings_stage = stage_dir / targets["settings"].name
        ModelSettings(settings_stage).write(plan.settings_document)
        reloaded = ModelSettings(settings_stage, read_only=True).load()
        if reloaded.status != "valid" or reloaded.document != plan.settings_document:
            raise MigrationApplyError("the staged managed settings did not reload as the expected document")
        stages["settings"] = settings_stage
        settings_candidate = _read_optional(settings_stage)
    return stages, {"config": _sha256(staged), "settings": _sha256(settings_candidate)}


def _new_record(
    agent_name: str, targets: dict[str, Path], originals: dict[str, bytes | None],
    candidates: dict[str, str | None],
) -> dict[str, Any]:
    """The versioned recovery manifest bound to this agent's absolute targets."""
    items = {}
    for key, target in targets.items():
        resolved = target.resolve()
        original = _sha256(originals[key])
        items[key] = {
            "path": str(resolved),
            "original_sha256": original,
            "backup_path": None if original is None else str(_backup_path(resolved)),
            "backup_sha256": original,
            "candidate_sha256": candidates[key],
        }
    return {"version": _MANIFEST_VERSION, "agent_name": agent_name, "phase": "prepared", "targets": items}


def _load_manifest(manifest: Path, agent_name: str, targets: dict[str, Path]) -> dict[str, Any]:
    """Read the active manifest, failing closed on anything unexpected."""
    with open(manifest, "rb") as stream:
        content = stream.read()
    try:
        record = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MigrationApplyError("the recovery record is not valid JSON") from None
    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != _MANIFEST_VERSION:
        raise MigrationApplyError("the recovery record has an unsupported version")
    if record.get("agent_name") != agent_name:
        raise MigrationApplyError("the recovery record belongs to a different agent")
    if record.get("phase") not in _PHASES:
        raise MigrationApplyError("the recovery record has an unknown phase")
    items = record.get("targets")
    if not isinstance(items, dict) or set(items) != set(targets):
        raise MigrationApplyError("the recovery record names unexpected targets")
    for key, target in targets.items():
        item = items[key]
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            raise MigrationApplyError(f"the recovery record entry for {key} is malformed")
        resolved = target.resolve()
        if item["path"] != str(resolved):
            raise MigrationApplyError(f"the recovery record path for {key} is not this agent's file")
        hashes = (item["original_sha256"], item["candidate_sha256"], item["backup_sha256"])
        if not all(value is None or (isinstance(value, str) and _SHA256.fullmatch(value)) for value in hashes):
            raise MigrationApplyError(f"the recovery record hashes for {key} are malformed")
        original = item["original_sha256"]
        expected_backup = (None, None) if original is None else (str(_backup_path(resolved)), original)
        if (item["backup_path"], item["backup_sha256"]) != expected_backup:
            raise MigrationApplyError(f"the recovery record backup for {key} is not the expected file")
    return record


def _abort(
    exc: BaseException, record: dict[str, Any] | None, manifest: Path, targets: dict[str, Path],
) -> int:
    """Roll back after an in-process failure; never touch operator edits."""
    logger.error("Apply failed: %s", exc)
    if record is None:
        logger.error("No configuration or settings file was changed.")
        return 1
    if record["phase"] == "completed":
        # A completed record could stay authoritative over the mixed state an
        # interrupted rollback leaves behind; durably mark the rollback first.
        try:
            _mark_interrupted(manifest, record)
        except OSError as mark_exc:
            logger.error(
                "Automatic rollback was not started (%s): the completed recovery record %s "
                "could not be marked. Run with --recover; the record was kept.",
                mark_exc, manifest,
            )
            return 1
    try:
        unresolved = _roll_back(record, targets)
    except (OSError, MigrationApplyError) as rollback_exc:
        logger.error("Automatic rollback stopped (%s); the recovery record %s was kept. "
                     "Fix the cause, then run with --recover.", rollback_exc, manifest)
        return 1
    if unresolved:
        logger.error("Left untouched because they changed during the migration: %s. Review them, then "
                     "run with --recover; the recovery record %s was kept.", ", ".join(unresolved), manifest)
        return 1
    try:
        _clear_manifest(manifest)
    except OSError:
        logger.warning("Could not remove the recovery record %s; run with --recover to clear it.", manifest)
    logger.error("Rolled back: the original files are in place; backups were kept.")
    return 1


def _execute(paths: XDGPaths, agent_name: str, plan: _ApplyPlan) -> int:
    """Stage, back up, record and replace both files; roll back on failure."""
    manifest = paths.config_home / _MANIFEST_NAME
    targets = _targets(paths)
    originals = {"config": plan.config_original, "settings": plan.settings_original}
    record: dict[str, Any] | None = None
    stage_dir: Path | None = None
    try:
        try:
            stage_dir = Path(tempfile.mkdtemp(prefix=".models-migration-stage.", dir=paths.config_home))
            stages, candidates = _stage(plan, stage_dir, targets)
            for key, data in originals.items():
                if data is not None:
                    backup = _backup_path(targets[key])
                    _publish(backup, data)
                    if _sha256(_read_optional(backup)) != _sha256(data):
                        raise MigrationApplyError(f"backup {backup} did not verify")
            # Set before publishing: once a manifest may exist, failures roll back by hash.
            record = _new_record(agent_name, targets, originals, candidates)
            _publish(manifest, _encode(record))
            for key, target in targets.items():
                item = record["targets"][key]
                stage = stages[key]
                if item["candidate_sha256"] == item["original_sha256"] or stage is None:
                    continue
                _set_phase(manifest, record, f"writing_{key}")
                _require_current(target, item["original_sha256"])
                os.replace(str(stage), str(target))
                _fsync_dir(target.parent)
                _set_phase(manifest, record, f"{key}_written")
            for key, target in targets.items():
                if _sha256(_read_optional(target)) != record["targets"][key]["candidate_sha256"]:
                    raise MigrationApplyError(f"{target} does not match the staged result after writing")
            _set_phase(manifest, record, "completed")
        except (OSError, ModelSettingsError, MigrationApplyError) as exc:
            return _abort(exc, record, manifest, targets)
        try:
            _clear_manifest(manifest)
        except OSError:
            logger.warning("Migration completed, but the recovery record %s could not be removed; "
                           "run with --recover to clear it.", manifest)
    finally:
        if stage_dir is not None:
            _remove_stage(stage_dir)
    return 0


def _remove_stage(stage_dir: Path) -> None:
    """Remove the private staging directory and anything left in it."""
    try:
        for name in os.listdir(stage_dir):
            _discard(stage_dir / name)
        os.rmdir(stage_dir)
    except OSError:
        logger.warning("could not remove staging directory %s", stage_dir)


def _apply(paths: XDGPaths, agent_name: str, scheduler_path: str) -> int:
    """Apply a complete preview under the agent lock; refusals precede any write."""
    manifest = paths.config_home / _MANIFEST_NAME
    if os.path.lexists(manifest):
        logger.error("An earlier migration is pending (%s); run with --agent-name %s --recover first. "
                     "Nothing was changed.", manifest, agent_name)
        return 1
    try:
        _require_real_targets(paths)
    except MigrationApplyError as exc:
        logger.error("Apply refused before any write: %s", exc)
        return 1
    config_text, preview = _load_preview(str(paths.config_file), scheduler_path)
    if preview.complete and preview.candidate_config_text is None:
        print(f"Nothing to migrate in {paths.config_file}; no file was changed.")
        return 0
    if not preview.complete or preview.candidate_config_text is None:
        print(render_preview(preview), end="")
        logger.error("Apply refused: the preview requires operator action (see 'Stopped' and 'Notes'). "
                     "Nothing was changed.")
        return 1
    try:
        plan = _plan_apply(paths, preview, config_text.encode("utf-8"))
    except (ConfigError, ModelSettingsError, MigrationApplyError, OSError) as exc:
        logger.error("Apply refused before any write: %s", exc)
        return 1
    code = _execute(paths, agent_name, plan)
    if code == 0:
        _report_applied(paths, preview, plan)
    return code


def _report_applied(paths: XDGPaths, preview: MigrationPreview, plan: _ApplyPlan) -> None:
    """Print what the successful apply changed and discarded."""
    lines = [f"Applied legacy model migration to {paths.config_file} "
             f"(backup {_backup_path(paths.config_file)})."]
    if plan.settings_document is None:
        lines.append(f"Managed settings {paths.models_config_file} unchanged.")
    else:
        lines.append(f"Managed settings {paths.models_config_file}: legacy default_model field set "
                     f"to {plan.seeded}. This does NOT set the runtime model selection; choose a model "
                     "explicitly with /models after starting the agent.")
    if plan.settings_original is not None:
        lines.append(f"Managed settings backup: {_backup_path(paths.models_config_file)}.")
    lines.append("Discarded values (not promoted):")
    lines.extend(f"  {d['key']}: {d['reason']}" for d in preview.discarded_values)
    print("\n".join(lines))


def _refuse_unresolved(
    record: dict[str, Any], targets: dict[str, Path], manifest: Path, reason: str,
) -> int:
    """Fail closed on a state only the operator can resolve, keeping evidence."""
    states = {
        key: _classify(record["targets"][key], target) for key, target in targets.items()
    }
    detail = ", ".join(f"{key} is at its {state}" for key, state in states.items())
    logger.error(
        "Recovery refused (%s): the state is unresolved — %s. Resolve the files manually "
        "(a verified backup sits beside each one), then rerun with --recover; "
        "the recovery record %s was kept.", reason, detail, manifest,
    )
    return 1


def _recover(paths: XDGPaths, agent_name: str) -> int:
    """Resolve an interrupted apply by content hash, never trusting the phase alone."""
    manifest = paths.config_home / _MANIFEST_NAME
    if not os.path.lexists(manifest):
        print("No pending model migration; nothing to recover.")
        return 0
    targets = _targets(paths)
    try:
        record = _load_manifest(manifest, agent_name, targets)
        states = {
            key: _classify(record["targets"][key], target) for key, target in targets.items()
        }
        if record["phase"] == "completed":
            if any(state != "candidate" for state in states.values()):
                # Post-rollback residue must never be cleared as committed.
                return _refuse_unresolved(
                    record, targets, manifest,
                    "the completed recovery record does not match the current files",
                )
            _clear_manifest(manifest)
            print("The migration had completed; nothing was rolled back and the recovery record was cleared.")
            return 0
        if record["phase"] == "interrupted_rollback" and "original" in states.values():
            # Some files were already restored when the rollback was interrupted;
            # the residue is not something recovery should finish silently.
            return _refuse_unresolved(
                record, targets, manifest, "an earlier rollback was interrupted part-way",
            )
        unresolved = _roll_back(record, targets)
    except (OSError, MigrationApplyError) as exc:
        logger.error("Recovery refused: %s. The recovery record %s was kept.", exc, manifest)
        return 1
    if unresolved:
        logger.error("Left untouched (matches neither its original nor this migration's result): %s. "
                     "Resolve it manually, then rerun --recover; the recovery record %s was kept.",
                     ", ".join(unresolved), manifest)
        return 1
    try:
        _clear_manifest(manifest)
    except OSError as exc:
        logger.error("Files are restored, but the recovery record %s could not be removed (%s).", manifest, exc)
        return 1
    print("Recovered: files written by the interrupted migration were restored; backups were kept.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_text(path: str, label: str) -> str:
    """Read *path* as UTF-8 or exit 1 with an actionable message (no content)."""
    try:
        with open(path, "rb") as stream:
            return stream.read().decode("utf-8")
    except FileNotFoundError:
        logger.error("%s file not found: %s", label, path)
    except UnicodeDecodeError:
        logger.error("%s file %s is not valid UTF-8; fix its encoding and rerun", label, path)
    except OSError as exc:
        logger.error("%s file %s could not be read (%s); check the path and permissions",
                     label, path, type(exc).__name__)
    sys.exit(1)


def _load_preview(config_path: str, scheduler_path: str) -> tuple[str, MigrationPreview]:
    """Read the inputs and build the preview, or exit 1 naming the bad file."""
    config_text = _read_text(config_path, "Config")
    scheduler_text = _read_text(scheduler_path, "Scheduler config") if scheduler_path else None
    try:
        return config_text, preview_migration(config_text, scheduler_text)
    except MigrationInputError as exc:
        source = config_path if "scheduler" not in str(exc) else scheduler_path
        logger.error("%s: %s; fix the syntax and rerun", source, exc)
        sys.exit(1)


def main(argv: Sequence[str] | None = None) -> None:
    """Preview (default), ``--apply`` or ``--recover``; exit 0 on success."""
    parser = argparse.ArgumentParser(
        description="Preview migrating legacy [[models]] configuration to [providers.<id>] connections.",
        epilog="Without --apply/--recover nothing is written. --apply and --recover need "
               "--agent-name and refuse while the agent is running.",
    )
    parser.add_argument("--agent-name", default=None,
                        help="Agent whose XDG config.toml/models-config.toml are migrated")
    parser.add_argument("--config", default=None,
                        help="Path to config.toml (default: the agent's XDG config with --agent-name, "
                             "else ./config.toml); with --apply/--recover it must be the agent's config")
    parser.add_argument("--scheduler-config", default="",
                        help="Optional scheduler.toml, read only to report per-job fallback_models")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="Apply a complete preview with backups and a recovery manifest")
    mode.add_argument("--recover", action="store_true",
                      help="Roll back or finish an interrupted --apply")
    args = parser.parse_args(argv)
    if (args.apply or args.recover) and not args.agent_name:
        parser.error("--apply and --recover require --agent-name NAME")
    if args.agent_name is not None and not _AGENT_NAME.fullmatch(args.agent_name):
        parser.error("--agent-name must match [A-Za-z0-9][A-Za-z0-9._-]*")
    paths = xdg_paths(args.agent_name) if args.agent_name else None
    config_path = args.config or (str(paths.config_file) if paths is not None else "config.toml")

    if paths is None or not (args.apply or args.recover):
        _, preview = _load_preview(config_path, args.scheduler_config)
        print(render_preview(preview), end="")
        sys.exit(0)

    if Path(config_path).resolve() != paths.config_file.resolve():
        logger.error("--config %s is not the config of agent %r (%s); nothing was changed",
                     config_path, args.agent_name, paths.config_file)
        sys.exit(1)
    with PidFileLock(str(paths.pid_file)):
        if args.apply:
            code = _apply(paths, args.agent_name, args.scheduler_config)
        else:
            code = _recover(paths, args.agent_name)
    sys.exit(code)


if __name__ == "__main__":
    main()
