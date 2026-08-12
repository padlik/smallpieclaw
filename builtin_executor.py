"""
builtin_executor.py
-------------------
Always-available built-in tools, injected into every agent run. See BUILTIN_TOOLS in
builtin_tools/descriptors.py for the full current list.

Dangerous operations (destructive commands, sensitive file access, any write)
require explicit user confirmation before execution. When confirmation is
needed, execute() returns {"requires_confirmation": True, "token": ..., ...}
and the caller is expected to call confirm(token) or cancel(token) after the
user responds.

Error classification contract implemented by the builtin_tools/* handlers
(Phase 3: Agent Recovery):
    * Transient (retryable): tool_timeout, network_error, syntax_error.
    * Planning (no retry, needs alternative approach): wrong_model_for_task,
      fundamentally_wrong_approach, impossible_with_current_tools.
    * Fatal (no retry, environment/fix required): permission_denied,
      file_not_found, command_not_found.

Result dicts produced by built-in tools include the following recovery fields:
    - error_type: kebab-case error identifier (empty string on success).
    - recoverable: whether the error might succeed on retry (False on success).
    - suggestion: human-readable recovery hint (empty string on success).
"""

from __future__ import annotations

import contextvars
import logging
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator, Optional

import agent_logging
from builtin_tools.access_control import GrantTracker
from builtin_tools.agents import AgentTools
from builtin_tools.context_io import (
    _save_context,  # noqa: F401  re-exported for tests
    _validate_context_key,  # noqa: F401  re-exported for tests
)
from builtin_tools.descriptors import BUILTIN_TOOLS, BuiltinTool
from builtin_tools.files import FileTools
from builtin_tools.logquery_helpers import (
    _LOG_QUERY_MAX_SCAN_LINES,  # noqa: F401  re-exported for tests
    _LOG_QUERY_TAIL_BYTES,  # noqa: F401  re-exported for tests
)
from builtin_tools.memory import MemoryTools
from builtin_tools.patterns import (
    _is_dangerous_shell,  # noqa: F401  re-exported for tests
    _is_sensitive_path,  # noqa: F401  re-exported for tests
)
from builtin_tools.schedule import exec_schedule
from builtin_tools.secrets_log import LogQueryTools, SecretsTools
from builtin_tools.shell import ShellTools

from xdg import xdg_paths
from builtin_tools.shell_env import ShellEnvTools
from nsjail_config import NsjailConfigBuilder
from builtin_tools.text_utils import (
    _truncate_output,  # noqa: F401  re-exported for tests
    _truncate_tail,  # noqa: F401  re-exported for tests
)

if TYPE_CHECKING:
    from prompt_registry import PromptRegistry

from sub_agent_supervisor import (
    SubAgentSupervisor,
    SupervisionOptions,
)

slog = agent_logging.get_logger(__name__)

logger = logging.getLogger(__name__)

# Module-level ContextVar holding the active per-run GrantTracker.
# None means no run context is active; the BuiltinExecutor property will fall
# back to the executor-wide default tracker for backward compatibility.
_grant_tracker_var: contextvars.ContextVar[Optional[GrantTracker]] = contextvars.ContextVar(
    "grant_tracker", default=None
)


@dataclass
class _CallContext:
    """Per-call routing context passed to the dispatch-table adapters.

    Carries the optional invocation parameters so each table adapter can forward
    exactly the kwargs its tool accepts today (Decision 3); tools that ignore a
    field simply do not read it.
    """

    caller_depth: int = 0
    caller_tag: str = ""
    chunk_callback: Optional[Callable[[str], None]] = None
    trace_id: str = ""


# ---------------------------------------------------------------------------
# BuiltinExecutor
# ---------------------------------------------------------------------------

class BuiltinExecutor:
    """
    Executes built-in tools with optional confirmation for dangerous operations.

    Confirmation flow:
      1. execute() detects a dangerous/sensitive operation.
      2. Returns {"requires_confirmation": True, "token": token, "description": desc}.
      3. Caller stores the token and prompts the user.
      4. On user approval:  call confirm(token) → returns the actual result dict.
      5. On user rejection: call cancel(token)  → cleans up state.
    """

    def __init__(self, default_timeout: int = 30, max_output: int = 4000, scheduler=None,
                 sub_agent_factory=None, data_dir: str = "data",
                 memory=None, max_subagents: int = 6, subagent_result_timeout: int = 300,
                 notify_html_fn=None, shell_backend: str = "subprocess",
                 shell_pty_cols: int = 220, shell_pty_rows: int = 50,
                 shell_streaming: bool = False, working=None, results=None,
                 vault_path: str = "", log_jsonl_path: str = "",
                 shell_nsjail_confirm_mode: str = "always",
                 shell_nsjail_memory_mb: int = 256,
                 shell_nsjail_pids_max: int = 64,
                 shell_nsjail_cpu_percent: int = 50,
                    allow_net: bool = False,
                    nsjail_dns_nameserver: str = "8.8.8.8",
                    nsjail_session_tmpdir: str = "",
                   nsjail_trusted_dirs_path: str = "",
                   skills_dir: str = "",
                   nsjail_agent_dir: str = "",
                   agent_name: str = "piclaw",
                   tmp_dir: str = "",
                   state_home: str = "",
                   vault_secrets: Optional[list[str]] = None,
                   shell_nsjail_dump_config_on_error: bool = False):
        self.default_timeout = default_timeout
        self.max_output = max_output
        self.scheduler = scheduler  # Optional[Scheduler] — for the schedule built-in
        self._sub_agent_factory = sub_agent_factory  # Callable[[model, context_key, label, notify_fn], SubAgentRunner]
        self._data_dir = data_dir
        self._agent_name = agent_name
        # XDGPaths.state_home (already agent_name-suffixed) as a str, passed down
        # explicitly so builtin_tools/shell.py doesn't re-derive it independently
        # (single source of truth is xdg.py). Falls back to xdg_paths() when unset
        # (tests, ad-hoc callers).
        self._state_home = state_home or str(xdg_paths(agent_name).state_home)
        # Optional[str] — current conversation id; set by main.py on startup and
        # rotated by AgentController.reset_task(). Used for per-conversation
        # session_logs paths and persistence.
        self.conversation_id: str = ""
        self._memory = memory  # Optional[MemoryStore] — for memory_write built-in
        self._working = working  # Optional[WorkingMemory] — for spawn_agent context summary
        self._results = results  # Optional[ResultsMemory] — for spawn_agent context summary
        self._max_subagents = max_subagents
        self._subagent_result_timeout = subagent_result_timeout
        self._notify_html_fn = notify_html_fn  # Optional[Callable[[str], None]] — HTML notify path
        self._vault_path = vault_path  # Path to TOML vault file for secret_get
        self._log_jsonl_path = log_jsonl_path  # Active JSONL log sink for the log_query built-in
        self._vault_secrets: list[str] = list(vault_secrets or [])
        self._graph_memory = None   # Optional[GraphMemoryStore] — set by main.py after init
        self._graph_memory_writer = None  # Optional[GraphMemoryWriter] — set by main.py after init
        self._shell_backend = shell_backend   # "subprocess" or "pty"
        self._shell_pty_cols = shell_pty_cols
        self._shell_pty_rows = shell_pty_rows
        self._shell_streaming = shell_streaming  # forward chunks to on_chunk callback (PTY only)
        # nsjail shell backend state
        self._shell_nsjail_confirm_mode = shell_nsjail_confirm_mode
        self._shell_nsjail_memory_mb = shell_nsjail_memory_mb
        self._shell_nsjail_pids_max = shell_nsjail_pids_max
        self._shell_nsjail_cpu_percent = shell_nsjail_cpu_percent
        self._allow_net = allow_net
        self._nsjail_dns_nameserver = nsjail_dns_nameserver
        self._shell_nsjail_session_tmpdir = nsjail_session_tmpdir
        self._shell_nsjail_dump_config_on_error = shell_nsjail_dump_config_on_error
        # Session-scoped env dict for nsjail -E flag injection
        self._shell_env: dict[str, str] = {}
        self._shell_env_lock = threading.Lock()
        # Whether nsjail backend is actually active (binary found + backend selected)
        self._shell_nsjail_active = False
        # Background sub-agent lifecycle is owned by the supervisor, which also
        # owns the thread pool. The model-facing _exec_spawn_agent shim and the
        # scheduler both delegate accepted runs to it.
        self._supervisor = SubAgentSupervisor(max_subagents=max_subagents)
        # pending: token -> (tool_name, args)
        self._pending: dict[str, tuple[str, dict]] = {}
        # Per-prompt approve-all set. Shared reference to the main agent's
        # ConfirmationManager.auto_approve_tools set during a run; None outside
        # of a run so sub-agents fail-closed after the prompt ends.
        self._prompt_approval_set: Optional[set[str]] = None
        # Active prompt ID and registry reference for sub-agent tracking.
        self._current_prompt_id: Optional[str] = None
        self._prompt_registry: Optional[PromptRegistry] = None
        # Headless (sub-agent) confirmation bridge
        # token -> threading.Event  (set when the operator responds)
        self._headless_confirm_events: dict[str, threading.Event] = {}
        # token -> bool  (True = approved, False = denied)
        self._headless_confirm_results: dict[str, bool] = {}
        # Optional prompt callback: fn(token, tool_name, description, caller_tag) -> None
        # Set by main.py after TelegramInterface is created.
        self._subagent_confirm_prompt_fn: Optional[Callable[[str, str, str, str], None]] = None
        # How long (seconds) to wait for the operator to respond to a sub-agent prompt
        self._subagent_confirm_timeout: int = 120
        # Tool-group handlers own the moved tool bodies; they read late-bound
        # collaborators and stage confirmation via this owner façade at call time.
        self._files = FileTools(self)
        self._memory_tools = MemoryTools(self)
        self._secrets = SecretsTools(self)
        self._logquery = LogQueryTools(self)
        self._shell = ShellTools(self)
        self._shell_env_tools = ShellEnvTools(self)
        self._agents = AgentTools(self)
        # nsjail config builder — only instantiated when nsjail backend is selected
        self._nsjail_builder: Optional[NsjailConfigBuilder] = None
        if shell_backend == "nsjail":
            if not (nsjail_session_tmpdir and tmp_dir):
                logger.warning(
                    "shell_backend='nsjail' but nsjail_session_tmpdir=%r/tmp_dir=%r not both "
                    "set — falling back to subprocess",
                    nsjail_session_tmpdir, tmp_dir,
                )
            else:
                nsjail_binary = shutil.which("nsjail")
                if nsjail_binary is not None:
                    self._shell_nsjail_active = True
                    self._nsjail_builder = NsjailConfigBuilder(
                        session_tmpdir=nsjail_session_tmpdir,
                        tmp_dir=tmp_dir,
                        trusted_dirs_path=nsjail_trusted_dirs_path,
                        memory_mb=shell_nsjail_memory_mb,
                        pids_max=shell_nsjail_pids_max,
                        cpu_percent=shell_nsjail_cpu_percent,
                        allow_net=self._allow_net,
                        dns_nameserver=self._nsjail_dns_nameserver,
                        skills_dir=skills_dir,
                        agent_dir=nsjail_agent_dir,
                    )
                    logger.info("nsjail shell backend active (binary: %s)", nsjail_binary)
                else:
                    logger.warning("shell_backend='nsjail' but nsjail binary not found — falling back to subprocess")
        # Zone-based access control — set by main.py after construction
        self.trusted_zone_checker = None  # Optional[TrustedZoneChecker]
        # Skill registry — set by main.py after construction (same pattern as trusted_zone_checker)
        self.skill_registry = None  # Optional[SkillRegistry]
        # Per-executor fallback GrantTracker used outside of an active run context.
        # Runs use a context-scoped ContextVar (set via use_grant_tracker) so
        # concurrent sub-agents are isolated automatically without push/pop bookkeeping.
        self._default_grant_tracker: GrantTracker = GrantTracker()
        # Per-confirmation zone_path store: token -> original path (for Telegram zone buttons)
        self._zone_paths: dict[str, str] = {}
        # Per-confirmation tracker capture: token -> GrantTracker (so the Telegram
        # callback thread can write to the run-scoped tracker, not the default).
        self._zone_trackers: dict[str, "GrantTracker"] = {}
        # Name-keyed dispatch registries (replace the former if/elif chains).
        # Each value is a per-tool adapter that forwards exactly the kwargs that
        # tool accepts today (Decision 3); vision_query has no entry — it is
        # executed by the ReAct loop, not by this dispatch.
        self._exec_table: dict[str, Callable[[dict, _CallContext], dict]] = {
            "shell": lambda a, ctx: self._shell._exec_shell(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
                chunk_callback=ctx.chunk_callback,
            ),
            "file_read": lambda a, ctx: self._files._exec_file_read(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
            ),
            "file_write": lambda a, ctx: self._files._exec_file_write(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
            ),
            "file_send": lambda a, ctx: self._files._exec_file_send(a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag),
            "schedule": lambda a, ctx: self._exec_schedule(a, caller_depth=ctx.caller_depth),
            "spawn_agent": lambda a, ctx: self._exec_spawn_agent(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
                trace_id=ctx.trace_id,
            ),
            "get_agent_result": lambda a, ctx: self._exec_get_agent_result(a, caller_tag=ctx.caller_tag),
            "wait_for_any_agent": lambda a, ctx: self._agents._exec_wait_for_any_agent(a, caller_tag=ctx.caller_tag),
            "cancel_agent": lambda a, ctx: self._agents._exec_cancel_agent(a, caller_tag=ctx.caller_tag),
            "memory_write": lambda a, ctx: self._memory_tools._exec_memory_write(a, caller_tag=ctx.caller_tag),
            "file_patch": lambda a, ctx: self._files._exec_file_patch(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
            ),
            "file_diff": lambda a, ctx: self._files._exec_file_diff(a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag),
            "memory_graph_search": lambda a, ctx: self._memory_tools._exec_memory_graph_search(a, caller_tag=ctx.caller_tag),
            "memory_graph_store": lambda a, ctx: self._memory_tools._exec_memory_graph_store(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
            ),
            "secret_get": lambda a, ctx: self._secrets._exec_secret_get(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
            ),
            "log_query": lambda a, ctx: self._logquery._exec_log_query(
                a, caller_depth=ctx.caller_depth, caller_tag=ctx.caller_tag,
            ),
            "shell_env_set": lambda a, ctx: self._shell_env_tools.shell_env_set(a),
            "shell_env_unset": lambda a, ctx: self._shell_env_tools.shell_env_unset(a),
            "shell_env_list": lambda a, ctx: self._shell_env_tools.shell_env_list(a),
            "shell_env_get": lambda a, ctx: self._shell_env_tools.shell_env_get(a),
        }
        # Confirmation-capable execution table: tools whose execute() path may
        # return ``requires_confirmation`` and therefore need a post-approval
        # runner. The two new sub-agent control tools are intentionally absent
        # because they are not confirmation-gated.
        self._run_table: dict[str, Callable[[dict, _CallContext], dict]] = {
            "shell": lambda a, ctx: self._shell._run_shell(
                a, caller_tag=ctx.caller_tag, chunk_callback=ctx.chunk_callback,
            ),
            "file_read": lambda a, ctx: self._files._run_file_read(a, caller_tag=ctx.caller_tag),
            "file_write": lambda a, ctx: self._files._run_file_write(a, caller_tag=ctx.caller_tag),
            "file_patch": lambda a, ctx: self._files._run_file_patch(a, caller_tag=ctx.caller_tag),
            "file_diff": lambda a, ctx: self._files._run_file_diff(a, caller_tag=ctx.caller_tag),
            "file_send": lambda a, ctx: self._files._run_file_send(a, caller_tag=ctx.caller_tag),
            "memory_graph_store": lambda a, ctx: self._memory_tools._run_memory_graph_store(a, caller_tag=ctx.caller_tag),
            "secret_get": lambda a, ctx: self._secrets._run_secret_get(a, caller_tag=ctx.caller_tag),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def grant_tracker(self) -> GrantTracker:
        """Return the active GrantTracker for the current run context.

        Uses a module-level ContextVar so each concurrent run (main or sub-agent)
        gets its own isolated tracker automatically. Falls back to the executor-wide
        default tracker when called outside of an active ``use_grant_tracker`` block.
        """
        active = _grant_tracker_var.get()
        return active if active is not None else self._default_grant_tracker

    @contextmanager
    def use_grant_tracker(self, gt: GrantTracker) -> Iterator[None]:
        """Set *gt* as the active GrantTracker for the current context.

        Thread- and asyncio-safe: the tracker is bound to the current thread/task
        context via a ContextVar, so concurrent sub-agent runs cannot see each
        other's grants. The previous value is restored on exit.
        """
        token = _grant_tracker_var.set(gt)
        try:
            yield
        finally:
            _grant_tracker_var.reset(token)

    def shutdown(self, graceful_timeout: float = 10.0) -> None:
        """Shut down the sub-agent thread pool.

        Delegates to the supervisor, which signals all active sub-agents to
        cancel, waits up to graceful_timeout seconds for them to finish, then
        forces shutdown of any stragglers.
        """
        self._supervisor.shutdown(graceful_timeout)

    def is_builtin(self, name: str) -> bool:
        return name in BUILTIN_TOOLS

    def all_tools(self) -> list[BuiltinTool]:
        return list(BUILTIN_TOOLS.values())

    def execute(self, tool_name: str, args: Optional[dict] = None, caller_depth: int = 0, caller_tag: str = "",
                chunk_callback: Optional[Callable[[str], None]] = None, trace_id: str = "") -> dict:
        """
        Execute a built-in tool. Returns standard result dict, or a
        requires_confirmation dict if the operation needs user approval.

        caller_depth is the depth of the AgentController invoking this tool
        (0 = main agent, 1 = sub-agent). Used to enforce the no-nested-spawn rule.
        caller_tag is a human-readable label for logging (e.g. "[main]", "[sa-fcf85d]").
        chunk_callback is an optional callable invoked with each output chunk during PTY
        shell execution (only when shell_streaming=True). Ignored for other tools.
        trace_id is the request-scoped trace of the invoking run; it is propagated to
        spawned sub-agents so their logs correlate with the parent request.

        Emits TOOL_START before dispatch and TOOL_END/TOOL_FAILED afterwards
        (plus ERROR on an unexpected exception). These lifecycle events wrap the
        existing dispatch without altering its result-dict contract or exception
        propagation. A deferred result (requires_confirmation) gets no completion
        event — the underlying operation has not run yet.
        """
        args = args or {}
        start = time.perf_counter()
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_START,
            f"tool start: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
        )
        try:
            result = self._dispatch(
                tool_name, args, caller_depth=caller_depth, caller_tag=caller_tag,
                chunk_callback=chunk_callback, trace_id=trace_id,
            )
        except Exception as exc:
            dur_ms = int((time.perf_counter() - start) * 1000)
            self._emit_tool_lifecycle_error(tool_name, exc, dur_ms)
            raise
        dur_ms = int((time.perf_counter() - start) * 1000)
        self._emit_tool_lifecycle_end(tool_name, result, dur_ms)
        return result

    def _emit_tool_lifecycle_end(self, tool_name: str, result: dict, dur_ms: int) -> None:
        """Emit TOOL_END on a successful result or TOOL_FAILED on an error result.

        A ``requires_confirmation`` result is a deferred operation (nothing has
        executed yet), so no completion event is emitted for it.
        """
        if isinstance(result, dict) and result.get("requires_confirmation"):
            return
        if isinstance(result, dict) and result.get("success"):
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_END,
                f"tool end: {tool_name}",
                level=logging.INFO,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=result.get("exit_code", 0),
            )
        else:
            exit_code = result.get("exit_code", -1) if isinstance(result, dict) else -1
            err = (result.get("error", "") if isinstance(result, dict) else "") or ""
            agent_logging.log_event(
                agent_logging.LogEvent.TOOL_FAILED,
                f"tool failed: {tool_name}",
                level=logging.ERROR,
                logger=slog,
                tool=tool_name,
                dur_ms=dur_ms,
                exit=exit_code,
                err=err,
            )

    def _emit_tool_lifecycle_error(self, tool_name: str, exc: BaseException, dur_ms: int) -> None:
        """Emit ERROR + TOOL_FAILED for an unexpected exception during a tool run.

        Shared by the ``execute()`` dispatch wrapper and the confirmed-run path
        in ``confirm()`` so a raised (rather than returned-as-dict) failure still
        closes the TOOL_START span.
        """
        agent_logging.log_event(
            agent_logging.LogEvent.ERROR,
            f"tool error: {tool_name}: {exc}",
            level=logging.ERROR,
            logger=slog,
            tool=tool_name,
            dur_ms=dur_ms,
            exit=-1,
            err=str(exc),
        )
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_FAILED,
            f"tool failed: {tool_name}",
            level=logging.ERROR,
            logger=slog,
            tool=tool_name,
            dur_ms=dur_ms,
            exit=-1,
            err=str(exc),
        )

    def _dispatch(self, tool_name: str, args: dict, caller_depth: int = 0, caller_tag: str = "",
                  chunk_callback: Optional[Callable[[str], None]] = None, trace_id: str = "") -> dict:
        """Route a built-in tool call to its handler (no lifecycle logging)."""
        handler = self._exec_table.get(tool_name)
        if handler is None:
            return {"success": False, "output": "", "error": f"Unknown built-in: {tool_name}", "exit_code": -1}
        ctx = _CallContext(
            caller_depth=caller_depth, caller_tag=caller_tag,
            chunk_callback=chunk_callback, trace_id=trace_id,
        )
        return handler(args, ctx)

    def confirm(self, token: str, chunk_callback: Optional[Callable[[str], None]] = None,
                *, _emit_lifecycle: bool = True) -> dict:
        """Execute a previously staged dangerous operation after user confirmation.

        chunk_callback is forwarded to the shell backend so live streaming keeps
        working for commands that required confirmation before running.

        execute() deferred the TOOL_END/TOOL_FAILED when it returned
        ``requires_confirmation`` (nothing had run yet), so the confirmed run
        emits the matching completion here — with ``tool`` and ``dur_ms`` — to
        close the TOOL_START span. ``_emit_lifecycle`` is set False by the
        headless bridge, whose own execute() wrapper still owns the span and
        would otherwise double-log the completion.
        """
        entry = self._pending.pop(token, None)
        self._zone_paths.pop(token, None)
        self._zone_trackers.pop(token, None)
        if entry is None:
            return {"success": False, "output": "", "error": "Confirmation token expired or unknown.", "exit_code": -1}
        tool_name, args = entry
        logger.info("Executing confirmed built-in '%s' (token %s)", tool_name, token[:8])
        start = time.perf_counter()
        try:
            result = self._run(tool_name, args, chunk_callback=chunk_callback)
        except Exception as exc:
            if _emit_lifecycle:
                dur_ms = int((time.perf_counter() - start) * 1000)
                self._emit_tool_lifecycle_error(tool_name, exc, dur_ms)
            raise
        if _emit_lifecycle:
            dur_ms = int((time.perf_counter() - start) * 1000)
            self._emit_tool_lifecycle_end(tool_name, result, dur_ms)
        return result

    def cancel(self, token: str) -> None:
        """Discard a pending confirmation.

        Closes the TOOL_START span left open when execute() deferred the
        operation: emits a cancelled TOOL_END (``cancelled=True``) when the
        token was still pending. An unknown/expired token is a no-op.
        """
        entry = self._pending.pop(token, None)
        self._zone_paths.pop(token, None)
        self._zone_trackers.pop(token, None)
        if entry is None:
            return
        tool_name = entry[0]
        agent_logging.log_event(
            agent_logging.LogEvent.TOOL_END,
            f"tool cancelled: {tool_name}",
            level=logging.INFO,
            logger=slog,
            tool=tool_name,
            cancelled=True,
        )

    def signal_headless_confirm(self, token: str, approved: bool) -> bool:
        """Signal the outcome of a headless (sub-agent) confirmation prompt.

        Returns True if the token was found and signalled, False if it was
        already expired or unknown (double-press / stale button).
        Called from the Telegram cb_subagent_confirm callback.
        """
        event = self._headless_confirm_events.pop(token, None)
        if event is None:
            return False  # expired or already resolved
        self._headless_confirm_results[token] = approved
        event.set()
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _requires_confirmation(self, tool_name: str, args: dict, description: str,
                               caller_depth: int = 0, caller_tag: str = "",
                               zone_path: str = "") -> dict:
        # In headless mode (sub-agents, caller_depth >= 1):
        #   shell/dangerous → always deny (too risky to run destructive commands unattended)
        #   file_read/sensitive, file_write, file_patch → require operator confirmation via Telegram
        if caller_depth >= 1:
            if tool_name == "shell":
                command = args.get("command", "")
                logger.warning(
                    "Headless sub-agent: dangerous shell command blocked (requires confirmation): %s",
                    command[:120],
                )
                return {
                    "success": False,
                    "output": "",
                    "error": (
                        f"Command blocked in headless mode (would require confirmation): {command[:200]}\n"
                        "Tip: use a safer alternative, or break the command into non-destructive steps."
                    ),
                    "exit_code": -1,
                }
            # Non-shell (sensitive file_read, file_write, file_patch): bridge to Telegram
            return self._headless_confirm_bridge(tool_name, args, description, caller_tag=caller_tag)

        token = secrets.token_hex(12)
        self._pending[token] = (tool_name, args)
        if zone_path:
            self._zone_paths[token] = zone_path
            self._zone_trackers[token] = self.grant_tracker
        logger.info("Built-in '%s' requires confirmation, token=%s", tool_name, token[:8])
        return {
            "requires_confirmation": True,
            "token": token,
            "description": description,
        }

    def _headless_confirm_bridge(self, tool_name: str, args: dict, description: str,
                                 caller_tag: str = "") -> dict:
        """Block the sub-agent thread until the operator approves or denies via Telegram.

        If the prompt callback is not wired (bot not ready) or times out, fails closed.
        """
        if self._prompt_approval_set is not None and tool_name in self._prompt_approval_set:
            caller_ok = True
            if self._current_prompt_id is not None and caller_tag:
                from sub_agent_registry import get_registry as _sar_get_registry
                _rec = _sar_get_registry().get(caller_tag.split()[0])
                if _rec is None or _rec.prompt_id != self._current_prompt_id:
                    caller_ok = False
            if caller_ok:
                logger.info(
                    "Headless sub-agent: auto-approving '%s' (prompt-scoped approve-all)", tool_name
                )
                token = secrets.token_hex(12)
                self._pending[token] = (tool_name, args)
                return self.confirm(token, _emit_lifecycle=False)

        if self._subagent_confirm_prompt_fn is None:
            logger.warning(
                "Headless sub-agent: Telegram bridge not wired — blocking %s (fail-closed)",
                tool_name,
            )
            return {
                "success": False,
                "output": "",
                "error": (
                    f"Sub-agent sensitive operation blocked: Telegram confirmation bridge "
                    f"not available for '{tool_name}'."
                ),
                "exit_code": -1,
            }

        token = secrets.token_hex(12)
        event = threading.Event()
        self._headless_confirm_events[token] = event
        self._pending[token] = (tool_name, args)

        logger.info(
            "Headless sub-agent: sending Telegram confirmation prompt for %s (token=%s)",
            tool_name, token[:8],
        )
        try:
            self._subagent_confirm_prompt_fn(token, tool_name, description, caller_tag)
        except Exception as exc:
            logger.error(
                "Headless sub-agent: failed to send Telegram prompt for %s: %s — blocking (fail-closed)",
                tool_name, exc,
            )
            self._headless_confirm_events.pop(token, None)
            self._pending.pop(token, None)
            return {
                "success": False,
                "output": "",
                "error": f"Sub-agent sensitive operation blocked: could not send Telegram prompt ({exc}).",
                "exit_code": -1,
            }

        answered = event.wait(timeout=self._subagent_confirm_timeout)
        if not answered:
            logger.warning(
                "Headless sub-agent: Telegram prompt timed out for %s (token=%s) — blocking",
                tool_name, token[:8],
            )
            self._headless_confirm_events.pop(token, None)
            self._pending.pop(token, None)
            self._headless_confirm_results.pop(token, None)
            return {
                "success": False,
                "output": "",
                "error": (
                    f"Sub-agent sensitive operation timed out waiting for operator confirmation "
                    f"('{tool_name}', {self._subagent_confirm_timeout}s)."
                ),
                "exit_code": -1,
            }

        approved = self._headless_confirm_results.pop(token, False)
        if not approved:
            self._pending.pop(token, None)
            logger.info("Headless sub-agent: operator denied %s (token=%s)", tool_name, token[:8])
            return {
                "success": False,
                "output": "",
                "error": f"Sub-agent sensitive operation denied by operator ('{tool_name}').",
                "exit_code": -1,
            }

        logger.info("Headless sub-agent: operator approved %s (token=%s) — executing", tool_name, token[:8])
        # The enclosing execute() call already owns this tool's lifecycle span,
        # so suppress confirm()'s own completion event to avoid double-logging.
        return self.confirm(token, _emit_lifecycle=False)

    def _run(self, tool_name: str, args: dict, caller_tag: str = "",
             chunk_callback: Optional[Callable[[str], None]] = None) -> dict:
        """Actually execute without any confirmation check."""
        handler = self._run_table.get(tool_name)
        if handler is None:
            return {"success": False, "output": "", "error": "Unknown built-in", "exit_code": -1}
        ctx = _CallContext(caller_tag=caller_tag, chunk_callback=chunk_callback)
        return handler(args, ctx)

    # ---- schedule ----

    def _exec_schedule(self, args: dict, caller_depth: int = 0) -> dict:
        return exec_schedule(self.scheduler, args, caller_depth=caller_depth)

    # ------------------------------------------------------------------
    # spawn_agent
    # ------------------------------------------------------------------

    def _exec_spawn_agent(self, args: dict, caller_depth: int = 0, caller_tag: str = "",
                          trace_id: str = "", options: Optional[SupervisionOptions] = None) -> dict:
        """Façade forwarder for the ``spawn_agent`` tool.

        Kept as a real method with the verbatim signature (Decision 4): the
        scheduler and several tests call it directly and assert on its
        ``call_args``. The body lives in ``AgentTools``.
        """
        return self._agents._exec_spawn_agent(
            args, caller_depth=caller_depth, caller_tag=caller_tag,
            trace_id=trace_id, options=options,
        )

    def _exec_get_agent_result(self, args: dict, caller_tag: str = "") -> dict:
        """Façade forwarder for the ``get_agent_result`` tool.

        Kept as a real method with the verbatim signature (Decision 4) because
        tests call it directly. The body lives in ``AgentTools``.
        """
        return self._agents._exec_get_agent_result(args, caller_tag=caller_tag)
