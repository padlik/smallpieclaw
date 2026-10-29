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

``--apply`` is not available in this release; the original files always
remain unchanged.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlsplit

try:
    import tomli as _toml  # type: ignore[import]
except ImportError:
    import tomllib as _toml  # type: ignore[no-redef]  # Python 3.11+

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
    notes = ["Preview only: no file was written; --apply is not available in this release."]
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


def main(argv: Sequence[str] | None = None) -> None:
    """Print the migration preview; exit 0 when a report is produced."""
    parser = argparse.ArgumentParser(
        description="Preview migrating legacy [[models]] configuration to [providers.<id>] connections.",
        epilog="Preview only: nothing is written. --apply is not available in this release.",
    )
    parser.add_argument("--config", default="config.toml", help="Path to config.toml (default: config.toml)")
    parser.add_argument("--scheduler-config", default="",
                        help="Optional scheduler.toml, read only to report per-job fallback_models")
    args = parser.parse_args(argv)

    config_text = _read_text(args.config, "Config")
    scheduler_text = _read_text(args.scheduler_config, "Scheduler config") if args.scheduler_config else None
    try:
        preview = preview_migration(config_text, scheduler_text)
    except MigrationInputError as exc:
        source = args.config if "scheduler" not in str(exc) else args.scheduler_config
        logger.error("%s: %s; fix the syntax and rerun", source, exc)
        sys.exit(1)
    print(render_preview(preview), end="")
    sys.exit(0)


if __name__ == "__main__":
    main()
