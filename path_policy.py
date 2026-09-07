"""Frozen three-tier path policy for file-tool access control and nsjail mounts.

The policy is constructed once at startup and is immutable afterwards.  It
categorizes every absolute, realpath'd path into one of three tiers:

* Tier 0 (PROHIBITED) — hardcoded system directories, derived agent-internal
  directories (XDG data/state/config home, vault/config files), plus any
  ``[security] prohibited_dirs`` from config.  These are always denied with a
  reason string and never reach the confirmation flow.

* Tier 1 (ALLOWED) — agent-controlled directories that are always allowed:
  workspace, downloads, ``/tmp/<agent>``, ``~/.<agent>/results`` (all rw),
  and ``~/.<agent>/skills`` (r).  The skills dir is read-only; writes there
  fall through to UNRECOGNISED.

* Tier 2 (ALLOWED) — operator-extended directories from ``[security]
  allowed_dirs`` (rw).  Conflicts with Tier 0 are warned once at startup;
  prohibited wins at classification time.

Any path not in any tier is UNRECOGNISED and may be promoted to allowed via
a per-prompt/session grant ledger (implemented elsewhere).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from exceptions import ConfigError

logger = logging.getLogger(__name__)


#: Prefix shared by agent-internal Tier 0 reason strings (data/state/config homes,
#: vault file, config file).  Kept as a constant so classification helpers and
#: reason-string construction stay in sync without changing emitted prose.
_AGENT_INTERNAL_PREFIX = "agent "


class ProhibitedCategory(str, Enum):
    """Coarse provenance of a Tier 0 reason string."""

    AGENT_STATE = "agent_state"      # agent state home → dedicated-tools suggestion
    AGENT_INTERNAL = "agent_internal"  # other agent-internal entries
    EXTERNAL = "external"            # hardcoded system / credential / config entries


#: Agent-internal reason strings.  Kept as constants so the prose stays byte-
#: identical between the policy builder and the module-level reverse map used by
#: the public ``prohibited_category`` seam.
_REASON_DATA_HOME = (
    f"{_AGENT_INTERNAL_PREFIX}data home — use memory tools and the dedicated data directory instead"
)
_REASON_STATE_HOME = (
    f"{_AGENT_INTERNAL_PREFIX}state home — use log_query, memory_*, or secret_get instead"
)
_REASON_CONFIG_HOME = (
    f"{_AGENT_INTERNAL_PREFIX}config home — configuration is managed outside the agent"
)
_REASON_VAULT_FILE = f"{_AGENT_INTERNAL_PREFIX}vault file"
_REASON_CONFIG_FILE = f"{_AGENT_INTERNAL_PREFIX}config file"
_REASON_HARDLINK_ALIAS = "prohibited file alias (hardlink)"


@dataclass(frozen=True)
class ProhibitedEntry:
    """Structured Tier 0 entry: human-readable reason + provenance category."""

    reason: str
    category: ProhibitedCategory


#: Module-level reverse map from reason string → category.  Pre-populated with
#: the hardcoded/agent-internal templates so the public ``prohibited_category``
#: function can resolve known literal strings without a policy instance; each
#: ``PathPolicy.create()`` adds its own resolved entries to the per-instance map.
_REASON_CATEGORY: dict[str, ProhibitedCategory] = {
    "system directory": ProhibitedCategory.EXTERNAL,
    "SSH credentials": ProhibitedCategory.EXTERNAL,
    "GPG keyring": ProhibitedCategory.EXTERNAL,
    "AWS credentials": ProhibitedCategory.EXTERNAL,
    "Kubernetes credentials": ProhibitedCategory.EXTERNAL,
    "Docker credentials": ProhibitedCategory.EXTERNAL,
    _REASON_DATA_HOME: ProhibitedCategory.AGENT_INTERNAL,
    _REASON_STATE_HOME: ProhibitedCategory.AGENT_STATE,
    _REASON_CONFIG_HOME: ProhibitedCategory.AGENT_INTERNAL,
    _REASON_VAULT_FILE: ProhibitedCategory.AGENT_INTERNAL,
    _REASON_CONFIG_FILE: ProhibitedCategory.AGENT_INTERNAL,
    _REASON_HARDLINK_ALIAS: ProhibitedCategory.EXTERNAL,
    "prohibited directory from config": ProhibitedCategory.EXTERNAL,
}


def prohibited_category(reason: str) -> ProhibitedCategory:
    """Classify a prohibited-path reason string into a provenance category.

    Looks up the exact reason string in the module-level reverse map.  This is
    the historical public seam; the canonical source of truth for a given policy
    is ``PathPolicy.prohibited_category``.
    """
    return _REASON_CATEGORY.get(reason, ProhibitedCategory.EXTERNAL)


#: Hardcoded Tier 0 entries mapping absolute path → human-readable reason.
_HARDCODED_PROHIBITED: dict[str, str] = {
    "/etc": "system directory",
    "/proc": "system directory",
    "/sys": "system directory",
    "/dev": "system directory",
    "/boot": "system directory",
    "/bin": "system directory",
    "/sbin": "system directory",
    "/lib": "system directory",
    "/lib64": "system directory",
    "/usr": "system directory",
    "/root": "system directory",
    "/var": "system directory",
    "/run": "system directory",
}

#: System directories that are always prohibited.  Kept as a sorted tuple so the
#: test suite can assert it is a superset of the legacy blocklists.  macOS exposes
#: some of these paths as symlinks into /private; the realpath'd variants are also
#: covered by Tier 0 at construction time, but the canonical names are listed
#: here for compatibility with the legacy tuples.
HARDCODED_SYSTEM_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "/etc",
            "/proc",
            "/sys",
            "/dev",
            "/boot",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/usr",
            "/root",
            "/var",
            "/run",
        )
    )
)

#: Credential homes resolved at construction time, mapping relative home path
#: to reason string.  These are expanded with ``~`` and realpath'd so a symlink
#: in the home directory cannot bypass the prohibition.
_CREDENTIAL_HOME_PATHS: dict[str, str] = {
    ".ssh": "SSH credentials",
    ".gnupg": "GPG keyring",
    ".aws": "AWS credentials",
    ".kube": "Kubernetes credentials",
    ".docker": "Docker credentials",
}


class PathVerdict(str, Enum):
    """Three-way classification result for a file path."""

    PROHIBITED = "prohibited"
    ALLOWED = "allowed"
    UNRECOGNISED = "unrecognised"


def is_contained(path: str, zone_dir: str) -> bool:
    """Return True when *path* is inside *zone_dir* using separator boundaries.

    Prevents sibling-prefix bypass: ``/srv/shared-evil`` is NOT inside
    ``/srv/shared``.  Uses ``os.path.normcase`` so case-insensitive filesystems
    match correctly.
    """
    norm_path = os.path.normcase(path)
    norm_zone = os.path.normcase(zone_dir)
    return norm_path == norm_zone or norm_path.startswith(
        norm_zone + os.path.normcase(os.sep)
    )


def _realify_list(
    raw: object,
    label: str,
    logger: logging.Logger | None,
    *,
    require_exists: bool = True,
) -> list[str]:
    """Validate and resolve a config list of directory paths.

    Each entry is expanded with ``~`` and realpath'd.  When *require_exists*
    is True (default), nonexistent entries are skipped with a warning.  When
    False the entry is retained so deny-list entries take effect as soon as
    the path appears; a warning is still emitted.  A non-list (or a list
    containing non-string items) raises ``ConfigError``.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(
            f"{label} must be a list of strings, got {type(raw).__name__}"
        )
    resolved: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(
                f"{label} must be a list of strings; found {type(item).__name__}: {item!r}"
            )
        expanded = os.path.realpath(os.path.expanduser(item))
        if not os.path.exists(expanded):
            if logger is not None:
                logger.warning(
                    "%s entry does not exist%s: %s",
                    label,
                    "" if not require_exists else ", skipping",
                    item,
                )
            if require_exists:
                continue
        resolved.append(expanded)
    return resolved


@dataclass(frozen=True, init=False)
class PathPolicy:
    """Immutable, session-static path classification policy.

    Construct via :meth:`PathPolicy.create` rather than the default
    constructor because several derived collections (resolved prohibited
    entries, inodes, conflict sets) need to be built at construction time.

    Attributes:
        agent_name: Agent identifier, used to derive ``/tmp/<agent>`` and XDG
            agent-scoped paths.
        workspace_dir: Absolute realpath of the workspace directory (Tier 1 rw).
        downloads_dir: Absolute realpath of the downloads directory (Tier 1 rw).
        tmp_dir: Absolute realpath of ``/tmp/<agent_name>`` (Tier 1 rw).
        skills_dir: Absolute realpath of ``~/.<agent>/skills`` (Tier 1 r).
        results_dir: Absolute realpath of ``~/.<agent>/results`` (Tier 1 rw).
        data_home: Absolute realpath of the agent XDG data home.
        state_home: Absolute realpath of the agent XDG state home.
        config_home: Absolute realpath of ``~/.config/<agent>``.
        vault_path: Absolute realpath of the vault file.
        config_path: Absolute realpath of the agent config file.
        prohibited_dirs: Raw config-appended prohibited directories (resolved
            entries live in the private ``_prohibited`` dict).
        allowed_dirs: Raw config-appended allowed directories (resolved entries
            live in the private ``_tier2`` tuple).
        conflict_skips: Allowed dirs that contain a prohibited path; the nsjail
            builder will skip mounting these.
        logger: Optional logger for construction warnings.
    """

    agent_name: str
    workspace_dir: str
    downloads_dir: str
    tmp_dir: str
    skills_dir: str
    results_dir: str
    data_home: str
    state_home: str
    config_home: str
    vault_path: str
    config_path: str
    prohibited_dirs: tuple[str, ...]
    allowed_dirs: tuple[str, ...]
    conflict_skips: frozenset[str]
    logger: logging.Logger | None

    # Internal derived state.  Kept private so callers only see the public API.
    _prohibited: dict[str, ProhibitedEntry]
    _tier1: tuple[tuple[str, str], ...]
    _tier2: tuple[str, ...]
    _prohibited_inodes: frozenset[tuple[int, int]]

    @classmethod
    def create(
        cls,
        *,
        agent_name: str,
        workspace_dir: str,
        downloads_dir: str,
        tmp_dir: str = "",
        skills_dir: str = "",
        results_dir: str = "",
        data_home: str = "",
        state_home: str = "",
        config_home: str = "",
        vault_path: str = "",
        config_path: str = "",
        prohibited_dirs: list[str] | None = None,
        allowed_dirs: list[str] | None = None,
        logger: logging.Logger | None = None,
    ) -> "PathPolicy":
        """Build a frozen policy from runtime paths and optional config lists."""
        if logger is None:
            logger = logging.getLogger(__name__)
        if not tmp_dir:
            tmp_dir = f"/tmp/{agent_name}"
        if not skills_dir:
            skills_dir = f"~/.{agent_name}/skills"
        if not results_dir:
            results_dir = f"~/.{agent_name}/results"

        # Resolve all directory parameters once.  Paths supplied by callers are
        # expected to be absolute already, but we normalise them defensively.
        resolved_workspace = os.path.realpath(os.path.expanduser(workspace_dir))
        resolved_downloads = os.path.realpath(os.path.expanduser(downloads_dir))
        resolved_tmp = os.path.realpath(os.path.expanduser(tmp_dir))
        resolved_skills = os.path.realpath(os.path.expanduser(skills_dir))
        resolved_results = os.path.realpath(os.path.expanduser(results_dir))
        resolved_data_home = _resolve_agent_dir(data_home, agent_name, "data")
        resolved_state_home = _resolve_agent_dir(state_home, agent_name, "state")
        resolved_config_home = _resolve_config_home(config_home, agent_name)
        resolved_vault = os.path.realpath(os.path.expanduser(vault_path)) if vault_path else ""
        resolved_config_path = os.path.realpath(os.path.expanduser(config_path)) if config_path else ""

        # Tier 0: hardcoded system directories.
        prohibited: dict[str, ProhibitedEntry] = {
            p: ProhibitedEntry(reason=reason, category=ProhibitedCategory.EXTERNAL)
            for p, reason in _HARDCODED_PROHIBITED.items()
        }

        # Tier 0: credential homes (resolve at construction).
        home = os.path.expanduser("~")
        for rel, reason in _CREDENTIAL_HOME_PATHS.items():
            expanded = os.path.realpath(os.path.join(home, rel))
            prohibited[expanded] = ProhibitedEntry(
                reason=reason, category=ProhibitedCategory.EXTERNAL,
            )

        # Tier 0: derived agent-internal directories.
        if resolved_data_home:
            prohibited[resolved_data_home] = ProhibitedEntry(
                reason=_REASON_DATA_HOME, category=ProhibitedCategory.AGENT_INTERNAL,
            )
        if resolved_state_home:
            prohibited[resolved_state_home] = ProhibitedEntry(
                reason=_REASON_STATE_HOME, category=ProhibitedCategory.AGENT_STATE,
            )
        if resolved_config_home:
            prohibited[resolved_config_home] = ProhibitedEntry(
                reason=_REASON_CONFIG_HOME, category=ProhibitedCategory.AGENT_INTERNAL,
            )

        # Tier 0: vault and config files.
        if resolved_vault:
            prohibited[resolved_vault] = ProhibitedEntry(
                reason=_REASON_VAULT_FILE, category=ProhibitedCategory.AGENT_INTERNAL,
            )
        if resolved_config_path:
            prohibited[resolved_config_path] = ProhibitedEntry(
                reason=_REASON_CONFIG_FILE, category=ProhibitedCategory.AGENT_INTERNAL,
            )

        # Tier 0: config-appended prohibited dirs.
        config_prohibited = _realify_list(prohibited_dirs, "prohibited_dirs", logger, require_exists=False)
        for p in config_prohibited:
            prohibited.setdefault(
                p,
                ProhibitedEntry(
                    reason="prohibited directory from config",
                    category=ProhibitedCategory.EXTERNAL,
                ),
            )

        # Tier 1: agent-controlled directories.  Order matches the design doc:
        # workspace, downloads, tmp, skills (r), results.
        tier1: list[tuple[str, str]] = [
            (resolved_workspace, "rw"),
            (resolved_downloads, "rw"),
            (resolved_tmp, "rw"),
            (resolved_skills, "r"),
            (resolved_results, "rw"),
        ]
        # Deduplicate while preserving order.
        seen_tier1: set[str] = set()
        deduped_tier1: list[tuple[str, str]] = []
        for path, mode in tier1:
            if path and path not in seen_tier1:
                seen_tier1.add(path)
                deduped_tier1.append((path, mode))

        # Tier 2: config-appended allowed dirs (rw-only).
        tier2 = _realify_list(allowed_dirs, "allowed_dirs", logger)

        # Conflict validation: warn once per conflicting pair and collect skips.
        conflict_skips: set[str] = set()
        for allowed in tier2:
            for prohibited_path in prohibited:
                if is_contained(prohibited_path, allowed) or is_contained(
                    allowed, prohibited_path
                ):
                    if logger is not None:
                        logger.warning(
                            "PathPolicy conflict: allowed dir %s overlaps prohibited path %s; "
                            "prohibited wins for the overlap and %s will not be mounted in nsjail",
                            allowed,
                            prohibited_path,
                            allowed,
                        )
                    conflict_skips.add(allowed)
                    break

        # Tier 1 conflict validation: fail-closed at startup.  Tier 1 directories
        # are hardcoded/derived from the agent's own configuration; if one overlaps
        # a prohibited entry the operator has misconfigured workspace/downloads/tmp
        # directories and must correct the configuration.
        for tier1_path, _mode in deduped_tier1:
            for prohibited_path in prohibited:
                if is_contained(prohibited_path, tier1_path) or is_contained(
                    tier1_path, prohibited_path
                ):
                    raise ConfigError(
                        f"PathPolicy conflict: Tier 1 directory {tier1_path} overlaps "
                        f"prohibited path {prohibited_path}. "
                        "Correct the agent configuration so workspace/downloads/tmp/skills/results "
                        "do not sit under a prohibited directory."
                    )

        # Inode-alias defense for prohibited files.
        prohibited_inodes = _collect_prohibited_inodes(
            (resolved_vault, resolved_config_path)
        )

        instance = cls.__new__(cls)
        object.__setattr__(instance, "agent_name", agent_name)
        object.__setattr__(instance, "workspace_dir", resolved_workspace)
        object.__setattr__(instance, "downloads_dir", resolved_downloads)
        object.__setattr__(instance, "tmp_dir", resolved_tmp)
        object.__setattr__(instance, "skills_dir", resolved_skills)
        object.__setattr__(instance, "results_dir", resolved_results)
        object.__setattr__(instance, "data_home", resolved_data_home)
        object.__setattr__(instance, "state_home", resolved_state_home)
        object.__setattr__(instance, "config_home", resolved_config_home)
        object.__setattr__(instance, "vault_path", resolved_vault)
        object.__setattr__(instance, "config_path", resolved_config_path)
        object.__setattr__(instance, "prohibited_dirs", tuple(prohibited_dirs or ()))
        object.__setattr__(instance, "allowed_dirs", tuple(allowed_dirs or ()))
        object.__setattr__(instance, "conflict_skips", frozenset(conflict_skips))
        object.__setattr__(instance, "logger", logger)
        object.__setattr__(instance, "_prohibited", prohibited)
        object.__setattr__(instance, "_tier1", tuple(deduped_tier1))
        object.__setattr__(instance, "_tier2", tuple(tier2))
        object.__setattr__(instance, "_prohibited_inodes", prohibited_inodes)
        return instance

    def classify(self, realpath: str, operation: str) -> tuple[PathVerdict, str]:
        """Classify an already-resolved absolute path.

        Args:
            realpath: Absolute, realpath'd path.  Callers are expected to run
                ``os.path.realpath(os.path.expanduser(p))`` first.
            operation: ``"read"`` or ``"write"``.

        Returns:
            ``(verdict, mode_or_reason)``.  For ``ALLOWED`` the second element is
            ``"r"`` or ``"rw"``; for ``PROHIBITED`` it is the reason string;
            for ``UNRECOGNISED`` it is an empty string.
        """
        if operation not in {"read", "write"}:
            raise ValueError(f"operation must be 'read' or 'write', got {operation!r}")

        # (a) Inode-alias defense.
        if self._prohibited_inodes:
            try:
                st = os.stat(realpath)
                if (st.st_dev, st.st_ino) in self._prohibited_inodes:
                    return (PathVerdict.PROHIBITED, "prohibited file alias (hardlink)")
            except OSError:
                pass

        # (b) Tier 0: prohibited directories (prefix wins first).
        for prohibited_path, entry in self._prohibited.items():
            if is_contained(realpath, prohibited_path):
                return (PathVerdict.PROHIBITED, entry.reason)

        # (c) Tier 1: agent-controlled directories.
        for zone, mode in self._tier1:
            if is_contained(realpath, zone):
                if mode == "r" and operation == "write":
                    break
                return (PathVerdict.ALLOWED, mode)

        # (d) Tier 2: operator-allowed directories (rw-only).
        for zone in self._tier2:
            if is_contained(realpath, zone):
                return (PathVerdict.ALLOWED, "rw")

        # (e) Fallback.
        return (PathVerdict.UNRECOGNISED, "")

    def prohibited_category(self, realpath: str) -> ProhibitedCategory:
        """Return the stored provenance category for a prohibited *realpath*.

        The path must already be resolved.  This is the canonical replacement
        for parsing the reason string.
        """
        for prohibited_path, entry in self._prohibited.items():
            if is_contained(realpath, prohibited_path):
                return entry.category
        return ProhibitedCategory.EXTERNAL

    def tier1_entries(self) -> tuple[tuple[str, str], ...]:
        """Return the immutable Tier 1 entries as ``(path, mode)`` tuples."""
        return self._tier1

    def tier2_entries(self) -> tuple[str, ...]:
        """Return Tier 2 entries excluding those that contain prohibited paths."""
        return tuple(z for z in self._tier2 if z not in self.conflict_skips)


def _resolve_agent_dir(path: str, agent_name: str, kind: str) -> str:
    """Resolve an XDG agent-scoped directory (data or state home).

    If *path* is provided it is used verbatim (expanded/realpath'd).  Otherwise
    the directory is derived from the ``XDG_DATA_HOME``/``XDG_STATE_HOME``
    environment base, falling back to the XDG default base (``~/.local/share``
    / ``~/.local/state``) — mirroring ``xdg.py`` resolution.  The fallback
    must NOT be the agent dot-home ``~/.<agent>``: Tier 1 skills/results live
    there, and the Tier-1 conflict check (fail-closed on overlap with a
    prohibited path) would reject the policy at construction.
    """
    if path:
        return os.path.realpath(os.path.expanduser(path))
    default_base = "~/.local/share" if kind == "data" else "~/.local/state"
    xdg_base = os.environ.get(f"XDG_{kind.upper()}_HOME", "")
    base = os.path.expanduser(xdg_base) if xdg_base else os.path.expanduser(default_base)
    return os.path.realpath(os.path.join(base, agent_name))


def _resolve_config_home(config_home: str, agent_name: str) -> str:
    """Resolve the agent config home, falling back to ``~/.config/<agent>``."""
    if config_home:
        return os.path.realpath(os.path.expanduser(config_home))
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.realpath(os.path.join(base, agent_name))


def _collect_prohibited_inodes(
    paths: Iterable[str],
) -> frozenset[tuple[int, int]]:
    """Stat prohibited files at construction; hardlink aliases will be denied."""
    inodes: set[tuple[int, int]] = set()
    for p in paths:
        if not p:
            continue
        try:
            st = os.stat(p)
            inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    return frozenset(inodes)
