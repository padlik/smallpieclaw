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
from typing import TYPE_CHECKING

from builtin_tools.patterns import _is_sensitive_path

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)


class FileTools:
    """Filesystem tool handlers; delegate confirmation staging to the owner façade."""

    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (Telegram bot API limit)

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    # ---- file_read ----

    def _exec_file_read(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}

        sensitive, reason = _is_sensitive_path(path)
        if sensitive:
            desc = f"Read file: <code>{path}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._owner._requires_confirmation("file_read", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

        return self._run_file_read(args, caller_tag=caller_tag)

    def _run_file_read(self, args: dict, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
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

    def _exec_file_diff(self, args: dict, caller_tag: str = "") -> dict:
        path_a = str(args.get("path_a", "")).strip()
        path_b = str(args.get("path_b", "")).strip()
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
        try:
            max_bytes = int(args.get("max_bytes", 200_000))
        except (TypeError, ValueError):
            return {"success": False, "output": "", "error": "file_diff: 'max_bytes' must be an integer.", "exit_code": -1}

        logger.info("Built-in file_diff: %s <-> %s (context=%d)", path_a, path_b, context_lines)

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
        return self._owner._requires_confirmation("file_write", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

    def _run_file_write(self, args: dict, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
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
        if not os.path.exists(path):
            return {"success": False, "output": "", "error": f"File not found: {path}", "exit_code": 1}

        # Validate the match before staging for confirmation
        try:
            with open(path, "r", errors="replace") as fh:
                content = fh.read()
        except PermissionError as exc:
            return {"success": False, "output": "", "error": f"Permission denied: {exc}", "exit_code": 1}
        except OSError as exc:
            return {"success": False, "output": "", "error": str(exc), "exit_code": 1}

        count = content.count(old_str)
        if count == 0:
            return {
                "success": False, "output": "",
                "error": (
                    f"file_patch: 'old_str' not found in {path}. "
                    "Make sure the text matches exactly (including whitespace and indentation). "
                    "Use file_read to inspect the file if needed."
                ),
                "exit_code": 1,
            }
        if occurrence == 1 and count > 1:
            return {
                "success": False, "output": "",
                "error": (
                    f"file_patch: 'old_str' matches {count} occurrences in {path} but occurrence=1 (ambiguous). "
                    "Include more surrounding context in 'old_str' to make it unique, "
                    "or set occurrence=0 to replace all."
                ),
                "exit_code": 1,
            }

        # Build a human-readable diff summary for the confirmation prompt
        old_lines = old_str.splitlines()
        new_lines = new_str.splitlines()
        removed = "\n".join(f"  - {ln}" for ln in old_lines[:8])
        added = "\n".join(f"  + {ln}" for ln in new_lines[:8])
        if len(old_lines) > 8:
            removed += f"\n  - … ({len(old_lines) - 8} more lines)"
        if len(new_lines) > 8:
            added += f"\n  + … ({len(new_lines) - 8} more lines)"
        replace_note = f" (replacing all {count} occurrences)" if occurrence == 0 else ""
        desc = (
            f"Patch file: <code>{path}</code>{replace_note}\n"
            f"{removed}\n{added}"
        )

        sensitive, _ = _is_sensitive_path(path)
        if sensitive:
            desc += "\n⚠️ Sensitive file"

        return self._owner._requires_confirmation("file_patch", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

    def _run_file_patch(self, args: dict, caller_tag: str = "") -> dict:
        path = str(args.get("path", "")).strip()
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

    def _exec_file_send(self, args: dict, caller_tag: str = "") -> dict:
        path = os.path.expanduser(str(args.get("path", "")).strip())
        caption = str(args.get("caption", "")).strip()
        if not path:
            return {"success": False, "output": "", "error": "No path provided.", "exit_code": -1}
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
