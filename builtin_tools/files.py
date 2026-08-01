"""Filesystem built-in tools: file_read, file_write, file_diff, file_patch, file_send.

Handler module: ``FileTools`` holds a live back-reference to the
``BuiltinExecutor`` façade (``owner``) and stages confirmation only through
``owner._requires_confirmation`` (Decision 8 seam constraint). No lifecycle
logging happens here — that stays on the façade. The ``builtin_executor`` import
is under ``TYPE_CHECKING`` only, so there is no runtime import cycle.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from typing import TYPE_CHECKING

from builtin_tools.access_control import ZoneClassification
from builtin_tools.patterns import _is_sensitive_path

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)

# Standard agentskills.io skill subdirectories for Tier 2 path substitution.
_SKILL_SUBDIRS = ("scripts", "assets", "references", "tests")
# Matches fenced code blocks (``` ... ```) and inline code spans (` ... `).
_CODE_SPAN_RE = re.compile(r"(```[^\n]*\n.*?```|`[^`\n]+`)", re.DOTALL)
# Matches any standard subdir at a path-component boundary, in a single pass.
_SKILL_SUBDIR_RE = re.compile(
    rf"(?<![/\w-])({'|'.join(re.escape(d) for d in _SKILL_SUBDIRS)})/"
)


def _subst_in_code_spans(text: str, pattern: re.Pattern, repl) -> str:
    """Apply a compiled-pattern substitution only inside code fences and inline code spans."""
    parts: list[str] = []
    last_end = 0
    for m in _CODE_SPAN_RE.finditer(text):
        parts.append(text[last_end:m.start()])
        parts.append(pattern.sub(repl, m.group(0)))
        last_end = m.end()
    parts.append(text[last_end:])
    return "".join(parts)


def _expand_skill_paths(content: str, skill_dir: str) -> str:
    """Substitute relative path references in SKILL.md content with absolute paths.

    Tier 1: ``./foo`` → ``<skill_dir>/foo`` globally (unambiguous in shell/path contexts).
    Tier 2: standard subdirs (scripts/, assets/, references/, tests/) at a path-component
    boundary, within fenced code blocks and inline code spans only.

    Replacements are passed as callables (not plain strings) so that any backslashes
    ``skill_dir`` contains are never interpreted as regex backreferences by ``re.sub``.
    """
    result = re.sub(r"(?<!\.)\./", lambda _m: skill_dir + "/", content)
    result = _subst_in_code_spans(
        result, _SKILL_SUBDIR_RE, lambda m: f"{skill_dir}/{m.group(1)}/"
    )
    return result


class FileTools:
    """Filesystem tool handlers; delegate confirmation staging to the owner façade."""

    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (Telegram bot API limit)

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    @property
    def _checker(self):
        """Return the TrustedZoneChecker from the owner, or None if not wired."""
        return getattr(self._owner, "trusted_zone_checker", None)

    def _request_grants(self):
        """Return a frozenset of current request grants, or empty frozenset."""
        gt = getattr(self._owner, "grant_tracker", None)
        return gt.snapshot() if gt is not None else frozenset()

    def _resolve_skill_paths(self, content: str, path: str) -> str:
        """Resolve relative paths in SKILL.md content using the skill registry."""
        registry = getattr(self._owner, "skill_registry", None)
        if registry is None:
            skill_dir = os.path.dirname(path)
        else:
            skill = next(
                (s for s in registry.all() if s.skill_md_path == path),
                None,
            )
            if skill is None:
                return content
            skill_dir = skill.path  # skill DIRECTORY (not skill_md_path)
        return _expand_skill_paths(content, skill_dir)

    # ---- file_read ----

    def _exec_file_read(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        checker = self._checker
        real_path = os.path.realpath(os.path.expanduser(path))
        args["_resolved_path"] = real_path
        if checker is not None:
            zone = checker.classify(path, operation="read", request_grants=self._request_grants())
            sensitive, reason = _is_sensitive_path(real_path)
            if zone == ZoneClassification.UNRECOGNISED:
                desc = f"Read file: <code>{path}</code>"
                if real_path != path:
                    desc += f"\n(→ <code>{real_path}</code>)"
                if sensitive:
                    desc += f"\n⚠️ Reason: {reason}"
                return self._owner._requires_confirmation(
                    "file_read", args, desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                    zone_path=real_path,
                )
            if sensitive:
                desc = f"Read file: <code>{path}</code>\n⚠️ Reason for confirmation: {reason}"
                if real_path != path:
                    desc += f"\n(→ <code>{real_path}</code>)"
                return self._owner._requires_confirmation(
                    "file_read", args, desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                )
            return self._run_file_read(args, caller_tag=caller_tag)

        # checker unwired: reads degrade to sensitive-only gate (writes fail closed)
        logger.error("Zone: trusted_zone_checker not wired — falling back to sensitive-only gate for file_read")
        sensitive, reason = _is_sensitive_path(real_path)
        if sensitive:
            desc = f"Read file: <code>{path}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._owner._requires_confirmation("file_read", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)
        return self._run_file_read(args, caller_tag=caller_tag)

    def _run_file_read(self, args: dict, caller_tag: str = "") -> dict:
        path = args.get("_resolved_path") or str(args.get("path", "")).strip()
        max_bytes = int(args.get("max_bytes", 50_000))
        offset = int(args.get("offset", 0))
        logger.info("Built-in file_read: %s (offset=%d, max=%d)", path, offset, max_bytes)
        try:
            if not os.path.exists(path):
                return {
                    "success": False,
                    "output": "",
                    "error": f"File not found: {path}",
                    "exit_code": 1,
                    "error_type": "file_not_found",
                    "recoverable": False,
                    "suggestion": "Check the file path or create the missing file.",
                }
            size = os.path.getsize(path)
            # Negative offset = from end of file (tail semantics)
            if offset < 0:
                offset = max(0, size + offset)
            with open(path, "r", errors="replace") as f:
                if offset:
                    f.seek(offset)
                content = f.read(max_bytes)
            truncated = size > offset + max_bytes
            note = f"\n[Showing {len(content)} of {size} bytes from offset {offset}]" if truncated else ""
            if os.path.basename(path) == "SKILL.md":
                content = self._resolve_skill_paths(content, path)
            return {
                "success": True,
                "output": content + note,
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except PermissionError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Permission denied: {exc}",
                "exit_code": 1,
                "error_type": "permission_denied",
                "recoverable": False,
                "suggestion": "Check file permissions or use sudo.",
            }
        except OSError as exc:
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "exit_code": 1,
                "error_type": "file_not_found" if "No such file" in str(exc) else "",
                "recoverable": False,
                "suggestion": "Check the file path or create the missing file." if "No such file" in str(exc) else "",
            }

    # ---- file_diff ----

    def _exec_file_diff(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path_a = os.path.expanduser(str(args.get("path_a", "")).strip())
        path_b = os.path.expanduser(str(args.get("path_b", "")).strip())
        if not path_a or not path_b:
            return {
                "success": False, "output": "",
                "error": "file_diff: both 'path_a' and 'path_b' are required.",
                "exit_code": -1,
            }
        try:
            context_lines = int(args.get("context_lines", 3))
        except (TypeError, ValueError):
            return {"success": False, "output": "", "error": "file_diff: 'context_lines' must be an integer.", "exit_code": -1}
        if context_lines < 0:
            context_lines = 0
        logger.info("Built-in file_diff: %s <-> %s (context=%d)", path_a, path_b, context_lines)

        checker = self._checker
        if checker is not None:
            real_path_a = os.path.realpath(os.path.expanduser(path_a))
            real_path_b = os.path.realpath(os.path.expanduser(path_b))
            args["_resolved_path_a"] = real_path_a
            args["_resolved_path_b"] = real_path_b
            grants = self._request_grants()
            zone_a = checker.classify(path_a, operation="read", request_grants=grants)
            zone_b = checker.classify(path_b, operation="read", request_grants=grants)
            sensitive_a, reason_a = _is_sensitive_path(real_path_a)
            sensitive_b, reason_b = _is_sensitive_path(real_path_b)

            # Prefer the actually-unrecognised path for zone_path so zone buttons grant the right dir
            unrecognised_path = None
            if zone_b == ZoneClassification.UNRECOGNISED:
                unrecognised_path = real_path_b
            elif zone_a == ZoneClassification.UNRECOGNISED:
                unrecognised_path = real_path_a

            needs_confirm = unrecognised_path is not None
            needs_sensitive_confirm = (
                not needs_confirm and (sensitive_a or sensitive_b)
            )

            if needs_confirm or needs_sensitive_confirm:
                diff_desc = f"Diff files: <code>{path_a}</code> ↔ <code>{path_b}</code>"
                if real_path_a != path_a:
                    diff_desc += f"\n(→ <code>{real_path_a}</code>)"
                if real_path_b != path_b:
                    diff_desc += f"\n(→ <code>{real_path_b}</code>)"
                if sensitive_a:
                    diff_desc += f"\n⚠️ {path_a}: {reason_a}"
                if sensitive_b:
                    diff_desc += f"\n⚠️ {path_b}: {reason_b}"
                return self._owner._requires_confirmation(
                    "file_diff", args, diff_desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                    zone_path=unrecognised_path or "",
                )
        else:
            # checker unwired: reads degrade to sensitive-only gate (writes fail closed)
            logger.error("Zone: trusted_zone_checker not wired — falling back to sensitive-only gate for file_diff")
            real_path_a = os.path.realpath(os.path.expanduser(path_a))
            real_path_b = os.path.realpath(os.path.expanduser(path_b))
            args["_resolved_path_a"] = real_path_a
            args["_resolved_path_b"] = real_path_b
            sensitive_a, reason_a = _is_sensitive_path(real_path_a)
            sensitive_b, reason_b = _is_sensitive_path(real_path_b)
            if sensitive_a or sensitive_b:
                diff_desc = f"Diff files: <code>{path_a}</code> ↔ <code>{path_b}</code>"
                if sensitive_a:
                    diff_desc += f"\n⚠️ {path_a}: {reason_a}"
                if sensitive_b:
                    diff_desc += f"\n⚠️ {path_b}: {reason_b}"
                return self._owner._requires_confirmation(
                    "file_diff", args, diff_desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                )

        # Zone-gated: now safe to access filesystem
        return self._run_file_diff(args, caller_tag=caller_tag)

    def _run_file_diff(self, args: dict, caller_tag: str = "") -> dict:
        """Execute file_diff after zone gate has passed."""
        path_a = args.get("_resolved_path_a") or os.path.expanduser(str(args.get("path_a", "")).strip())
        path_b = args.get("_resolved_path_b") or os.path.expanduser(str(args.get("path_b", "")).strip())
        try:
            context_lines = int(args.get("context_lines", 3))
        except (TypeError, ValueError):
            context_lines = 3
        if context_lines < 0:
            context_lines = 0
        try:
            max_bytes = int(args.get("max_bytes", 200_000))
        except (TypeError, ValueError):
            max_bytes = 200_000
        try:
            for p in (path_a, path_b):
                if not os.path.exists(p):
                    return {"success": False, "output": "", "error": f"File not found: {p}", "exit_code": 1}
            with open(path_a, "r", errors="replace") as f:
                a_text = f.read(max_bytes)
            with open(path_b, "r", errors="replace") as f:
                b_text = f.read(max_bytes)
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except OSError as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}
        a_lines = a_text.splitlines(keepends=True)
        b_lines = b_text.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=path_a, tofile=path_b,
            n=context_lines,
        ))
        if not diff:
            return {"success": True, "output": "Files are identical.", "error": "", "exit_code": 0}
        return {"success": True, "output": "".join(diff), "error": "", "exit_code": 0}

    # ---- file_write ----

    def _exec_file_write(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "w"))
        if mode not in ("w", "a"):
            mode = "w"
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        action = "append to" if mode == "a" else "overwrite"
        desc = f"{action.capitalize()} file: <code>{path}</code> ({len(content)} chars)"

        checker = self._checker
        if checker is not None:
            real_path = os.path.realpath(os.path.expanduser(path))
            args["_resolved_path"] = real_path
            if real_path != path:
                desc += f"\n(→ <code>{real_path}</code>)"
            zone = checker.classify(path, operation="write", request_grants=self._request_grants())
            sensitive, _ = _is_sensitive_path(real_path)
            if zone == ZoneClassification.UNRECOGNISED:
                if sensitive:
                    desc += "\n⚠️ Sensitive file"
                return self._owner._requires_confirmation(
                    "file_write", args, desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                    zone_path=real_path,
                )
            if sensitive:
                return self._owner._requires_confirmation(
                    "file_write", args, desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                )
            return self._run_file_write(args, caller_tag=caller_tag)

        # Fallback: no checker — always confirm
        logger.error("Zone: trusted_zone_checker not wired — always confirming file_write")
        return self._owner._requires_confirmation("file_write", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

    def _run_file_write(self, args: dict, caller_tag: str = "") -> dict:
        path = args.get("_resolved_path") or str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        mode = str(args.get("mode", "w"))
        if mode not in ("w", "a"):
            mode = "w"
        logger.info("Built-in file_write: %s (mode=%s, len=%d)", path, mode, len(content))
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, mode) as f:
                f.write(content)
            return {
                "success": True,
                "output": f"Written {len(content)} chars to {path}.",
                "error": "",
                "exit_code": 0,
                "error_type": "",
                "recoverable": False,
                "suggestion": "",
            }
        except PermissionError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Permission denied: {exc}",
                "exit_code": 1,
                "error_type": "permission_denied",
                "recoverable": False,
                "suggestion": "Check file permissions or use sudo.",
            }
        except OSError as exc:
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "exit_code": 1,
                "error_type": "file_not_found" if "No such file" in str(exc) else "",
                "recoverable": False,
                "suggestion": "Check the file path or create the missing file." if "No such file" in str(exc) else "",
            }

    # ---- file_patch ----

    def _exec_file_patch(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        old_str = str(args.get("old_str", ""))
        new_str = str(args.get("new_str", ""))
        try:
            occurrence = int(args.get("occurrence", 1))
        except (ValueError, TypeError):
            return {"success": False, "output": "", "error": "file_patch: 'occurrence' must be an integer.", "exit_code": -1}
        if occurrence < 0:
            return {"success": False, "output": "", "error": "file_patch: 'occurrence' must be >= 0 (0 = replace all).", "exit_code": -1}

        if not path:
            return {"success": False, "output": "", "error": "file_patch: 'path' is required.", "exit_code": -1}
        if not old_str:
            return {"success": False, "output": "", "error": "file_patch: 'old_str' is required.", "exit_code": -1}

        # FIX 2: realpath + zone classification BEFORE any file read (content oracle fix)
        real_path = os.path.realpath(os.path.expanduser(path))
        args["_resolved_path"] = real_path
        checker = self._checker
        zone = None
        if checker is not None:
            zone = checker.classify(path, operation="write", request_grants=self._request_grants())

        # Build confirmation description from args only — no file read before confirm
        old_lines = old_str.splitlines()
        new_lines = new_str.splitlines()
        removed = "\n".join(f"  - {ln}" for ln in old_lines[:8])
        added = "\n".join(f"  + {ln}" for ln in new_lines[:8])
        if len(old_lines) > 8:
            removed += f"\n  - … ({len(old_lines) - 8} more lines)"
        if len(new_lines) > 8:
            added += f"\n  + … ({len(new_lines) - 8} more lines)"
        replace_note = " (replacing all occurrences)" if occurrence == 0 else ""
        desc = (
            f"Patch file: <code>{path}</code>{replace_note}\n"
            f"{removed}\n{added}"
        )
        sensitive, _ = _is_sensitive_path(real_path)
        if sensitive:
            desc += "\n⚠️ Sensitive file"
        if checker is not None and real_path != path:
            desc += f"\n(→ <code>{real_path}</code>)"

        # FIX 2: stage confirmation before any file read for UNRECOGNISED zone
        if checker is not None:
            if zone == ZoneClassification.UNRECOGNISED:
                return self._owner._requires_confirmation(
                    "file_patch", args, desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                    zone_path=real_path,
                )
            if sensitive:
                return self._owner._requires_confirmation(
                    "file_patch", args, desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                )
            # Auto-allowed: TRUSTED, REQUEST_GRANT
            if not os.path.exists(path):
                return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}
            return self._run_file_patch(args, caller_tag=caller_tag)

        # No checker: check existence then always confirm
        if not os.path.exists(path):
            return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}
        return self._owner._requires_confirmation("file_patch", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

    def _run_file_patch(self, args: dict, caller_tag: str = "") -> dict:
        path = args.get("_resolved_path") or str(args.get("path", "")).strip()
        old_str = str(args.get("old_str", ""))
        new_str = str(args.get("new_str", ""))
        try:
            occurrence = int(args.get("occurrence", 1))
        except (ValueError, TypeError):
            occurrence = 1
        logger.info("Built-in file_patch: %s (occurrence=%d)", path, occurrence)
        try:
            with open(path, "r", errors="replace") as fh:
                content = fh.read()
            if occurrence == 0:
                count = content.count(old_str)
                if count == 0:
                    return {
                        "success": False, "output": "",
                        "error": f"file_patch: 'old_str' not found in {path} at execution time.",
                        "exit_code": 1,
                    }
                patched = content.replace(old_str, new_str)
                n_replaced = count
            else:
                # Find the Nth occurrence (occurrence >= 1)
                pos = 0
                idx = -1
                for _ in range(occurrence):
                    idx = content.find(old_str, pos)
                    if idx == -1:
                        return {
                            "success": False, "output": "",
                            "error": (
                                f"file_patch: occurrence {occurrence} of 'old_str' not found in {path} "
                                "at execution time (file may have changed after validation)."
                            ),
                            "exit_code": 1,
                        }
                    pos = idx + 1
                patched = content[:idx] + new_str + content[idx + len(old_str):]
                n_replaced = 1
            with open(path, "w") as fh:
                fh.write(patched)
            return {
                "success": True,
                "output": f"Patched {path}: replaced {n_replaced} occurrence(s).",
                "error": "", "exit_code": 0,
            }
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except OSError as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

    # ---- file_send ----

    def _exec_file_send(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = os.path.expanduser(str(args.get("path", "")).strip())
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        checker = self._checker
        real_path = os.path.realpath(path)
        args["_resolved_path"] = real_path
        if checker is not None:
            zone = checker.classify(path, operation="read", request_grants=self._request_grants())
            sensitive, reason = _is_sensitive_path(real_path)
            if zone == ZoneClassification.UNRECOGNISED:
                send_desc = f"Send file: <code>{path}</code>"
                if real_path != path:
                    send_desc += f"\n(→ <code>{real_path}</code>)"
                if sensitive:
                    send_desc += f"\n⚠️ {reason}"
                return self._owner._requires_confirmation(
                    "file_send", args, send_desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                    zone_path=real_path,
                )
            if sensitive:
                send_desc = f"Send file: <code>{path}</code>\n⚠️ {reason}"
                if real_path != path:
                    send_desc += f"\n(→ <code>{real_path}</code>)"
                return self._owner._requires_confirmation(
                    "file_send", args, send_desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                )
        else:
            # checker unwired: reads degrade to sensitive-only gate (writes fail closed)
            logger.error("Zone: trusted_zone_checker not wired — falling back to sensitive-only gate for file_send")
            sensitive, reason = _is_sensitive_path(real_path)
            if sensitive:
                send_desc = f"Send file: <code>{path}</code>\n⚠️ {reason}"
                return self._owner._requires_confirmation(
                    "file_send", args, send_desc,
                    caller_depth=caller_depth, caller_tag=caller_tag,
                )

        # Zone-gated: now safe to access filesystem
        return self._run_file_send(args, caller_tag=caller_tag)

    def _run_file_send(self, args: dict, caller_tag: str = "") -> dict:
        """Execute file_send after zone gate has passed."""
        path = args.get("_resolved_path") or os.path.expanduser(str(args.get("path", "")).strip())
        caption = str(args.get("caption", "")).strip()
        if not os.path.exists(path):
            return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}
        if not os.path.isfile(path):
            return {"success": False, "output": "", "error": f"Not a file: {path}", "exit_code": 1}
        size = os.path.getsize(path)
        if size > self._MAX_FILE_SIZE:
            return {
                "success": False, "output": "",
                "error": f"File too large ({size // 1024 // 1024} MB). Max 50 MB.", "exit_code": 1,
            }
        logger.info("Built-in file_send: %s (%d bytes)", path, size)
        return {
            "success": True,
            "output": f"Sending {os.path.basename(path)} to chat…",
            "error": "",
            "exit_code": 0,
            "send_file": path,
            "caption": caption,
        }
