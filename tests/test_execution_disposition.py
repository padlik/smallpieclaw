"""R6: producer-stamped execution disposition across builtins, MCP, plans and the loop.

The private ``_execution_disposition`` marker is stamped locally by the semantic
boundary that attempted (or refused/deferred/cancelled) the operation, consumed
and stripped by the ReAct loop before any formatting/serialisation, and is the
only evidence the selection guard accepts.
"""

from __future__ import annotations

import itertools
import json
import os
import pathlib
import threading
import time
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import react_loop
from builtin_tools import agents as agents_mod
from builtin_tools import shell as shell_mod
from builtin_tools.descriptors import BUILTIN_TOOLS
from execution_plan import ExecutionPlan, PlanExecutor, PlanStep, _step_disposition
from memory_store import MemoryStore
from outcome_utils import (
    consume_execution,
    effective_execution,
    read_execution,
    strip_execution,
    with_execution,
)
from react_loop import ToolTrace, _outcome_guard_eligible
from sub_agent_registry import (
    SOURCE_ON_DEMAND,
    SubAgentRecord,
    SubAgentRegistry,
    deregister_run,
    get_registry,
    register_run,
)
from sub_agent_supervisor import ExecutionOwnership
from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
from tests.test_file_tools_zone import _make_policy
from tests.test_vision_query_confirm import (
    _FakeBuiltinExecutor as _VisionFakeExecutor,
)
from tests.test_vision_query_confirm import (
    _FakeConfirmationManager as _VisionFakeConfirmation,
)
from tests.test_vision_query_confirm import _make_policy as _make_vision_policy

MARKER = "_execution_disposition"


# ---------------------------------------------------------------------------
# outcome_utils primitives
# ---------------------------------------------------------------------------

class TestMarkerPrimitives:
    def test_with_execution_copies_and_stamps(self):
        original = {"success": True}
        stamped = with_execution(original, "executed")
        assert stamped is not original
        assert MARKER not in original
        assert stamped[MARKER] == "executed"

    def test_invalid_disposition_rejected(self):
        with pytest.raises(ValueError):
            with_execution({}, "bogus")  # type: ignore[arg-type]

    @pytest.mark.parametrize("raw", [None, "", "bogus", 1, ["executed"], "EXECUTED"])
    def test_invalid_or_absent_reads_unknown(self, raw):
        assert read_execution({MARKER: raw}) == "unknown"
        assert read_execution({}) == "unknown"
        assert read_execution("not a dict") == "unknown"

    @pytest.mark.parametrize("veto", [
        {"requires_confirmation": True},
        {"vision_reexecute": True},
        {"_operator_cancelled": True},
        {"_guard_refusal": True},
        {"error_type": "prohibited_path"},
    ])
    def test_vetoes_override_executed_marker_and_success(self, veto):
        outcome = with_execution({"success": True, **veto}, "executed")
        assert effective_execution(outcome) != "executed"
        assert _outcome_guard_eligible(outcome) is False

    @pytest.mark.parametrize("disposition", ["refused", "deferred", "cancelled"])
    def test_non_executed_overrides_success(self, disposition):
        assert _outcome_guard_eligible(with_execution({"success": True}, disposition)) is False

    def test_success_and_error_text_are_not_evidence(self):
        assert _outcome_guard_eligible({"success": True, "output": "ok"}) is False
        assert _outcome_guard_eligible({"success": False, "error": "boom"}) is False

    def test_consume_strips_marker(self):
        outcome, disposition = consume_execution(with_execution({"success": True}, "executed"))
        assert disposition == "executed"
        assert MARKER not in outcome
        assert strip_execution({"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------
# Real BuiltinExecutor producer matrix (all 22 table builtins)
# ---------------------------------------------------------------------------

def _fake_exited_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    """Popen stand-in over real pipes holding canned output; no process is started."""
    pipes = []
    for data in (stdout, stderr):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, data)
        os.close(write_fd)
        pipes.append(os.fdopen(read_fd, "rb", buffering=0))
    proc = SimpleNamespace(
        stdout=pipes[0], stderr=pipes[1], returncode=returncode, pid=0,
        poll=lambda: returncode, wait=lambda timeout=None: returncode,
    )
    return proc, [p.close for p in pipes]


def _record(agent_id: str, status: str = "done"):
    rec = SubAgentRecord(
        agent_id=agent_id, label="t", model="m", task_preview="t",
        started_at=time.time(), source=SOURCE_ON_DEMAND, status=status,
    )
    rec.ownership = ExecutionOwnership(origin=SOURCE_ON_DEMAND, owner_turn_id="r-owner")
    rec.result = "child-result"
    rec._result_event.set()
    return rec


@pytest.fixture
def world(make_builtin_executor, tmp_path):
    policy = _make_policy(str(tmp_path))
    memory = MemoryStore(str(tmp_path / "memory.json"))
    scheduler = MagicMock()
    scheduler.list_jobs.return_value = []
    snapshot = SimpleNamespace(
        system_prompt_tokens=1, chat_history_tokens=2,
        tool_defs_tokens=3, tool_defs_by_server={}, compaction_threshold=6,
        model_id="pinned/model", is_live=True, turn=1,
    )
    monitor = MagicMock()
    monitor.read.return_value = snapshot
    full = make_builtin_executor(
        path_policy=policy, memory=memory, scheduler=scheduler,
        context_monitor=monitor, log_store_path=str(tmp_path / "missing.sqlite"),
        vault_path=str(tmp_path / "vault.toml"),
    )
    full._graph_memory = MagicMock()
    full._graph_memory.format_for_prompt.return_value = "graph context"
    bare = make_builtin_executor()
    ws = policy.workspace_dir
    paths = {}
    for name in ("a.txt", "b.txt", "p.txt"):
        path = f"{ws}/{name}"
        with open(path, "w") as stream:
            stream.write("hello world\n")
        paths[name] = path
    registry = SubAgentRegistry()
    registry.register(_record("sa-a"))
    return SimpleNamespace(full=full, bare=bare, paths=paths, ws=ws, registry=registry)


def _executed_calls(w):
    p = w.paths
    return {
        "shell": ({"command": "echo hi"}, False),
        "file_read": ({"path": p["a.txt"]}, False),
        "file_write": ({"path": f"{w.ws}/new.txt", "content": "x"}, False),
        "file_patch": ({"path": p["p.txt"], "old_str": "hello", "new_str": "bye"}, False),
        "file_send": ({"path": p["a.txt"]}, False),
        "file_diff": ({"path_a": p["a.txt"], "path_b": p["b.txt"]}, False),
        "schedule": ({"action": "list"}, False),
        "spawn_agent": ({"task": "do it"}, False),
        "get_agent_result": ({"agent_id": "sa-a", "timeout": 1}, False),
        "wait_for_any_agent": ({"agent_ids": ["sa-a"], "timeout": 1}, False),
        "cancel_agent": ({"agent_id": "managed"}, False),
        "memory_write": ({"action": "get", "key": "k"}, False),
        "memory_graph_search": ({"query": "q"}, False),
        "memory_graph_store": ({"content": "fact"}, True),
        "secret_get": ({"key": "nokey"}, True),
        "log_query": ({}, False),
        "log_detail": ({"id": 1}, False),
        "shell_env_set": ({"key": "FOO", "value": "bar"}, False),
        "shell_env_unset": ({"key": "FOO"}, False),
        "shell_env_list": ({}, False),
        "shell_env_get": ({"key": "FOO"}, False),
        "context_profile": ({}, False),
    }


class TestBuiltinProducerMatrix:
    def test_matrix_covers_every_registered_builtin(self, world):
        covered = set(_executed_calls(world))
        assert covered | {"vision_query"} == set(BUILTIN_TOOLS)

    @pytest.mark.parametrize("tool", sorted(set(BUILTIN_TOOLS) - {"vision_query"}))
    def test_attempted_operation_is_executed(self, world, tool):
        args, via_confirm = _executed_calls(world)[tool]
        ex = world.full
        created: list = []
        if tool == "spawn_agent":
            ex._sub_agent_factory = _spawn_factory(created)
        with patch.object(agents_mod, "_get_agent_registry", return_value=world.registry):
            outcome = ex.execute(tool, dict(args), caller_tag="main r-owner")
            if via_confirm:
                assert read_execution(outcome) == "deferred"
                assert outcome["requires_confirmation"] is True
                outcome = ex.confirm(outcome["token"])
        assert read_execution(outcome) == "executed", (tool, outcome)
        assert effective_execution(outcome) == "executed"
        if tool == "spawn_agent":
            # Real admission ran: the configured factory built exactly one runner
            # for this task and the producer-stamped result names that runner.
            assert len(created) == 1
            kwargs, runner = created[0]
            assert outcome["agent_id"] == runner.agent_id
            assert runner.ran.wait(5), "controlled runner never executed the task"
            assert "do it" in runner.tasks[0]
            assert runner.results == ["child-done"]
            deadline = time.monotonic() + 5
            while get_registry().get(runner.agent_id) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert get_registry().get(runner.agent_id) is None

    def test_spawn_agent_without_factory_is_refused_without_submission(self, world):
        ex = world.full
        ex._sub_agent_factory = None
        with patch.object(agents_mod, "_get_agent_registry", return_value=world.registry), \
                patch.object(ex._supervisor, "submit") as submit:
            outcome = ex.execute("spawn_agent", {"task": "do it"}, caller_tag="main r-owner")
        assert read_execution(outcome) == "refused"
        assert outcome["success"] is False
        assert "sub_agent_factory not configured" in outcome["error"]
        submit.assert_not_called()

    @pytest.mark.parametrize("tool", sorted(
        set(BUILTIN_TOOLS) - {"vision_query", "shell_env_unset", "shell_env_list", "shell_env_get"}
    ))
    def test_missing_args_or_collaborator_is_refused_even_with_success(self, world, tool):
        outcome = world.bare.execute(tool, {})
        assert read_execution(outcome) == "refused", (tool, outcome)
        assert _outcome_guard_eligible(outcome) is False

    def test_unknown_builtin_refused(self, world):
        assert read_execution(world.bare.execute("nope", {})) == "refused"

    def test_log_tools_without_store_refuse_despite_success_flag(self, world):
        for tool, args in (("log_query", {}), ("log_detail", {"id": 1})):
            outcome = world.bare.execute(tool, args)
            assert outcome["success"] is True
            assert read_execution(outcome) == "refused"

    def test_valid_not_found_read_is_executed_failure(self, world):
        outcome = world.full.execute("file_read", {"path": f"{world.ws}/absent.txt"})
        assert outcome["success"] is False
        assert read_execution(outcome) == "executed"

    def test_nonzero_shell_exit_is_executed_failure(self, world):
        proc, closers = _fake_exited_proc(returncode=2, stderr=b"boom\n")
        try:
            with patch.object(shell_mod, "_spawn", return_value=proc) as spawn:
                outcome = world.full.execute("shell", {"command": "false"})
        finally:
            for closer in closers:
                closer()
        spawn.assert_called_once()
        assert outcome["success"] is False
        assert outcome["exit_code"] == 2
        assert read_execution(outcome) == "executed"
        assert _outcome_guard_eligible(outcome) is True

    def test_empty_shell_command_refused_both_paths(self, world):
        with patch.object(shell_mod, "_spawn") as spawn:
            for outcome in (
                world.full.execute("shell", {"command": "  "}),
                world.full._run("shell", {"command": ""}),
            ):
                assert read_execution(outcome) == "refused"
                assert _outcome_guard_eligible(outcome) is False
        spawn.assert_not_called()

    @pytest.mark.parametrize("route", ["direct", "staged"])
    @pytest.mark.parametrize("kind", ["directory", "oversize"])
    def test_file_send_prerequisite_rejection_is_refused_without_send(
        self, world, tmp_path, kind, route,
    ):
        base = pathlib.Path(world.ws) if route == "direct" else tmp_path / "outside"
        base.mkdir(exist_ok=True)
        target = base / "target"
        if kind == "directory":
            target.mkdir()
        else:
            with open(target, "wb") as stream:
                stream.truncate(world.full._files._MAX_FILE_SIZE + 1)
        args = {"path": str(target)}
        outcome = world.full.execute("file_send", dict(args))
        if route == "staged":
            assert outcome["requires_confirmation"] is True
            assert read_execution(outcome) == "deferred"
            assert _outcome_guard_eligible(outcome) is False
            assert "send_file" not in outcome
            outcome = world.full.confirm(outcome["token"])
        else:
            assert not outcome.get("requires_confirmation")
        payload, disposition = consume_execution(outcome)
        assert disposition == "refused"
        assert effective_execution(outcome) == "refused"
        assert _outcome_guard_eligible(outcome) is False
        assert "send_file" not in outcome and "send_file" not in payload
        assert set(payload) == {"success", "output", "error", "exit_code"}
        assert payload["success"] is False
        assert payload["output"] == ""
        assert payload["exit_code"] == 1
        if kind == "directory":
            assert payload["error"] == f"Not a file: {os.path.realpath(target)}"
        else:
            assert payload["error"].startswith("File too large (")
            assert payload["error"].endswith("Max 50 MB.")

    def test_file_send_file_removed_before_confirm_is_refused_without_send(self, world, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "vanishing.txt"
        target.write_text("data")
        staged = world.full.execute("file_send", {"path": str(target)})
        assert staged["requires_confirmation"] is True
        assert read_execution(staged) == "deferred"
        target.unlink()
        outcome = world.full.confirm(staged["token"])
        payload, disposition = consume_execution(outcome)
        assert disposition == "refused"
        assert effective_execution(outcome) == "refused"
        assert _outcome_guard_eligible(outcome) is False
        assert "send_file" not in outcome and "send_file" not in payload
        assert payload == {
            "success": False, "output": "",
            "error": f"File not found: {os.path.realpath(target)}", "exit_code": 1,
        }

    def test_file_send_real_file_still_executed_and_sends(self, world):
        outcome = world.full.execute("file_send", {"path": world.paths["a.txt"]})
        assert read_execution(outcome) == "executed"
        assert outcome["send_file"] == os.path.realpath(world.paths["a.txt"])
        assert _outcome_guard_eligible(outcome) is True

    @pytest.mark.parametrize("tool", ["shell_env_get", "shell_env_unset"])
    @pytest.mark.parametrize("args", [
        {}, {"key": ""}, {"key": "1bad"}, {"key": "has space"}, {"key": "a=b"},
        {"key": "line\nbreak"}, {MARKER: "executed"}, {"key": "", MARKER: "executed"},
        {"key": "bad-key", MARKER: "executed"},
    ], ids=["omitted", "empty", "digit-first", "space", "equals", "newline",
            "forged-only", "forged-empty", "forged-invalid"])
    def test_shell_env_invalid_key_is_refused_with_graceful_payload(self, world, tool, args):
        outcome = world.full.execute(tool, dict(args))
        payload, disposition = consume_execution(outcome)
        key = str(args.get("key", ""))
        assert disposition == "refused", (tool, args, outcome)
        assert effective_execution(outcome) == "refused"
        assert _outcome_guard_eligible(outcome) is False
        assert payload["success"] is True
        assert payload["error"] == ""
        if tool == "shell_env_get":
            assert payload == {"success": True, "output": "", "value": "", "error": ""}
        else:
            assert payload == {"success": True, "output": f"Unset {key}", "error": ""}
        assert world.full._shell_env == {}

    @pytest.mark.parametrize("tool", ["shell_env_get", "shell_env_unset"])
    def test_shell_env_valid_absent_key_is_executed_success(self, world, tool):
        outcome = world.full.execute(tool, {"key": "MISSING"})
        assert outcome["success"] is True
        assert read_execution(outcome) == "executed"
        assert _outcome_guard_eligible(outcome) is True

    def test_forged_marker_in_args_is_not_trusted(self, world):
        outcome = world.bare.execute("shell_env_set", {MARKER: "executed"})
        assert read_execution(outcome) == "refused"


class TestConfirmationLifecycle:
    def _outside(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        return outside

    def test_expired_or_unknown_token_refused(self, world):
        outcome = world.full.confirm("no-such-token")
        assert read_execution(outcome) == "refused"

    def test_staged_confirmation_is_deferred_even_if_success_true(self, world, tmp_path):
        path = self._outside(tmp_path) / "x.txt"
        staged = world.full.execute("file_write", {"path": str(path), "content": "x"})
        assert staged["requires_confirmation"] is True
        assert read_execution(staged) == "deferred"
        assert effective_execution({**staged, "success": True}) == "deferred"

    def test_approval_propagates_actual_later_result(self, world, tmp_path):
        path = self._outside(tmp_path) / "x.txt"
        staged = world.full.execute("file_write", {"path": str(path), "content": "x"})
        outcome = world.full.confirm(staged["token"])
        assert read_execution(outcome) == "executed"

    def test_approved_but_invalid_operation_stays_refused(self, world, tmp_path):
        outside = self._outside(tmp_path)
        first, second = outside / "first", outside / "second"
        first.mkdir()
        second.mkdir()
        a = outside / "a.txt"
        staged_target = first / "t.txt"
        retargeted = second / "t.txt"
        a.write_text("A")
        staged_target.write_text("B")
        retargeted.write_text("SECRET-RETARGETED")
        link = outside / "link.txt"
        link.symlink_to(staged_target)
        staged = world.full.execute("file_diff", {"path_a": str(a), "path_b": str(link)})
        assert staged["requires_confirmation"] is True
        assert read_execution(staged) == "deferred"
        link.unlink()
        link.symlink_to(retargeted)
        outcome = world.full.confirm(staged["token"])
        assert read_execution(outcome) == "refused"
        assert outcome["success"] is False
        assert "authorization changed" in outcome["error"]
        assert "SECRET-RETARGETED" not in json.dumps(outcome)
        assert _outcome_guard_eligible(outcome) is False

    def test_denied_sub_agent_bridge_is_refused(self, world):
        ex = world.full
        outcome = ex._headless_confirm_bridge("file_write", {"path": "/x"}, "d", caller_tag="sa-1")
        assert read_execution(outcome) in ("refused", "cancelled")
        assert _outcome_guard_eligible(outcome) is False

    @pytest.mark.parametrize("cancel_when", ["before_prompt", "after_confirmation_returns"])
    def test_cancelled_bridge_never_executes_and_overrides_success(self, world, tmp_path, cancel_when):
        ex = world.full
        target = tmp_path / "bridge-cancel.txt"
        runner = _Runner(lambda _t, _task: "", None, "cancel-me")
        register_run(runner, source=SOURCE_ON_DEMAND, label="cancel-me", task_preview="t")
        prompt_fn = MagicMock()
        coordinator = MagicMock()
        ex._coordinator = coordinator
        ex._subagent_confirm_prompt_fn = prompt_fn

        def approve_then_cancel(*_a, **_k):
            runner.cancel()
            return True

        if cancel_when == "before_prompt":
            runner.cancel()
        else:
            coordinator.request_headless_confirmation.side_effect = approve_then_cancel

        stage = MagicMock(wraps=ex._pending_confirmations.stage)
        try:
            with patch("builtin_executor.secrets.token_hex", return_value="tok-cancel"), \
                    patch.object(ex._pending_confirmations, "stage", stage), \
                    patch.object(ex, "confirm") as confirm:
                outcome = ex._headless_confirm_bridge(
                    "file_write", {"path": str(target), "content": "x"}, "d",
                    caller_tag=f"{runner.agent_id} r-owner",
                )
        finally:
            deregister_run(runner.agent_id)

        stage.assert_called_once()
        assert stage.call_args.args[0] == "tok-cancel"
        assert read_execution(outcome) == "cancelled"
        assert outcome["success"] is False
        assert ex._pending_confirmations.take("tok-cancel") is None
        confirm.assert_not_called()
        assert not target.exists()
        prompt_fn.assert_not_called()
        if cancel_when == "before_prompt":
            coordinator.request_headless_confirmation.assert_not_called()
        else:
            coordinator.request_headless_confirmation.assert_called_once()
        # a conflicting success flag can never rescue a cancelled operation
        forged = {**outcome, "success": True}
        assert effective_execution(forged) == "cancelled"
        assert _outcome_guard_eligible(forged) is False


# ---------------------------------------------------------------------------
# ReAct loop: guard eligibility through the actual BuiltinExecutor
# ---------------------------------------------------------------------------

_SHELL = '{"action": "tool", "tool": "shell", "args": {"command": "%s"}}'
_FINISH = '{"action": "finish", "result": "done"}'


def _run(world, replies, *, confirm=None, **overrides):
    accepted = MagicMock()
    llm = ScriptedLLM(replies)
    ctx = build_context(llm, RecordingExecutor(), on_accepted=accepted, **overrides)
    ctx.builtin_executor = world.full
    if confirm is not None:
        ctx.confirmation = MagicMock()
        ctx.confirmation.request_confirmation.side_effect = confirm
    progress: list[str] = []
    with patch("react_loop._build_system_prompt", return_value=("sys", None)):
        react_loop.react_loop(ctx, "goal", progress_callback=progress.append)
    return accepted, llm


def _first_tool_state(world, action, confirm=None):
    ctx = build_context(ScriptedLLM([_FINISH]), RecordingExecutor())
    ctx.builtin_executor = world.full
    if confirm is not None:
        ctx.confirmation = MagicMock()
        ctx.confirmation.request_confirmation.side_effect = confirm
    outcome = react_loop._dispatch_tool(ctx, action, lambda _m: None)
    return ctx, outcome


class TestLoopGuardThroughRealExecutor:
    def test_mocked_nonzero_exit_clears_guard(self, world):
        failed = {"success": False, "output": "", "error": "exit 2", "exit_code": 2}
        with patch.object(world.full._shell, "_run_shell_subprocess", return_value=failed):
            accepted, _ = _run(world, [_SHELL % "false", _FINISH])
        accepted.assert_called_once()

    def test_three_identical_empty_command_refusals_abort_before_finish(self, world):
        # The existing doom-loop policy aborts on the 3rd identical failure, so
        # the trailing finish is never consumed and nothing clears the guard.
        with patch.object(world.full._shell, "_run_shell_subprocess") as subprocess_run:
            accepted, llm = _run(world, [_SHELL % ""] * 3 + [_FINISH])
        subprocess_run.assert_not_called()
        accepted.assert_not_called()
        assert len(llm.calls) == 3

    def test_fewer_than_three_empty_command_refusals_then_valid_finish_clears_once(self, world):
        order: list[int] = []
        holder: dict = {}
        accepted = MagicMock(side_effect=lambda *a, **k: order.append(len(holder["llm"].calls)))
        with patch.object(world.full._shell, "_run_shell_subprocess") as subprocess_run:
            llm = ScriptedLLM([_SHELL % ""] * 2 + [_FINISH])
            holder["llm"] = llm
            ctx = build_context(llm, RecordingExecutor(), on_accepted=accepted)
            ctx.builtin_executor = world.full
            with patch("react_loop._build_system_prompt", return_value=("sys", None)):
                react_loop.react_loop(ctx, "goal", progress_callback=lambda _m: None)
        subprocess_run.assert_not_called()
        accepted.assert_called_once()
        assert order == [3]  # acceptance happened only after the finish turn

    def test_tool_step_alone_does_not_clear_when_refused(self, world):
        seen = []
        ctx = build_context(ScriptedLLM([_SHELL % ""]), RecordingExecutor())
        ctx.builtin_executor = world.full
        outcome = react_loop._dispatch_tool(ctx, json.loads(_SHELL % ""), lambda _m: None)
        outcome, disposition = consume_execution(outcome)
        seen.append(disposition)
        assert seen == ["refused"]
        assert MARKER not in outcome

    def test_non_shell_missing_key_refused(self, world):
        ctx = build_context(ScriptedLLM([_FINISH]), RecordingExecutor())
        ctx.builtin_executor = world.full
        action = {"action": "tool", "tool": "file_read", "args": {}}
        _, disposition = consume_execution(react_loop._dispatch_tool(ctx, action, lambda _m: None))
        assert disposition == "refused"

    def test_unknown_tool_refused(self, world):
        ctx = build_context(ScriptedLLM([_FINISH]), RecordingExecutor())
        ctx.builtin_executor = None
        action = {"action": "tool", "tool": "zzz", "args": {}}
        _, disposition = consume_execution(react_loop._dispatch_tool(ctx, action, lambda _m: None))
        assert disposition == "refused"

    def test_operator_denial_is_cancelled(self, world, tmp_path):
        path = tmp_path / "outside" / "x.txt"
        path.parent.mkdir()
        action = {"action": "tool", "tool": "file_write", "args": {"path": str(path), "content": "x"}}
        ctx = build_context(ScriptedLLM([_FINISH]), RecordingExecutor())
        ctx.builtin_executor = world.full
        ctx.confirmation = MagicMock()
        ctx.confirmation.request_confirmation.return_value = False
        outcome = react_loop._dispatch_tool(ctx, action, lambda _m: None)
        assert effective_execution(outcome) == "cancelled"
        assert not path.exists()

    def test_operator_approval_propagates_executed(self, world, tmp_path):
        path = tmp_path / "outside" / "x.txt"
        path.parent.mkdir()
        action = {"action": "tool", "tool": "file_write", "args": {"path": str(path), "content": "x"}}
        ctx = build_context(ScriptedLLM([_FINISH]), RecordingExecutor())
        ctx.builtin_executor = world.full
        ctx.confirmation = MagicMock()
        ctx.confirmation.request_confirmation.return_value = True
        outcome = react_loop._dispatch_tool(ctx, action, lambda _m: None)
        assert effective_execution(outcome) == "executed"
        assert path.read_text() == "x"

    def test_marker_never_reaches_wire_history_or_trace(self, world):
        traces: list[ToolTrace] = []
        accepted, llm = _run(world, [_SHELL % "echo hi", _FINISH], on_tool_trace=traces.append)
        accepted.assert_called_once()
        for call in llm.calls:
            assert MARKER not in json.dumps(call.messages, default=str)
        assert [t.execution_disposition for t in traces] == ["executed"]
        assert MARKER not in repr(traces)

    def test_valid_finish_still_accepted_without_tools(self, world):
        accepted, _ = _run(world, [_FINISH])
        accepted.assert_called_once()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

_RUNNER_SEQ = itertools.count(1)


class _Runner:
    """Registry-complete fake: the real register_run/deregister_run read these attributes."""

    def __init__(self, behaviour, on_trace, label, *, cancel_event=None, max_iterations=8):
        self._behaviour = behaviour
        self._on_trace = on_trace
        self.label = label
        self.agent_id = f"sa-{label}-{next(_RUNNER_SEQ)}"
        self.model_id = "m"
        self._model_id = "m"
        self.trace_id = "r-fixture"
        self.short_term = None
        self._llm = None
        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._agent = SimpleNamespace(max_iterations=max_iterations, _on_step=None)
        self.tasks: list[str] = []
        self.results: list[str] = []
        self.errors: list[BaseException] = []
        self.closed = False
        self.ran = threading.Event()

    def run(self, task, prompt_id=None):
        self.tasks.append(task)
        try:
            result = self._behaviour(self._on_trace, task)
            self.results.append(result)
            return result
        except BaseException as exc:
            self.errors.append(exc)
            raise
        finally:
            self.ran.set()

    def cancel(self):
        self._cancel_event.set()

    def close(self):
        self.closed = True


def _trace(disposition):
    return ToolTrace("shell", "", True, 1.0, execution_disposition=disposition)


def _factory(behaviours, calls=None, runners=None):
    """Return a factory whose step runners replay scripted trace/result behaviour."""
    def factory(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        label = kwargs["label"]
        behaviour = behaviours.get(label.split("-", 1)[1] if label.startswith("plan-") else label)
        if behaviour is None:
            behaviour = lambda on_trace, task: '{"success": true, "output": "x"}'  # noqa: E731
        runner = _Runner(
            behaviour, kwargs.get("on_tool_trace"), label,
            cancel_event=kwargs.get("cancel_event"),
            max_iterations=kwargs.get("max_iterations", 8),
        )
        if runners is not None:
            runners.append(runner)
        return runner
    return factory


def _spawn_factory(created):
    """Deterministic spawn_agent factory: records kwargs and the controlled child runner."""
    def factory(**kwargs):
        runner = _Runner(
            lambda _on_trace, _task: "child-done", kwargs.get("on_tool_trace"),
            kwargs.get("label") or "spawn",
            cancel_event=kwargs.get("cancel_event"),
            max_iterations=kwargs.get("max_iterations") or 8,
        )
        created.append((kwargs, runner))
        return runner
    return factory


def _assert_ran_once_and_deregistered(runners):
    assert runners, "no runner was created, so the scenario proved nothing"
    for runner in runners:
        assert len(runner.tasks) == 1, runner.label
        assert not runner.errors, (runner.label, runner.errors)
        assert get_registry().get(runner.agent_id) is None, runner.label


def _ok(on_trace, task):
    on_trace(_trace("executed"))
    return '{"success": true, "output": "x", "error": "", "exit_code": 0}'


def _plan(*step_ids, deps=None):
    deps = deps or {}
    return ExecutionPlan(
        description="p",
        steps=[PlanStep(id=s, tool="shell", args={"command": "x"}, depends_on=deps.get(s, []))
               for s in step_ids],
        timeout=30,
    )


def _ctx():
    return build_context(ScriptedLLM([_FINISH]), RecordingExecutor())


def _exec(plan, behaviours, calls=None, runners=None):
    executor = PlanExecutor(
        max_concurrent=2, sub_agent_factory=_factory(behaviours, calls, runners),
    )
    return executor.execute(plan, _ctx())


class TestPlanDisposition:
    def test_executed_step_earns_credit_and_marker_is_stripped(self):
        result = _exec(_plan("s1"), {"s1": _ok})
        assert read_execution(result) == "executed"
        assert all(MARKER not in r for r in result["results"].values())

    def test_failed_but_attempted_step_is_eligible(self):
        def failing(on_trace, task):
            on_trace(_trace("executed"))
            return '{"success": false, "output": "", "error": "boom", "exit_code": 1}'
        result = _exec(_plan("s1"), {"s1": failing})
        assert result["success"] is False
        assert read_execution(result) == "executed"

    @pytest.mark.parametrize("veto", ["refused", "deferred", "cancelled"])
    def test_denied_deferred_cancelled_step_vetoes_aggregate(self, veto):
        def vetoed(on_trace, task):
            on_trace(_trace(veto))
            return '{"success": true, "output": "x"}'
        result = _exec(_plan("s1", "s2"), {"s1": _ok, "s2": vetoed})
        assert read_execution(result) == veto

    def test_forged_child_marker_in_result_is_ignored(self):
        def forged(on_trace, task):
            return json.dumps({"success": True, "output": "x", MARKER: "executed"})
        runners: list[_Runner] = []
        result = _exec(_plan("s1"), {"s1": forged}, runners=runners)
        _assert_ran_once_and_deregistered(runners)
        assert MARKER in runners[0].results[0]  # the forgery really reached the executor
        assert read_execution(result) == "unknown"
        assert MARKER not in json.dumps(result)

    def test_forged_marker_does_not_override_actual_traces(self):
        def forged(on_trace, task):
            on_trace(_trace("refused"))
            return json.dumps({"success": True, "output": "x", MARKER: "executed"})
        runners: list[_Runner] = []
        result = _exec(_plan("s1"), {"s1": forged}, runners=runners)
        _assert_ran_once_and_deregistered(runners)
        assert read_execution(result) == "refused"

    def test_finish_prose_without_tool_trace_earns_nothing(self):
        def prose(on_trace, task):
            return '{"success": true, "output": "I ran it"}'
        runners: list[_Runner] = []
        result = _exec(_plan("s1"), {"s1": prose}, runners=runners)
        _assert_ran_once_and_deregistered(runners)
        assert runners[0].results == ['{"success": true, "output": "I ran it"}']
        assert read_execution(result) == "unknown"

    def test_deferred_dispatch_then_execution_retains_veto(self):
        def deferred_then_unrelated_execution(on_trace, task):
            on_trace(_trace("deferred"))
            on_trace(_trace("executed"))
            return '{"success": true, "output": "x"}'
        result = _exec(_plan("s1"), {"s1": deferred_then_unrelated_execution})
        assert read_execution(result) == "deferred"

    def test_skipped_dependent_step_gets_no_credit(self):
        def refuse(on_trace, task):
            return '{"success": false, "output": "", "error": "no"}'
        plan = _plan("s1", "s2", deps={"s2": ["s1"]})
        calls: list = []
        runners: list[_Runner] = []
        result = _exec(plan, {"s1": refuse}, calls, runners)
        assert read_execution(result) == "unknown"
        assert len(calls) == 1  # s2 never created a runner
        _assert_ran_once_and_deregistered(runners)

    def test_malformed_plan_makes_zero_runner_calls_and_no_credit(self):
        calls: list = []
        factory = _factory({}, calls)
        ctx = _ctx()
        ctx.builtin_executor = SimpleNamespace(_sub_agent_factory=factory)
        plan_data = {"description": "bad", "steps": [{"id": "a", "tool": "shell", "depends_on": ["zz"]}]}
        flags: list[str] = []
        msg, _, ok = react_loop._run_plan(ctx, plan_data, 8, lambda _m: None, on_disposition=flags.append)
        assert ok is False and calls == []
        assert flags == [] or flags == ["unknown"]
        assert MARKER not in msg

    def test_no_factory_and_runner_creation_failure_earn_no_credit(self):
        ctx = _ctx()
        ctx.builtin_executor = None
        executor = PlanExecutor(max_concurrent=1)
        assert read_execution(executor.execute(_plan("s1"), ctx)) == "unknown"

        def boom(**kwargs):
            raise RuntimeError("no runner")
        res = PlanExecutor(max_concurrent=1, sub_agent_factory=boom).execute(_plan("s1"), _ctx())
        assert read_execution(res) == "unknown"

    def test_retry_is_attempt_local_no_stale_credit(self):
        attempts = {"n": 0}

        def flaky(on_trace, task):
            attempts["n"] += 1
            if attempts["n"] == 1:
                on_trace(_trace("executed"))
                return json.dumps({
                    "success": False, "output": "", "error": "slow",
                    "error_type": "tool_timeout", "recoverable": True,
                })
            return '{"success": false, "output": "", "error": "still failing"}'

        runners: list[_Runner] = []
        executor = PlanExecutor(
            max_concurrent=1, sub_agent_factory=_factory({"s1": flaky}, runners=runners),
        )
        with patch.object(threading.Event, "wait", return_value=False):
            result = executor.execute(_plan("s1"), _ctx())
        assert attempts["n"] >= 2
        assert len(runners) == attempts["n"]
        assert all(get_registry().get(r.agent_id) is None for r in runners)
        assert read_execution(result) == "unknown"

    def test_diagnostic_agent_gets_no_credit(self):
        calls: list = []

        def failing(on_trace, task):
            return json.dumps({
                "success": False, "output": "", "error": "x", "error_type": "bad_thing",
                "recoverable": False,
            })

        runners: list[_Runner] = []

        def diag(on_trace, task):
            # Diagnostics receive no observer by design; tolerate that, and if one
            # were ever supplied its activity must still earn no credit.
            if on_trace is not None:
                on_trace(_trace("executed"))
            return "diagnosis"

        def factory(**kwargs):
            calls.append(kwargs)
            behaviour = diag if kwargs["label"].startswith("diagnose-") else failing
            runner = _Runner(
                behaviour, kwargs.get("on_tool_trace"), kwargs["label"],
                cancel_event=kwargs.get("cancel_event"),
                max_iterations=kwargs.get("max_iterations", 8),
            )
            runners.append(runner)
            return runner

        result = PlanExecutor(max_concurrent=1, sub_agent_factory=factory).execute(_plan("s1"), _ctx())
        assert any(c["label"].startswith("diagnose-") for c in calls)
        assert all(c.get("on_tool_trace") is None for c in calls if c["label"].startswith("diagnose-"))
        diagnostics = [r for r in runners if r.label.startswith("diagnose-")]
        assert diagnostics
        for runner in diagnostics:
            assert runner.results == ["diagnosis"]  # completed, no swallowed callback error
            assert not runner.errors
            assert runner.closed
            assert get_registry().get(runner.agent_id) is None
        assert [r for r in runners if not r.label.startswith("diagnose-")]
        assert read_execution(result) == "unknown"

    def test_substituted_dependency_results_carry_no_marker(self):
        seen: list[str] = []

        def first(on_trace, task):
            on_trace(_trace("executed"))
            return '{"success": true, "output": "first-out"}'

        def second(on_trace, task):
            seen.append(task)
            on_trace(_trace("executed"))
            return '{"success": true, "output": "x"}'

        plan = ExecutionPlan(description="p", timeout=30, steps=[
            PlanStep(id="s1", tool="shell", args={"command": "a"}),
            PlanStep(id="s2", tool="shell", args={"command": "{{s1}}"}, depends_on=["s1"]),
        ])
        _exec(plan, {"s1": first, "s2": second})
        assert seen and MARKER not in seen[0]

    def test_loop_plan_branch_eligibility_and_serialisation(self):
        def run_plan(behaviours):
            ctx = _ctx()
            ctx.builtin_executor = SimpleNamespace(_sub_agent_factory=_factory(behaviours))
            sink: list[str] = []
            flags: list[str] = []
            msg, new_max, ok = react_loop._run_plan(
                ctx, {"description": "d", "steps": [{"id": "s1", "tool": "shell", "args": {}}]},
                8, sink.append, on_disposition=flags.append,
            )
            return msg, ok, flags

        msg, ok, flags = run_plan({"s1": _ok})
        assert ok is True and flags == ["executed"]
        assert MARKER not in msg
        msg, ok, flags = run_plan({"s1": lambda t, task: '{"success": true, "output": "x"}'})
        assert flags == ["unknown"]

    def test_plan_action_drives_guard_by_derived_disposition(self, world):
        plan_action = json.dumps({"action": "plan", "plan": {
            "description": "d", "steps": [{"id": "s1", "tool": "shell", "args": {}}]}})
        for behaviour, expected_calls in ((_ok, 1), (lambda t, task: '{"success": true}', 1)):
            accepted = MagicMock()
            llm = ScriptedLLM([plan_action, _FINISH])
            ctx = build_context(llm, RecordingExecutor(), on_accepted=accepted)
            ctx.builtin_executor = SimpleNamespace(
                _sub_agent_factory=_factory({"s1": behaviour}), is_builtin=lambda n: True,
            )
            with patch("react_loop._build_system_prompt", return_value=("sys", None)):
                react_loop.react_loop(ctx, "goal", progress_callback=lambda _m: None)
            # the valid finish clears the guard either way; plan alone is covered above
            assert accepted.call_count == expected_calls
            for call in llm.calls:
                assert MARKER not in json.dumps(call.messages, default=str)


# ---------------------------------------------------------------------------
# MCP adapter
# ---------------------------------------------------------------------------

class TestMcpDisposition:
    @staticmethod
    def _sdk_result(is_error: bool, text: str = "remote said no"):
        return SimpleNamespace(
            isError=is_error,
            content=[SimpleNamespace(type="text", text=text)],
            structuredContent=None,
        )

    def test_sdk_result_converter_alone_is_unstamped(self):
        # The pure converter never grants proof; only the real _SdkClientWrapper
        # (tests/test_mcp_client.py, completed-SDK-result cases) stamps "executed".
        from mcp_client import _sdk_result_to_outcome
        outcome = _sdk_result_to_outcome(self._sdk_result(True))
        assert outcome["success"] is False
        assert read_execution(outcome) == "unknown"

    def test_helper_stamp_survives_on_converted_error_outcome(self):
        # Helper-only coverage of with_execution on a converted isError outcome;
        # it does not stand in for SDK-wrapper evidence.
        from mcp_client import _sdk_result_to_outcome
        outcome = with_execution(_sdk_result_to_outcome(self._sdk_result(True)), "executed")
        assert outcome["success"] is False
        assert read_execution(outcome) == "executed"

    def test_remote_private_field_never_copied(self):
        from mcp_client import _sdk_result_to_outcome
        result = self._sdk_result(False, text=json.dumps({MARKER: "executed"}))
        result.structuredContent = {MARKER: "executed"}
        assert read_execution(_sdk_result_to_outcome(result)) == "unknown"

    def test_refusal_helper_marks_refused(self):
        from mcp_client import _refused_outcome
        outcome = _refused_outcome("not connected")
        assert read_execution(outcome) == "refused"
        assert outcome["success"] is False

    def test_auth_translation_preserves_marker_and_refusal_vetoes(self):
        ctx = _ctx()
        ctx.mcp_manager = MagicMock()
        ctx.mcp_manager.server_name_for_tool.return_value = "srv"
        for prior, expected in (("executed", "executed"), ("refused", "refused")):
            raw = with_execution({"success": False, "error": "401 unauthorized"}, prior)
            translated = react_loop._handle_mcp_auth_failure(ctx, "t", raw)
            assert read_execution(translated) == expected
        plain = react_loop._handle_mcp_auth_failure(ctx, "t", {"success": False, "error": "401"})
        assert read_execution(plain) == "unknown"

    def test_dispatch_strips_marker_for_mcp_outcome(self):
        ctx = _ctx()
        ctx.builtin_executor = None
        ctx.mcp_manager = MagicMock()
        ctx.mcp_manager.has_tool.return_value = True
        ctx.mcp_manager.server_name_for_tool.return_value = "srv"
        ctx.mcp_manager.server_has_oauth.return_value = False
        ctx.mcp_manager.call_tool.return_value = with_execution(
            {"success": False, "output": "", "error": "tool error", "exit_code": -1}, "executed",
        )
        outcome = react_loop._dispatch_tool(
            ctx, {"action": "tool", "tool": "remote", "args": {}}, lambda _m: None,
        )
        outcome, disposition = consume_execution(outcome)
        assert disposition == "executed" and MARKER not in outcome


_AUTH_VETOES = [
    pytest.param({"_guard_refusal": True}, "refused", id="guard_refusal"),
    pytest.param({"_operator_cancelled": True}, "cancelled", id="operator_cancelled"),
    pytest.param({"requires_confirmation": True}, "deferred", id="requires_confirmation"),
    pytest.param({"vision_reexecute": True}, "deferred", id="vision_reexecute"),
    pytest.param({"error_type": "prohibited_path"}, "refused", id="prohibited_path"),
    pytest.param({"error_type": "scheduled_approval_denied"}, "refused", id="scheduled_approval_denied"),
    pytest.param({"error_type": "confirmation_failed"}, "refused", id="confirmation_failed"),
]
_MCP_ACTION = json.dumps({"action": "tool", "tool": "remote", "args": {}})


def _auth_raw(extra=None, prior="executed"):
    raw = {"success": False, "output": "", "error": "401 unauthorized", "exit_code": 1, **(extra or {})}
    return with_execution(raw, prior) if prior else raw


def _mcp_manager(raw):
    mgr = MagicMock()
    mgr.has_tool.return_value = True
    mgr.server_name_for_tool.return_value = "srv"
    mgr.server_has_oauth.return_value = True
    mgr.get_tools.return_value = []
    mgr.list_servers.return_value = []
    mgr.call_tool.side_effect = lambda *_a, **_k: dict(raw)
    return mgr


def _run_mcp_auth(raw):
    """Run the real loop (dispatch, final trace, accepted callback) over an MCP auth failure."""
    traces: list[ToolTrace] = []
    order: list[int] = []
    holder: dict = {}
    accepted = MagicMock(side_effect=lambda *a, **k: order.append(len(holder["llm"].calls)))
    llm = ScriptedLLM([_MCP_ACTION, _FINISH])
    holder["llm"] = llm
    mgr = _mcp_manager(raw)
    ctx = build_context(
        llm, RecordingExecutor(), on_accepted=accepted, on_tool_trace=traces.append,
        builtin_executor=None, mcp_manager=mgr,
    )
    with patch("react_loop._build_system_prompt", return_value=("sys", None)):
        react_loop.react_loop(ctx, "goal", progress_callback=lambda _m: None)
    mgr.call_tool.assert_called_once()  # the MCP path really ran
    return SimpleNamespace(traces=traces, order=order, accepted=accepted, mgr=mgr, llm=llm)


class TestMcpAuthTranslationKeepsEffectiveDisposition:
    """Auth rewrite must keep the EFFECTIVE (post-veto) disposition, not the raw marker."""

    @pytest.mark.parametrize(("veto", "expected"), _AUTH_VETOES)
    def test_translation_helper_keeps_vetoed_disposition(self, veto, expected):
        ctx = _ctx()
        ctx.mcp_manager = _mcp_manager({})
        translated = react_loop._handle_mcp_auth_failure(ctx, "t", _auth_raw(veto))
        assert "/mcp auth srv" in translated["error"]
        assert effective_execution(translated) == expected
        assert _outcome_guard_eligible(translated) is False

    @pytest.mark.parametrize(("veto", "expected"), _AUTH_VETOES)
    def test_actual_dispatch_keeps_vetoed_disposition(self, veto, expected):
        ctx = _ctx()
        ctx.builtin_executor = None
        ctx.mcp_manager = _mcp_manager(_auth_raw(veto))
        outcome = react_loop._dispatch_tool(ctx, json.loads(_MCP_ACTION), lambda _m: None)
        ctx.mcp_manager.call_tool.assert_called_once()
        ctx.mcp_manager.mark_needs_auth.assert_called_once_with("srv")
        assert "/mcp auth srv" in outcome["error"]
        assert effective_execution(outcome) == expected

    @pytest.mark.parametrize(("veto", "expected"), _AUTH_VETOES)
    def test_final_trace_and_accepted_callback_keep_veto(self, veto, expected):
        run = _run_mcp_auth(_auth_raw(veto))
        run.mgr.mark_needs_auth.assert_called_once_with("srv")
        assert [t.execution_disposition for t in run.traces] == [expected]
        assert MARKER not in repr(run.traces)
        for call in run.llm.calls:
            assert MARKER not in json.dumps(call.messages, default=str)
        # acceptance came only from the later valid finish, never from the vetoed tool step
        run.accepted.assert_called_once()
        assert run.order == [2]

    def test_genuine_executed_sdk_error_remains_guard_eligible(self):
        run = _run_mcp_auth(_auth_raw())
        run.mgr.mark_needs_auth.assert_called_once_with("srv")
        assert [t.execution_disposition for t in run.traces] == ["executed"]
        assert run.traces[0].success is False
        run.accepted.assert_called_once()
        assert run.order == [1]  # fired right after the tool step, before the finish turn

    def test_unstamped_auth_failure_is_not_guard_eligible(self):
        run = _run_mcp_auth(_auth_raw(prior=None))
        assert [t.execution_disposition for t in run.traces] == ["unknown"]
        run.accepted.assert_called_once()
        assert run.order == [2]

    def test_predispatch_refusal_stays_refused_through_translation(self):
        run = _run_mcp_auth(_auth_raw(prior="refused"))
        assert [t.execution_disposition for t in run.traces] == ["refused"]
        assert run.order == [2]


class TestPersistenceExceptionsAreNotUsableOutcomes:
    """An escaping memory persistence exception is never an accepted outcome."""

    _CASES = {
        "get": {"action": "get", "key": "k"},
        "set": {"action": "set", "key": "k", "value": "v"},
        "delete": {"action": "delete", "key": "k"},
    }

    @staticmethod
    def _boom():
        root = ValueError("root cause")
        exc = OSError("disk full")
        exc.__cause__ = root
        return exc, root

    @pytest.mark.parametrize("method", sorted(_CASES))
    def test_exception_escapes_actual_execute_unchanged(self, world, method):
        exc, root = self._boom()
        with patch.object(world.full._memory, method, side_effect=exc) as store_call:
            with pytest.raises(OSError) as info:
                world.full.execute("memory_write", dict(self._CASES[method]))
        store_call.assert_called_once()
        assert info.value is exc
        assert info.value.__cause__ is root

    @pytest.mark.parametrize("method", sorted(_CASES))
    def test_exception_escapes_dispatch_without_accepting_or_tracing(self, world, method):
        exc, root = self._boom()
        accepted = MagicMock()
        traces: list[ToolTrace] = []
        ctx = build_context(
            ScriptedLLM([_FINISH]), RecordingExecutor(),
            on_accepted=accepted, on_tool_trace=traces.append,
        )
        ctx.builtin_executor = world.full
        state = react_loop._LoopState(messages=[], goal_idx=0, max_steps=10)
        sink = MagicMock()
        action = {"action": "tool", "tool": "memory_write", "args": dict(self._CASES[method])}
        with patch.object(world.full._memory, method, side_effect=exc) as store_call:
            with pytest.raises(OSError) as info:
                react_loop._dispatch_action(
                    ctx, action, sink, state, "goal", time.time(), lambda _m: None,
                )
        store_call.assert_called_once()
        assert info.value is exc
        assert info.value.__cause__ is root
        assert traces == []
        sink.assert_not_called()
        assert state.guard_eligible is False and state.action_accepted is False
        react_loop._fire_accepted(ctx, state)
        accepted.assert_not_called()
        assert ctx.on_accepted is accepted  # guard completion still pending

    def test_returned_executed_failure_contrasts_as_eligible(self, world):
        failed = {"success": False, "output": "", "error": "exit 2", "exit_code": 2}
        accepted = MagicMock()
        traces: list[ToolTrace] = []
        ctx = build_context(
            ScriptedLLM([_FINISH]), RecordingExecutor(),
            on_accepted=accepted, on_tool_trace=traces.append,
        )
        ctx.builtin_executor = world.full
        state = react_loop._LoopState(messages=[], goal_idx=0, max_steps=10)
        action = {"action": "tool", "tool": "shell", "args": {"command": "false"}}
        with patch.object(world.full._shell, "_run_shell_subprocess", return_value=failed) as run:
            react_loop._dispatch_action(
                ctx, action, MagicMock(), state, "goal", time.time(), lambda _m: None,
            )
        run.assert_called_once()
        assert [t.execution_disposition for t in traces] == ["executed"]
        assert state.guard_eligible is True
        react_loop._fire_accepted(ctx, state)
        accepted.assert_called_once()


# ---------------------------------------------------------------------------
# R6 recovery: attempt-wide aggregation, cancellation, isolation, real dispatch
#
# NOTE: TestPlanDisposition.test_deferred_dispatch_then_execution_retains_veto scripts
# two separate FINAL traces (deferred, then an unrelated executed one). The production
# loop never emits a provisional confirmation trace: confirmation resolves
# synchronously inside dispatch and ONE final trace is emitted, so a real approval
# yields a single executed trace (pinned by the dispatch tests below).
# ---------------------------------------------------------------------------

_VETO_ORDER = ("cancelled", "deferred", "refused", "executed")
_SUCCESS_JSON = '{"success": true, "output": "x", "error": "", "exit_code": 0}'
_RECOVERABLE_JSON = json.dumps({
    "success": False, "output": "", "error": "slow",
    "error_type": "tool_timeout", "recoverable": True,
})
_HARD_FAIL_JSON = '{"success": false, "output": "", "error": "hard"}'


def _emit(on_trace, *kinds):
    """Emit traces; 'missing' has no disposition attribute, 'bogus' an invalid one."""
    for kind in kinds:
        if kind == "missing":
            on_trace(SimpleNamespace())
        elif kind == "bogus":
            on_trace(SimpleNamespace(execution_disposition="bogus"))
        else:
            on_trace(_trace(kind))


@contextmanager
def _no_bridge():
    """Remove the 100ms parent-cancel poller so only synchronous sampling can see it."""
    @contextmanager
    def noop(_parent, _cancel_event, _flag):
        yield

    with patch("execution_plan._parent_cancel_bridge", noop):
        yield


class TestStepDispositionAggregation:
    def test_every_subset_in_every_order_resolves_by_priority(self):
        values = ("cancelled", "deferred", "refused", "executed", "unknown")
        for size in range(1, len(values) + 1):
            for combo in itertools.combinations(values, size):
                expected = next((v for v in _VETO_ORDER if v in combo), "unknown")
                for ordering in itertools.permutations(combo):
                    assert _step_disposition(list(ordering)) == expected, ordering

    def test_empty_and_unknown_only_earn_no_credit(self):
        assert _step_disposition([]) == "unknown"
        assert _step_disposition(["unknown"]) == "unknown"
        assert _step_disposition(["bogus"]) == "unknown"
        assert _step_disposition(["bogus", "unknown", ""]) == "unknown"

    @pytest.mark.parametrize("junk", ["unknown", "bogus", ""])
    def test_unknown_or_invalid_cannot_erase_execution(self, junk):
        assert _step_disposition(["executed", junk]) == "executed"
        assert _step_disposition([junk, "executed"]) == "executed"
        assert _step_disposition(["executed", junk, "executed"]) == "executed"

    @pytest.mark.parametrize("veto", ["cancelled", "deferred", "refused"])
    def test_invalid_trace_cannot_erase_veto(self, veto):
        assert _step_disposition([veto, "bogus"]) == veto
        assert _step_disposition(["bogus", veto]) == veto


class TestPlanAttemptAggregation:
    @pytest.mark.parametrize("sequence, expected", [
        (("refused", "executed"), "refused"),
        (("executed", "refused"), "refused"),
        (("deferred", "executed"), "deferred"),
        (("executed", "deferred"), "deferred"),
        (("cancelled", "executed"), "cancelled"),
        (("executed", "cancelled"), "cancelled"),
        (("deferred", "refused"), "deferred"),
        (("executed", "missing"), "executed"),
        (("missing", "executed"), "executed"),
        (("executed", "bogus"), "executed"),
        (("bogus", "executed"), "executed"),
        (("missing",), "unknown"),
        (("bogus", "missing"), "unknown"),
    ])
    def test_actual_plan_aggregates_whole_attempt(self, sequence, expected):
        def behaviour(on_trace, task):
            _emit(on_trace, *sequence)
            return _SUCCESS_JSON

        runners: list[_Runner] = []
        result = _exec(_plan("s1"), {"s1": behaviour}, runners=runners)
        _assert_ran_once_and_deregistered(runners)
        assert read_execution(result) == expected
        assert MARKER not in json.dumps(result["results"])
        public_result, disposition = consume_execution(result)
        assert disposition == expected
        assert MARKER not in json.dumps(public_result)


class TestPlanCancellationAfterExecution:
    @pytest.mark.parametrize("bridge", ["stubbed", "real"])
    def test_parent_cancel_before_bridge_poll_overrides_execution(self, bridge):
        ctx = _ctx()
        ctx.cancel_event = threading.Event()

        def behaviour(on_trace, task):
            on_trace(_trace("executed"))
            ctx.cancel_event.set()  # parent cancels; the 100ms poller has not run
            return _SUCCESS_JSON

        runners: list[_Runner] = []
        executor = PlanExecutor(
            max_concurrent=1, sub_agent_factory=_factory({"s1": behaviour}, runners=runners),
        )
        with _no_bridge() if bridge == "stubbed" else nullcontext():
            result = executor.execute(_plan("s1"), ctx)
        _assert_ran_once_and_deregistered(runners)
        assert result["success"] is False
        assert read_execution(result) == "cancelled"
        assert MARKER not in json.dumps(result["results"])
        public_result, disposition = consume_execution(result)
        assert disposition == "cancelled"
        assert MARKER not in json.dumps(public_result)

    def test_plan_timeout_after_executed_step_is_cancelled(self):
        clock = [1000.0]

        def advance_past_deadline(on_trace, task):
            on_trace(_trace("executed"))
            clock[0] += 10_000.0
            return _SUCCESS_JSON

        runners: list[_Runner] = []
        plan = _plan("s1", "s2", deps={"s2": ["s1"]})
        executor = PlanExecutor(
            max_concurrent=1,
            sub_agent_factory=_factory({"s1": advance_past_deadline}, runners=runners),
        )
        with patch("execution_plan.time", SimpleNamespace(monotonic=lambda: clock[0])):
            result = executor.execute(plan, _ctx())
        _assert_ran_once_and_deregistered(runners)
        assert [r.label for r in runners] == ["plan-s1"]  # s2 never started
        assert result["success"] is False
        assert any("timeout" in e.lower() for e in result["errors"])
        assert read_execution(result) == "cancelled"

    def test_cancel_during_retry_backoff_discards_earlier_execution_credit(self):
        runners: list[_Runner] = []

        def executed_then_cancelled(on_trace, task):
            on_trace(_trace("executed"))
            runners[-1]._cancel_event.set()  # plan cancelled before backoff wait
            return _RECOVERABLE_JSON

        executor = PlanExecutor(
            max_concurrent=1,
            sub_agent_factory=_factory({"s1": executed_then_cancelled}, runners=runners),
        )
        result = executor.execute(_plan("s1"), _ctx())
        _assert_ran_once_and_deregistered(runners)
        assert len(runners) == 1  # no retry runner and no diagnostic after cancel
        assert result["success"] is False
        assert read_execution(result) == "cancelled"


class TestRetryEvidenceIsolation:
    @staticmethod
    def _run_attempts(scripts):
        runners: list[_Runner] = []
        observers: list = []
        counter = {"n": 0}

        def behaviour(on_trace, task):
            observers.append(on_trace)
            index = counter["n"]
            counter["n"] += 1
            return scripts[index](on_trace, observers)

        executor = PlanExecutor(
            max_concurrent=1, sub_agent_factory=_factory({"s1": behaviour}, runners=runners),
        )
        with patch.object(threading.Event, "wait", return_value=False):
            result = executor.execute(_plan("s1"), _ctx())
        assert len(runners) == len(scripts)
        _assert_ran_once_and_deregistered(runners)
        return result, observers

    def test_old_veto_does_not_leak_into_executed_retry(self):
        def first(on_trace, _observers):
            _emit(on_trace, "refused")
            return _RECOVERABLE_JSON

        def second(on_trace, _observers):
            _emit(on_trace, "executed")
            return _SUCCESS_JSON

        result, observers = self._run_attempts([first, second])
        assert observers[0] is not observers[1]
        assert read_execution(result) == "executed"

    def test_old_execution_does_not_leak_into_traceless_retry(self):
        def first(on_trace, _observers):
            _emit(on_trace, "executed")
            return _RECOVERABLE_JSON

        def second(_on_trace, _observers):
            return _SUCCESS_JSON

        result, _ = self._run_attempts([first, second])
        assert read_execution(result) == "unknown"

    def test_retained_old_observer_cannot_mutate_new_attempt(self):
        def first(on_trace, _observers):
            _emit(on_trace, "executed")
            return _RECOVERABLE_JSON

        def second(on_trace, observers):
            _emit(observers[0], "refused")  # stale observer fires during attempt 2
            _emit(on_trace, "executed")
            return _SUCCESS_JSON

        result, _ = self._run_attempts([first, second])
        assert read_execution(result) == "executed"

    def test_retained_old_observer_cannot_grant_credit_to_new_attempt(self):
        def first(on_trace, _observers):
            return _RECOVERABLE_JSON

        def second(_on_trace, observers):
            _emit(observers[0], "executed")  # stale observer fires during attempt 2
            return _SUCCESS_JSON

        result, _ = self._run_attempts([first, second])
        assert read_execution(result) == "unknown"


class TestActualDispatchConfirmationTrace:
    def test_ordinary_confirmation_emits_one_final_executed_trace(self, world, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "x.txt"
        action = json.dumps({"action": "tool", "tool": "file_write",
                             "args": {"path": str(target), "content": "x"}})
        traces: list[ToolTrace] = []
        asked: list[tuple] = []

        def approve(*args, **kwargs):
            asked.append(args)
            assert traces == []  # no provisional trace while the request is pending
            return True

        accepted, _ = _run(
            world, [action, _FINISH], confirm=approve, on_tool_trace=traces.append,
        )
        assert len(asked) == 1
        assert target.read_text() == "x"
        assert [t.execution_disposition for t in traces] == ["executed"]
        assert traces[0].success is True
        accepted.assert_called_once()

    def test_vision_confirmation_emits_one_final_executed_trace(self, tmp_path):
        image_path = "/unrecognised/pic.png"
        action = json.dumps({"action": "tool", "tool": "vision_query",
                             "args": {"path": image_path, "question": "What?"}})
        llm = ScriptedLLM([action, _FINISH])
        llm.chat = MagicMock(return_value="A cat.")
        traces: list[ToolTrace] = []
        accepted = MagicMock()
        ctx = build_context(llm, RecordingExecutor(), on_accepted=accepted,
                            on_tool_trace=traces.append)
        stub = ctx.builtin_executor

        class _Executor(_VisionFakeExecutor):
            def __getattr__(self, name):
                if name.startswith("_"):
                    raise AttributeError(name)
                return getattr(stub, name)

        class _Confirmation(_VisionFakeConfirmation):
            def __getattr__(self, name):
                if name.startswith("_"):
                    raise AttributeError(name)
                return MagicMock()

        confirmation = _Confirmation(approved=True, grant_dir="/unrecognised")
        original = confirmation.request_confirmation

        def recording_request(*args, **kwargs):
            assert traces == []  # no provisional trace while the request is pending
            return original(*args, **kwargs)

        confirmation.request_confirmation = recording_request
        ctx.builtin_executor = _Executor(_make_vision_policy(str(tmp_path)))
        ctx.confirmation = confirmation

        with patch("react_loop._encode_images", return_value=["<b64>"]), \
                patch("react_loop._build_system_prompt", return_value=("sys", None)):
            react_loop.react_loop(ctx, "goal", progress_callback=lambda _m: None)

        assert confirmation.last_call is not None
        assert confirmation.last_call[1] == "vision_query"
        llm.chat.assert_called_once()
        assert [t.execution_disposition for t in traces] == ["executed"]
        assert traces[0].success is True
        accepted.assert_called_once()
