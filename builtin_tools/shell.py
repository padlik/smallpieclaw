"""Shell built-in tool: subprocess and PTY backends plus artifact logging.

Handler module: ``ShellTools`` holds a back-reference to the ``BuiltinExecutor``
façade (``owner``) and reads constructor-time settings (``default_timeout``,
``max_output``, ``_data_dir``, ``_state_home``, ``_shell_*``) via it at call
time. Confirmation is
staged only through ``owner._requires_confirmation`` (Decision 8 seam
constraint); no lifecycle logging happens here. The PTY fallback,
process-group kill, and incremental UTF-8 decode behaviour are preserved
verbatim. Heavy/optional imports (``select``, ``ptyprocess``) stay
function-local; the ``builtin_executor`` import is under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import codecs
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from builtin_tools.patterns import _is_dangerous_shell
from builtin_tools.text_utils import _truncate_tail

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)


class _SupportsClose(Protocol):
    """Protocol for file-like objects that support close()."""

    def close(self) -> None: ...


class _SupportsWriteClose(Protocol):
    """Protocol for file-like objects that support write() and close()."""

    def write(self, text: str, /) -> int: ...
    def close(self) -> None: ...


class ShellTools:
    """Shell tool handlers (subprocess + PTY) with run-scoped artifact logging."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _should_confirm(self, category: str) -> bool:
        """Decide whether a dangerous shell pattern requires confirmation.

        Uses ``shell_nsjail_confirm_mode`` and whether nsjail is active:
        - ``"always"`` (default): confirm all dangerous patterns regardless of category.
        - ``"adaptive"``: skip confirmation for ``network`` category patterns when
          nsjail network isolation is active (``allow_net = false``).
          All other categories (including ``resource``) still confirm.
        - ``"never"``: skip confirmation for all dangerous patterns when nsjail is active.

        Falls back to ``"always"`` behaviour when nsjail is not active (subprocess
        fallback) — the sandbox is not present, so all dangerous patterns must confirm.
        """
        if not self._owner._shell_nsjail_active:
            return True
        mode = self._owner._shell_nsjail_confirm_mode
        if mode == "never":
            return False
        if mode == "adaptive":
            # Only skip network-category commands when the sandbox has network isolation.
            # resource (fork bomb) always confirms — rlimit_nproc is user-wide, not per-jail.
            if category == "network" and not self._owner._allow_net:
                return False
            return True
        return True  # "always"

    def _exec_shell(self, args: dict, caller_depth: int = 0, caller_tag: str = "",
                    chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        command = str(args.get("command", "")).strip()
        if not command:
            return {"success": False, "output": "", "error": "No command provided.", "exit_code": -1}

        dangerous, reason, category = _is_dangerous_shell(command)
        if dangerous and self._should_confirm(category):
            desc = f"Run shell command: <code>{command}</code>\n⚠️ Reason for confirmation: {reason}"
            return self._owner._requires_confirmation("shell", args, desc, caller_depth=caller_depth, caller_tag=caller_tag)

        return self._run_shell(args, caller_tag=caller_tag, chunk_callback=chunk_callback)

    def _run_shell(self, args: dict, caller_tag: str = "",
                   chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        """Dispatch to the configured shell backend (subprocess, pty, or nsjail)."""
        if self._owner._shell_backend == "nsjail" and self._owner._shell_nsjail_active:
            return self._run_shell_nsjail(args, caller_tag=caller_tag)
        if self._owner._shell_backend == "pty" and sys.platform != "win32":
            return self._run_shell_pty(args, caller_tag=caller_tag, chunk_callback=chunk_callback)
        return self._run_shell_subprocess(args, caller_tag=caller_tag)

    def _open_shell_log(self, caller_tag: str = "", conv_id: str = "") -> tuple[Optional[_SupportsWriteClose], Optional[str]]:
        """Open a run-specific artifact log file for incremental writing.

        Returns (file_handle, absolute_path) or (None, None) on failure.
        The caller must close the file handle and call _finalize_shell_log to
        either keep or remove the file.

        Shell logs can contain sensitive command output, so the directory is
        created owner-only (0700) and the file owner-only (0600).
        """
        try:
            conv_id = conv_id or self._owner.conversation_id or "default"
            log_dir = os.path.join(
                self._owner._state_home, "session_logs", conv_id
            )
            os.makedirs(log_dir, mode=0o700, exist_ok=True)
            # makedirs honours mode only when creating; tighten an existing dir.
            try:
                os.chmod(log_dir, 0o700)
            except OSError:
                pass
            ts = time.strftime("%Y%m%d-%H%M%S")
            fname = f"shell-{ts}-{secrets.token_hex(4)}.log"
            path = os.path.abspath(os.path.join(log_dir, fname))
            # O_EXCL guarantees we created the file; 0o600 → owner read/write only.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            fh = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
            return fh, path
        except OSError as exc:
            logger.warning("Built-in shell: cannot open artifact log: %s", exc)
            return None, None

    def _finalize_shell_log(self, fh, path: Optional[str], total_chars: int,
                            caller_tag: str = "") -> Optional[str]:
        """Close the artifact file and decide whether to keep or delete it.

        Keeps the file (and returns path) only when total_chars exceeds
        max_output.  Otherwise removes the file and returns None.
        """
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        if path is None:
            return None
        if total_chars > self._owner.max_output:
            logger.info("Built-in shell: full output (%d chars) saved to %s",
                        total_chars, path)
            _secrets = getattr(self._owner, '_vault_secrets', [])
            if _secrets:
                _REDACT_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB
                try:
                    file_size = os.path.getsize(path)
                except OSError:
                    file_size = 0
                if file_size > _REDACT_SIZE_LIMIT:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    logger.warning(
                        "session log at %s exceeded redaction size limit (%d bytes) — deleted",
                        path,
                        file_size,
                    )
                    return None
                _tmp = path + '.tmp'
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as _f:
                        _content = _f.read()
                    for _s in _secrets:
                        if _s:
                            _content = _content.replace(_s, '[REDACTED]')
                    with open(_tmp, 'w', encoding='utf-8') as _f:
                        _f.write(_content)
                    try:
                        os.chmod(_tmp, 0o600)
                    except OSError:
                        pass
                    os.replace(_tmp, path)
                except OSError:
                    try:
                        os.unlink(_tmp)
                    except OSError:
                        pass
            return path
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    def _run_shell_nsjail(self, args: dict, caller_tag: str = "") -> dict:
        """Run a shell command inside an nsjail sandbox.

        Builds the nsjail config + command list via ``NsjailConfigBuilder``,
        then delegates to ``_run_shell_subprocess`` with the nsjail command
        (reusing the same select() loop, output truncation, artifact logging,
        and error classification). On an nsjail setup failure, when
        ``shell_nsjail_dump_config_on_error`` is enabled, the generated config
        is copied into the per-conversation session_logs directory before the
        tempfile is cleaned up in a ``finally`` block.

        Falls back to ``_run_shell_subprocess`` if the nsjail binary is not
        found at runtime (e.g. binary removed after startup).
        """
        import shutil as _shutil

        if _shutil.which("nsjail") is None:
            logger.warning("nsjail binary not found at runtime — falling back to subprocess")
            self._owner._shell_nsjail_active = False
            return self._run_shell_subprocess(args, caller_tag=caller_tag)

        builder = self._owner._nsjail_builder
        if builder is None:
            logger.warning("nsjail builder not initialised — falling back to subprocess")
            return self._run_shell_subprocess(args, caller_tag=caller_tag)

        conv_id = self._owner.conversation_id or "default"
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self._owner.default_timeout))
        with self._owner._shell_env_lock:
            env_snapshot = dict(self._owner._shell_env)
        session_logs_dir = os.path.join(
            self._owner._state_home,
            "session_logs",
            conv_id,
        )
        try:
            os.makedirs(session_logs_dir, mode=0o700, exist_ok=True)
        except OSError:
            pass
        cfg_path, nsjail_cmd = builder.build(
            command, timeout, shell_env=env_snapshot, session_logs_dir=session_logs_dir
        )

        try:
            result = self._run_shell_subprocess(args, caller_tag=caller_tag, nsjail_cmd=nsjail_cmd, conv_id=conv_id)
            # On nsjail setup failure, optionally snapshot the generated config
            # into the per-conversation session_logs directory for post-mortem
            # debugging. The dump runs inside the try block so it only fires on
            # a real result; the finally below always cleans up the tempfile.
            if (
                result.get("error_type") == "nsjail_error"
                and self._owner._shell_nsjail_dump_config_on_error
            ):
                self._dump_nsjail_config(cfg_path, session_logs_dir)
            return result
        finally:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass

    def _dump_nsjail_config(self, cfg_path: str, session_logs_dir: str) -> None:
        """Best-effort copy of a generated nsjail config into session_logs.

        Used for post-mortem debugging when ``shell_nsjail_dump_config_on_error``
        is enabled and an nsjail setup failure occurred. Copy errors are logged
        at warning level and swallowed — the config dump must never mask the
        original shell failure.
        """
        import shutil as _shutil
        import threading as _threading
        from datetime import datetime as _datetime
        try:
            os.makedirs(session_logs_dir, mode=0o700, exist_ok=True)
            # Microseconds + thread id guard against same-second collisions when
            # parallel shell calls (plan steps / sub-agents run as threads in
            # this process) both hit nsjail_error within one wall-clock second.
            stamp = _datetime.now().strftime("%Y%m%dT%H%M%S_%f")
            dest = os.path.join(session_logs_dir, f"nsjail-config-{stamp}-{_threading.get_ident()}.cfg")
            _shutil.copy2(cfg_path, dest)
            logger.info("nsjail: dumped failed config to %s", dest)
        except OSError as exc:
            logger.warning("nsjail: failed to dump config to %s: %s", session_logs_dir, exc)

    def _run_shell_subprocess(self, args: dict, caller_tag: str = "",
                              nsjail_cmd: Optional[list[str]] = None,
                              conv_id: str = "") -> dict:
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self._owner.default_timeout))
        logger.info("Built-in shell (subprocess) executing: %s", command[:120])
        _start = time.monotonic()

        # Open artifact log for incremental writing; kept only if output is large.
        _log_fh, _artifact_path = self._open_shell_log(caller_tag, conv_id=conv_id)
        _tail_out = ""
        _tail_err = ""
        _total_out = 0
        _total_err = 0
        _stderr_header_written = False

        # Start the command in its own process group/session so that on timeout
        # we can kill the whole tree (the shell plus any children that inherited
        # the stdout/stderr pipes), not just the top-level shell.  Without this,
        # a leaked grandchild can keep the pipes open and block the reader threads.
        _popen_kwargs: dict = {}
        if sys.platform != "win32":
            _popen_kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows-only
            _popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            if nsjail_cmd is not None:
                proc = subprocess.Popen(
                    nsjail_cmd,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **_popen_kwargs,
                )
            else:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **_popen_kwargs,
                )
        except OSError as exc:
            if _log_fh:
                _log_fh.close()
            if _artifact_path:
                try:
                    os.unlink(_artifact_path)
                except OSError:
                    pass
            err_text = str(exc)
            error_type = "command_not_found" if "No such file or directory" in err_text else "tool_timeout"
            suggestion = (
                "Check the command name or install the missing executable."
                if error_type == "command_not_found"
                else "Try the command again with a longer timeout."
            )
            return {
                "success": False,
                "output": "",
                "error": err_text,
                "exit_code": -1,
                "error_type": error_type,
                "recoverable": error_type == "tool_timeout",
                "suggestion": suggestion,
            }

        def _kill_tree() -> None:
            """Kill the whole process group (POSIX) or the process (Windows)."""
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    return
                except (OSError, ProcessLookupError):
                    pass
            try:
                proc.kill()
            except OSError:
                pass

        def _close_pipe(pipe) -> None:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

        def _disable_artifact_log() -> None:
            """Silently close and unlink the artifact on write failure."""
            nonlocal _log_fh, _artifact_path
            fh, path = _log_fh, _artifact_path
            _log_fh = None
            _artifact_path = None
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        def _append_stdout(text: str) -> None:
            nonlocal _tail_out, _total_out
            if not text:
                return
            _total_out += len(text)
            _tail_out = (_tail_out + text)[-self._owner.max_output:]
            if _log_fh is not None:
                try:
                    _log_fh.write(text)
                except OSError:
                    _disable_artifact_log()

        def _append_stderr(text: str) -> None:
            nonlocal _tail_err, _total_err, _stderr_header_written
            if not text:
                return
            _total_err += len(text)
            _tail_err = (_tail_err + text)[-self._owner.max_output:]
            if _log_fh is not None:
                try:
                    if not _stderr_header_written:
                        _log_fh.write("\n--- stderr ---\n")
                        _stderr_header_written = True
                    _log_fh.write(text)
                except OSError:
                    _disable_artifact_log()

        import select as _select

        # Per-stream incremental UTF-8 decoders keep multibyte characters that
        # straddle os.read() chunk boundaries intact (a plain chunk.decode()
        # would emit U+FFFD replacement chars for the split halves).
        streams: dict[int, tuple[object, Callable[[str], None], codecs.IncrementalDecoder]] = {}
        for _pipe, _append in ((proc.stdout, _append_stdout), (proc.stderr, _append_stderr)):
            if _pipe is None:
                continue
            try:
                os.set_blocking(_pipe.fileno(), False)
                streams[_pipe.fileno()] = (
                    _pipe, _append, codecs.getincrementaldecoder("utf-8")(errors="replace"),
                )
            except (OSError, ValueError):
                _close_pipe(_pipe)

        timed_out = False
        deadline = _start + timeout
        while streams:
            now = time.monotonic()
            if not timed_out and proc.poll() is None and now >= deadline:
                timed_out = True
                _kill_tree()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                break

            # If the shell has exited and no stream is immediately readable,
            # return without waiting for EOF: escaped descendants can keep pipe
            # fds open indefinitely.  This preserves data already available in
            # the pipe while avoiding reader-thread leaks/hangs.
            select_timeout = 0.05 if proc.poll() is not None else max(0.0, min(0.1, deadline - now))
            try:
                ready, _, _ = _select.select(list(streams), [], [], select_timeout)
            except (OSError, ValueError):
                break
            if not ready and proc.poll() is not None:
                break
            for fd in ready:
                pipe, append, decoder = streams.get(fd, (None, None, None))
                if pipe is None or append is None or decoder is None:
                    continue
                while True:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        break
                    except OSError:
                        append(decoder.decode(b"", final=True))
                        streams.pop(fd, None)
                        _close_pipe(pipe)
                        break
                    if not chunk:
                        append(decoder.decode(b"", final=True))
                        streams.pop(fd, None)
                        _close_pipe(pipe)
                        break
                    append(decoder.decode(chunk))

        for _pipe, _append, _decoder in list(streams.values()):
            _append(_decoder.decode(b"", final=True))
            _close_pipe(_pipe)
        streams.clear()

        if proc.poll() is None and not timed_out:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        elapsed_ms = (time.monotonic() - _start) * 1000.0
        total_combined = _total_out + _total_err
        full_log_path = self._finalize_shell_log(_log_fh, _artifact_path, total_combined, caller_tag)

        # Build truncated outputs from rolling tails using correct total counts.
        output = _truncate_tail(_tail_out, _total_out, self._owner.max_output)
        error = _truncate_tail(_tail_err, _total_err, self._owner.max_output)

        returncode = proc.returncode if not timed_out else -1

        # Detect nsjail-own failures before any stderr→output promotion.
        # nsjail prefixes its own error lines with [E][ in its log output (stderr).
        # This must run before the stderr promotion below, which would move the
        # [E][ marker into `output` and empty `error`, hiding the nsjail failure.
        is_nsjail_error = (
            nsjail_cmd is not None
            and not timed_out
            and returncode != 0
            and "[E][" in error
        )

        if not timed_out and returncode != 0 and not output.strip() and error:
            # Some commands write only to stderr (e.g. systemctl status);
            # promote stderr → output so the LLM sees the failure reason.
            # Skip promotion for nsjail-own failures so the [E][ marker stays
            # in `error` for the nsjail_error classification below.
            if not is_nsjail_error:
                output = error
                error = ""

        if full_log_path:
            notice = f"\n[full output saved to: {full_log_path} — use file_read to view it]"
            output = output + notice

        logger.info(
            "Built-in shell exit=%s stdout=%d stderr=%d chars in %.0fms",
            returncode, _total_out, _total_err, elapsed_ms,
        )
        if timed_out:
            timeout_error = f"Command timed out after {timeout}s."
            if error.strip():
                timeout_error = f"{timeout_error}\nstderr:\n{error}"
            return {
                "success": False,
                "output": output,
                "error": timeout_error,
                "exit_code": -1,
                "elapsed_ms": round(elapsed_ms),
                "full_log_path": full_log_path,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Try the command again with a longer timeout.",
            }
        if is_nsjail_error:
            return {
                "success": False,
                "output": output,
                "error": error,
                "exit_code": returncode,
                "elapsed_ms": round(elapsed_ms),
                "full_log_path": full_log_path,
                "error_type": "nsjail_error",
                "recoverable": False,
                "suggestion": (
                    "The nsjail sandbox failed to set up. "
                    "Check kernel namespace permissions, cgroup availability, or nsjail binary."
                ),
            }

        # Classify non-zero exit codes from the shell.
        error_type = ""
        recoverable = False
        suggestion = ""
        if returncode != 0:
            error_lower = error.lower()
            output_lower = output.lower()
            combined = f"{error_lower}\n{output_lower}"
            if "permission denied" in combined:
                error_type = "permission_denied"
                recoverable = False
                suggestion = "Check file permissions or use sudo."
            elif "command not found" in combined or ("not found" in error_lower and "file" not in error_lower):
                error_type = "command_not_found"
                recoverable = False
                suggestion = "Check the command name or install the missing executable."
            elif "no such file or directory" in combined:
                error_type = "file_not_found"
                recoverable = False
                suggestion = "Check the file path or create the missing file."
        return {
            "success": returncode == 0,
            "output": output,
            "error": error,
            "exit_code": returncode,
            "elapsed_ms": round(elapsed_ms),
            "full_log_path": full_log_path,
            "error_type": error_type,
            "recoverable": recoverable,
            "suggestion": suggestion,
        }

    def _run_shell_pty(self, args: dict, caller_tag: str = "",
                       chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        """Run shell command inside a pseudo-terminal.

        Gives the child process a real TTY so isatty()==True, enabling:
        - line-buffered (real-time) output instead of 64 KB block buffering
        - ANSI colour codes from tools like git, pytest, npm
        - progress indicators that detect a terminal
        stdout and stderr are merged by the PTY line discipline (chronological
        order preserved).  Falls back to subprocess on import error.

        When chunk_callback is provided and self._owner._shell_streaming is True, each
        decoded text chunk is forwarded to the callback as it arrives.
        """
        conv_id = self._owner.conversation_id or "default"
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", self._owner.default_timeout))
        logger.info("Built-in shell (pty) executing: %s", command[:120])
        streaming = self._owner._shell_streaming and chunk_callback is not None

        try:
            from ptyprocess import PtyProcessUnicode  # type: ignore[import]
        except ImportError:
            logger.warning("ptyprocess not available, falling back to subprocess")
            return self._run_shell_subprocess(args, caller_tag=caller_tag)

        import select as _select
        import re as _re
        _ANSI_RE = _re.compile(r'\x1b\[[0-9;]*[mGKHF]|\x1b\].*?\x07')

        try:
            proc = PtyProcessUnicode.spawn(
                ['/bin/sh', '-c', command],
                dimensions=(self._owner._shell_pty_rows, self._owner._shell_pty_cols),
                echo=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PTY spawn failed (%s), falling back to subprocess", exc)
            return self._run_shell_subprocess(args, caller_tag=caller_tag)

        import time as _time
        total_chars = 0
        timed_out = False
        _start = _time.monotonic()
        deadline = _start + timeout

        # Rolling tail: bounded memory regardless of how much the process emits.
        _tail = ""

        # Open artifact log for incremental writing.
        _log_fh, _artifact_path = self._open_shell_log(caller_tag, conv_id=conv_id)

        try:
            while proc.isalive():
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    ready, _, _ = _select.select([proc.fd], [], [], min(remaining, 0.25))
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = proc.read(4096)
                except EOFError:
                    break
                # Normalize TTY line endings and strip ANSI for clean LLM output
                chunk = chunk.replace('\r\n', '\n').replace('\r', '\n')
                chunk = _ANSI_RE.sub('', chunk)
                total_chars += len(chunk)
                _tail = (_tail + chunk)[-self._owner.max_output:]
                if _log_fh is not None:
                    _log_fh.write(chunk)
                if streaming and chunk_callback is not None:
                    try:
                        chunk_callback(chunk)
                    except Exception:  # noqa: BLE001
                        pass

            # Drain remaining output after loop
            if not timed_out:
                for _ in range(20):
                    try:
                        r, _, _ = _select.select([proc.fd], [], [], 0.05)
                        if not r:
                            break
                        chunk = proc.read(4096).replace('\r\n', '\n').replace('\r', '\n')
                        chunk = _ANSI_RE.sub('', chunk)
                        total_chars += len(chunk)
                        _tail = (_tail + chunk)[-self._owner.max_output:]
                        if _log_fh is not None:
                            _log_fh.write(chunk)
                        if streaming and chunk_callback is not None:
                            try:
                                chunk_callback(chunk)
                            except Exception:  # noqa: BLE001
                                pass
                    except (EOFError, OSError, ValueError):
                        break
        finally:
            if timed_out:
                # On POSIX, signal the PTY child's process group so background
                # children that ignore SIGHUP also get terminated.
                if sys.platform != "win32":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                proc.terminate(force=True)
            proc.close(force=False)

        elapsed_ms = (_time.monotonic() - _start) * 1000.0
        full_log_path = self._finalize_shell_log(_log_fh, _artifact_path, total_chars, caller_tag)

        output = _truncate_tail(_tail, total_chars, self._owner.max_output)
        if full_log_path:
            output = output + f"\n[full output saved to: {full_log_path} — use file_read to view it]"

        exit_code = proc.exitstatus if not timed_out else -1
        # exitstatus is None if signalled; treat as failure
        if exit_code is None:
            exit_code = -1
        logger.info(
            "Built-in shell (pty) exit=%s combined=%d chars in %.0fms",
            exit_code, total_chars, elapsed_ms,
        )
        if timed_out:
            return {
                "success": False,
                "output": output,
                "error": f"Command timed out after {timeout}s.",
                "exit_code": -1,
                "elapsed_ms": round(elapsed_ms),
                "full_log_path": full_log_path,
                "error_type": "tool_timeout",
                "recoverable": True,
                "suggestion": "Try the command again with a longer timeout.",
            }
        error_type = ""
        recoverable = False
        suggestion = ""
        if exit_code != 0:
            output_lower = output.lower()
            if "permission denied" in output_lower:
                error_type = "permission_denied"
                suggestion = "Check file permissions or use sudo."
            elif "command not found" in output_lower:
                error_type = "command_not_found"
                suggestion = "Check the command name or install the missing executable."
            elif "no such file or directory" in output_lower:
                error_type = "file_not_found"
                suggestion = "Check the file path or create the missing file."
        return {
            "success": exit_code == 0,
            "output": output,
            "error": "" if exit_code == 0 else output,
            "exit_code": exit_code,
            "elapsed_ms": round(elapsed_ms),
            "full_log_path": full_log_path,
            "error_type": error_type,
            "recoverable": recoverable,
            "suggestion": suggestion,
        }
