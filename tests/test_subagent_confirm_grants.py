"""Tests for the unified grant-ledger sub-agent confirmation flow."""

from __future__ import annotations

import html
import asyncio
import secrets
import threading
from unittest.mock import MagicMock, patch

import pytest

from confirmation import ConfirmationManager, GrantLifetime
from sub_agent_registry import SOURCE_ON_DEMAND, SubAgentRecord, SubAgentRegistry
from telegram_callbacks import cb_confirm, cb_subagent_confirm


class _MockQuery:
    """Minimal CallbackQuery stand-in."""

    def __init__(self, data: str, uid: int = 1):
        self.data = data
        self.from_user = type("_U", (), {"id": uid})()
        self.answer_called = False
        self.edited_text: str | None = None

    async def answer(self, text: str | None = None, *_args, **_kwargs) -> None:
        self.answer_called = True

    async def edit_message_text(self, text: str, **_) -> None:
        self.edited_text = text


class _FakeUpdate:
    def __init__(self, query: _MockQuery):
        self.callback_query = query
        self.effective_user = query.from_user


class _FakeAgent:
    def __init__(self):
        self._confirmation = ConfirmationManager()
        self.builtin_executor = None


class _FakeIface:
    def __init__(self, agent=None, builtin=None, uid: int = 1):
        self.agent = agent
        if self.agent is not None:
            self.agent.builtin_executor = builtin
        self.builtin_executor = builtin
        self.allowed_ids = {uid}

    def _is_authorized(self, uid: int) -> bool:
        return uid in self.allowed_ids


@pytest.fixture
def executor(make_builtin_executor, tmp_path):
    return make_builtin_executor(data_dir=str(tmp_path))


@pytest.fixture
def iface(executor):
    agent = _FakeAgent()
    return _FakeIface(agent=agent, builtin=executor)


class TestGrantButtonRenders:
    """Sub-agent prompts now render Approve / Till /reset / Deny (grant-capable) or Approve/Deny only (shell/secret_get)."""

    def _capture_keyboard(self, executor, coordinator, tool_name: str):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        captured: dict = {}

        def fake_prompt(token, tool_name, description, caller_tag=""):
            from confirmation import NO_STANDING_GRANT_TOOLS

            grant_capable = tool_name and tool_name not in NO_STANDING_GRANT_TOOLS
            rows = [[InlineKeyboardButton("✅ Approve", callback_data=f"subconfirm_yes:{token}")]]
            if grant_capable:
                rows[0].append(
                    InlineKeyboardButton("✅✅ Till /reset", callback_data=f"subconfirm_till_reset:{token}")
                )
            rows[0].append(InlineKeyboardButton("❌ Deny", callback_data=f"subconfirm_no:{token}"))
            captured["keyboard"] = InlineKeyboardMarkup(rows)

        executor._subagent_confirm_prompt_fn = fake_prompt
        with patch.object(coordinator, "default_headless_timeout", 0):
            executor._headless_confirm_bridge(
                tool_name, {"path": "/tmp/x"}, "do it", caller_tag="sa-1"
            )
        return captured["keyboard"]

    def test_file_tools_render_till_reset_button(self, executor):
        coordinator = ConfirmationManager()
        executor._coordinator = coordinator

        for tool in ("file_read", "file_write", "file_patch"):
            keyboard = self._capture_keyboard(executor, coordinator, tool)
            assert len(keyboard.inline_keyboard) == 1
            assert len(keyboard.inline_keyboard[0]) == 3
            texts = [btn.text for btn in keyboard.inline_keyboard[0]]
            datas = [btn.callback_data for btn in keyboard.inline_keyboard[0]]
            assert "✅ Approve" in texts
            assert "✅✅ Till /reset" in texts
            assert "❌ Deny" in texts
            assert any(d.startswith("subconfirm_till_reset:") for d in datas)

    def test_shell_does_not_render_till_reset_button(self, executor):
        prompt_fn = MagicMock()
        executor._subagent_confirm_prompt_fn = prompt_fn
        coordinator = ConfirmationManager()
        executor._coordinator = coordinator

        with patch.object(coordinator, "default_headless_timeout", 0):
            result = executor._headless_confirm_bridge(
                "shell", {"command": "rm -rf /"}, "danger", caller_tag="sa-1"
            )
        assert result.get("success") is False
        assert prompt_fn.called is True


class TestSubAgentGrantCallbacks:
    @pytest.mark.asyncio
    async def test_subconfirm_yes_creates_prompt_grant_with_scope(self, executor, iface, tmp_path):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor._subagent_confirm_prompt_fn = MagicMock()

        target = str(tmp_path / "x.txt")
        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        second_target = str(tmp_path / "second" / "y.txt")
        executor._pending_confirmations.stage(
            token, "file_diff", {"path_a": target, "path_b": second_target},
            zone_path=target, extra_zone_paths=(second_target,), scope_owner="sa-1",
        )
        executor._pending[token] = ("file_diff", {"path_a": target, "path_b": second_target})

        query = _MockQuery(f"subconfirm_yes:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert coordinator.grant_ledger.check("file_diff", target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_diff", second_target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_diff", target, scope_owner=None) is False
        assert "Approved for this run" in html.unescape(query.edited_text or "")

    @pytest.mark.asyncio
    async def test_subconfirm_till_reset_creates_session_grant(self, executor, iface, tmp_path):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor._subagent_confirm_prompt_fn = MagicMock()

        target = str(tmp_path / "x.txt")
        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        second_target = str(tmp_path / "second" / "y.txt")
        executor._pending[token] = ("file_diff", {"path_a": target, "path_b": second_target})
        executor._pending_confirmations.stage(
            token, "file_diff", {"path_a": target, "path_b": second_target},
            zone_path=target, extra_zone_paths=(second_target,), scope_owner="sa-1",
        )

        query = _MockQuery(f"subconfirm_till_reset:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert coordinator.grant_ledger.check("file_diff", target, scope_owner="sa-1") is True
        assert coordinator.grant_ledger.check("file_diff", second_target, scope_owner="sa-1") is True
        # Session grants are scope-free: "Till /reset" consent covers the whole
        # session (approval-grants spec) — main agent included.
        assert coordinator.grant_ledger.check("file_diff", target, scope_owner=None) is True
        assert "Approved until /reset" in html.unescape(query.edited_text or "")

    @pytest.mark.asyncio
    async def test_subconfirm_no_denies(self, executor, iface):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator

        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        executor._pending[token] = ("file_read", {})

        query = _MockQuery(f"subconfirm_no:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        assert event.is_set()
        assert coordinator._headless_confirm_results.get(token) is False
        assert "Denied" in html.unescape(query.edited_text or "")

    @pytest.mark.asyncio
    async def test_subconfirm_stale_token_does_not_grant(self, executor, iface, tmp_path):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        token = secrets.token_hex(12)
        query = _MockQuery(f"subconfirm_yes:{token}")
        await cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock())
        assert coordinator.grant_ledger.check("file_diff", str(tmp_path), scope_owner="sa-1") is False

    @pytest.mark.asyncio
    async def test_subagent_bridge_approves_single_zone_file_read(self, executor, iface, tmp_path):
        from tests.test_file_tools_zone import _make_policy

        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor.path_policy = _make_policy(str(tmp_path))
        secret = tmp_path / "outside" / "secret.txt"
        secret.parent.mkdir()
        secret.write_text("approved content")
        prompt_tokens: list[str] = []
        loop = asyncio.get_running_loop()

        def prompt(token, _tool, _description, _tag):
            prompt_tokens.append(token)
            query = _MockQuery(f"subconfirm_yes:{token}")
            asyncio.run_coroutine_threadsafe(
                cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock()), loop,
            )

        executor._subagent_confirm_prompt_fn = prompt
        result = await asyncio.to_thread(
            executor.execute, "file_read", {"path": str(secret)},
            caller_depth=1, caller_tag="sa-1",
        )
        assert result["success"] is True
        assert result["output"] == "approved content"
        assert len(prompt_tokens) == 1
        repeated = executor.execute("file_read", {"path": str(secret)}, caller_depth=1, caller_tag="sa-1")
        assert repeated["success"] is True
        assert len(prompt_tokens) == 1

    @pytest.mark.asyncio
    async def test_subagent_bridge_approves_two_zone_file_diff(self, executor, iface, tmp_path):
        from tests.test_file_tools_zone import _make_policy

        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        executor.path_policy = _make_policy(str(tmp_path))
        left, right = tmp_path / "left", tmp_path / "right"
        left.mkdir()
        right.mkdir()
        path_a, path_b = left / "a.txt", right / "b.txt"
        path_a.write_text("A")
        path_b.write_text("B")
        prompt_tokens: list[str] = []
        loop = asyncio.get_running_loop()

        def prompt(token, _tool, _description, _tag):
            prompt_tokens.append(token)
            query = _MockQuery(f"subconfirm_yes:{token}")
            asyncio.run_coroutine_threadsafe(
                cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock()), loop,
            )

        executor._subagent_confirm_prompt_fn = prompt
        result = await asyncio.to_thread(
            executor.execute, "file_diff", {"path_a": str(path_a), "path_b": str(path_b)},
            caller_depth=1, caller_tag="sa-1",
        )
        assert result["success"] is True
        assert len(prompt_tokens) == 1
        ledger = coordinator.grant_ledger
        assert ledger.check("file_diff", str(left), scope_owner="sa-1") is True
        assert ledger.check("file_diff", str(right), scope_owner="sa-1") is True
        assert ledger.check("file_diff", str(left), scope_owner=None) is False
        repeated = executor.execute(
            "file_diff", {"path_a": str(path_a), "path_b": str(path_b)},
            caller_depth=1, caller_tag="sa-1",
        )
        assert repeated["success"] is True
        assert len(prompt_tokens) == 1


    @pytest.mark.asyncio
    @pytest.mark.parametrize("action,lifetime", [
        ("confirm_yes", GrantLifetime.PROMPT),
        ("confirm_till_reset", GrantLifetime.SESSION),
        ("confirm_no", None),
    ])
    async def test_main_confirmation_grants_all_staged_paths(self, executor, iface, tmp_path, action, lifetime):
        iface.agent.resume = MagicMock()
        executor._coordinator = iface.agent._confirmation
        paths = (str(tmp_path / "a" / "x"), str(tmp_path / "b" / "y"))
        staged = executor._requires_confirmation(
            "file_diff", {"path_a": paths[0], "path_b": paths[1]}, "diff",
            zone_path=paths[0], extra_zone_paths=(paths[1],),
        )
        query = _MockQuery(f"{action}:{staged['token']}")
        await cb_confirm(iface, _FakeUpdate(query), MagicMock())
        for path in paths:
            grants = iface.agent._confirmation.grant_ledger
            scope_dir = str(__import__("os").path.dirname(path))
            assert grants.check("file_diff", scope_dir) is (lifetime is not None)
            if lifetime is not None:
                assert grants.check("file_diff", scope_dir, scope_owner="sa-other") is (lifetime is GrantLifetime.SESSION)
        assert iface.agent.resume.call_args.args == (staged["token"], action != "confirm_no")

    @pytest.mark.asyncio
    async def test_main_stale_confirmation_does_not_grant(self, executor, iface, tmp_path):
        iface.agent.resume = MagicMock()
        executor._coordinator = iface.agent._confirmation
        token = secrets.token_hex(12)
        query = _MockQuery(f"confirm_yes:{token}")
        await cb_confirm(iface, _FakeUpdate(query), MagicMock())
        assert iface.agent._confirmation.grant_ledger.check("file_diff", str(tmp_path / "x")) is False

    @pytest.mark.asyncio
    async def test_main_stage_callback_confirm_and_reexecute_without_prompt(self, executor, iface, tmp_path):
        from tests.test_file_tools_zone import _make_policy

        iface.agent.resume = MagicMock()
        executor._coordinator = iface.agent._confirmation
        executor.path_policy = _make_policy(str(tmp_path))
        left, right = tmp_path / "left", tmp_path / "right"
        left.mkdir()
        right.mkdir()
        path_a, path_b = left / "a.txt", right / "b.txt"
        path_a.write_text("A")
        path_b.write_text("B")
        staged = executor.execute("file_diff", {"path_a": str(path_a), "path_b": str(path_b)})
        assert staged["requires_confirmation"] is True
        query = _MockQuery(f"confirm_yes:{staged['token']}")
        await cb_confirm(iface, _FakeUpdate(query), MagicMock())
        result = executor.confirm(staged["token"])
        assert result["success"] is True
        repeated = executor.execute("file_diff", {"path_a": str(path_a), "path_b": str(path_b)})
        assert repeated["success"] is True


class TestTurnDrainGrantCleanup:
    """Turn confirmation state remains live until owned child cleanup completes."""

    def test_prompt_grant_cleanup_waits_for_child_lifecycle(
        self, make_agent_controller, make_builtin_executor, tmp_path
    ):
        from types import SimpleNamespace

        from sub_agent_registry import SubAgentRegistry

        allow_result = threading.Event()
        close_entered = threading.Event()
        release_close = threading.Event()

        class BlockingCloseRunner:
            agent_id = "sa-drain-grant"
            _model_id = "test-model"
            model_id = "test-model"
            _cancel_event = threading.Event()
            _llm = MagicMock()
            _agent = SimpleNamespace(max_iterations=8, _on_step=None)
            short_term = MagicMock()
            trace_id = None
            notify_fn = staticmethod(lambda _message: None)

            def run(self, _task):
                allow_result.wait(timeout=5.0)
                return "done"

            def close(self):
                close_entered.set()
                release_close.wait(timeout=5.0)

        runner = BlockingCloseRunner()
        executor = make_builtin_executor(
            sub_agent_factory=lambda **_kwargs: runner,
            data_dir=str(tmp_path),
        )
        controller = make_agent_controller(builtin_executor=executor)
        local_registry = SubAgentRegistry()
        run_result = []

        def fake_loop(ctx, *_args, **_kwargs):
            ctx.turn_id = "r-owner"
            spawned = executor._exec_spawn_agent(
                {"task": "child work", "_operator_turn_id": ctx.turn_id},
                trace_id="r-owner",
            )
            assert spawned["success"] is True
            controller._confirmation.add_grant(
                "file_write",
                str(tmp_path),
                GrantLifetime.PROMPT,
                scope_owner=runner.agent_id,
            )
            allow_result.set()
            record = local_registry.get(runner.agent_id)
            assert record is not None
            assert record._result_event.wait(timeout=5.0)
            return "done"

        confirmation_event = threading.Event()
        controller._confirmation._confirm_events["confirm-token"] = confirmation_event

        try:
            with patch("sub_agent_registry.get_registry", return_value=local_registry), \
                 patch("agent_controller.react_loop", side_effect=fake_loop), \
                 patch("agent_controller.AgentRuntime.build_react_context", return_value=MagicMock()), \
                patch("agent_controller.bind_run_context"):
                run_thread = threading.Thread(
                    target=lambda: run_result.append(
                        controller.run("operator task", trace_id="r-owner")
                    )
                )
                run_thread.start()
                assert close_entered.wait(timeout=5.0)

                assert run_thread.is_alive()
                assert executor._coordinator is controller._confirmation
                assert controller._confirmation.check_grant(
                    "file_write",
                    str(tmp_path / "out.txt"),
                    scope_owner=runner.agent_id,
                ) is True

                controller.resume("confirm-token", True)
                assert confirmation_event.wait(timeout=0.5)

                release_close.set()
                run_thread.join(timeout=5.0)
        finally:
            release_close.set()

        assert run_result == ["done"]
        assert executor._coordinator is None
        assert controller._confirmation.check_grant(
            "file_write",
            str(tmp_path / "out.txt"),
            scope_owner=runner.agent_id,
        ) is False


class TestGrantSinkOwningTurnBinding:
    """A child consults the grant ledger only while its owning turn is an active turn."""

    def _stage_child(
        self, executor, tmp_path, *, owner_turn, active_turns=(), drain_expired=False,
    ):
        from sub_agent_supervisor import ExecutionOwnership, _OwnedChildState
        from tests.test_file_tools_zone import _make_policy

        coordinator = ConfirmationManager()
        executor._coordinator = coordinator
        executor.path_policy = _make_policy(str(tmp_path))
        outside = tmp_path / "outside"
        outside.mkdir()
        coordinator.add_grant("file_write", str(outside), GrantLifetime.SESSION)
        record = SubAgentRecord(
            agent_id="sa-bound", label="on-demand", model="test-model",
            task_preview="write", started_at=0.0, source=SOURCE_ON_DEMAND,
        )
        record.ownership = ExecutionOwnership("on-demand", owner_turn)
        state = _OwnedChildState(record)
        state.drain_expired = drain_expired
        executor._supervisor._owned_children[record.agent_id] = state
        for turn_id in active_turns:
            executor._supervisor.register_active_turn(turn_id)
        return record, outside / "out.txt", coordinator

    def _write(self, executor, target):
        return executor.execute(
            "file_write", {"path": str(target), "content": "x"},
            caller_depth=1, caller_tag="sa-bound",
        )

    def _assert_denied_without_ledger(self, executor, target, coordinator):
        with patch.object(coordinator.grant_ledger, "check", wraps=coordinator.grant_ledger.check) as check:
            result = self._write(executor, target)
        check.assert_not_called()
        assert result["success"] is False
        assert target.exists() is False

    def test_live_child_of_prior_turn_does_not_consult_ledger(self, executor, tmp_path):
        _record, target, coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-old", active_turns=("r-new",),
        )
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_drain_expired_child_does_not_consult_ledger(self, executor, tmp_path):
        _record, target, coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-live", active_turns=("r-live",),
            drain_expired=True,
        )
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_cancelled_child_does_not_consult_ledger(self, executor, tmp_path):
        record, target, coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-live", active_turns=("r-live",),
        )
        record.cancel()
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_cancelled_child_with_no_active_turn_does_not_consult_ledger(self, executor, tmp_path):
        record, target, coordinator = self._stage_child(executor, tmp_path, owner_turn="r-gone")
        record.cancel()
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_drain_expired_child_with_no_active_turn_does_not_consult_ledger(self, executor, tmp_path):
        _record, target, coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-gone", drain_expired=True,
        )
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_stuck_child_is_denied_in_gap_after_its_turn_unregisters(self, executor, tmp_path):
        _record, target, coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-a", active_turns=("r-a",),
        )
        executor._supervisor.unregister_active_turn("r-a")
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_stuck_child_is_denied_when_next_turn_registers(self, executor, tmp_path):
        _record, target, coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-a", active_turns=("r-a",),
        )
        executor._supervisor.unregister_active_turn("r-a")
        executor._supervisor.register_active_turn("r-b")
        self._assert_denied_without_ledger(executor, target, coordinator)

    def test_live_child_of_active_turn_uses_matching_grant(self, executor, tmp_path):
        _record, target, _coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-live", active_turns=("r-live",),
        )
        result = self._write(executor, target)
        assert result["success"] is True
        assert target.read_text() == "x"

    def test_live_child_stays_valid_while_overlapping_turn_registers(self, executor, tmp_path):
        _record, target, _coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-a", active_turns=("r-a", "r-b"),
        )
        assert self._write(executor, target)["success"] is True

    def test_live_child_stays_valid_after_overlapping_turn_unregisters(self, executor, tmp_path):
        _record, target, _coordinator = self._stage_child(
            executor, tmp_path, owner_turn="r-a", active_turns=("r-a", "r-b"),
        )
        executor._supervisor.unregister_active_turn("r-b")
        assert self._write(executor, target)["success"] is True


class TestChildMayUseTurnGrants:
    """Pure supervisor-level contract of the grant-gate predicate."""

    def _supervisor_with_child(self, *, owner_turn, cancelled=False, drain_expired=False):
        from sub_agent_supervisor import ExecutionOwnership, SubAgentSupervisor, _OwnedChildState

        supervisor = SubAgentSupervisor(max_subagents=1)
        record = SubAgentRecord(
            agent_id="sa-x", label="on-demand", model="test-model",
            task_preview="t", started_at=0.0, source=SOURCE_ON_DEMAND,
        )
        record.ownership = ExecutionOwnership("on-demand", owner_turn)
        if cancelled:
            record.cancel()
        state = _OwnedChildState(record)
        state.drain_expired = drain_expired
        supervisor._owned_children["sa-x"] = state
        return supervisor

    def test_unknown_child_keeps_legacy_true_with_no_active_turn(self):
        from sub_agent_supervisor import SubAgentSupervisor

        assert SubAgentSupervisor(max_subagents=1).child_may_use_turn_grants("sa-unowned") is True

    def test_unknown_child_keeps_legacy_true_with_active_turn(self):
        from sub_agent_supervisor import SubAgentSupervisor

        supervisor = SubAgentSupervisor(max_subagents=1)
        supervisor.register_active_turn("r-a")
        assert supervisor.child_may_use_turn_grants("sa-unowned") is True

    def test_known_child_with_no_active_turn_is_denied(self):
        supervisor = self._supervisor_with_child(owner_turn="r-a")
        assert supervisor.child_may_use_turn_grants("sa-x") is False

    def test_unregister_removes_only_the_given_turn(self):
        supervisor = self._supervisor_with_child(owner_turn="r-a")
        supervisor.register_active_turn("r-a")
        supervisor.register_active_turn("r-b")
        supervisor.unregister_active_turn("r-b")
        assert supervisor.child_may_use_turn_grants("sa-x") is True

    def test_unregister_of_unknown_turn_is_noop(self):
        supervisor = self._supervisor_with_child(owner_turn="r-a")
        supervisor.register_active_turn("r-a")
        supervisor.unregister_active_turn("r-never")
        assert supervisor.child_may_use_turn_grants("sa-x") is True

    def test_cancelled_child_of_active_turn_is_denied(self):
        supervisor = self._supervisor_with_child(owner_turn="r-a", cancelled=True)
        supervisor.register_active_turn("r-a")
        assert supervisor.child_may_use_turn_grants("sa-x") is False


class TestReactLoopActiveTurnRegistration:
    """The depth-0 loop registers its turn on entry and always unregisters it."""

    def _ctx_with_child(self):
        from sub_agent_supervisor import ExecutionOwnership, SubAgentSupervisor, _OwnedChildState
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context

        ctx = build_context(
            ScriptedLLM(['{"action":"finish","result":"done"}']), RecordingExecutor(),
        )
        supervisor = SubAgentSupervisor(max_subagents=1)
        record = SubAgentRecord(
            agent_id="sa-loop", label="on-demand", model="test-model",
            task_preview="t", started_at=0.0, source=SOURCE_ON_DEMAND,
        )
        record.ownership = ExecutionOwnership("on-demand", ctx.turn_id)
        state = _OwnedChildState(record)
        state.cleanup_event.set()
        supervisor._owned_children[record.agent_id] = state
        ctx.builtin_executor._supervisor = supervisor
        return ctx, supervisor

    @staticmethod
    def _restage_child(supervisor, owner_turn):
        from sub_agent_supervisor import ExecutionOwnership, _OwnedChildState

        record = SubAgentRecord(
            agent_id="sa-loop", label="on-demand", model="test-model",
            task_preview="t", started_at=0.0, source=SOURCE_ON_DEMAND,
        )
        record.ownership = ExecutionOwnership("on-demand", owner_turn)
        supervisor._owned_children["sa-loop"] = _OwnedChildState(record)

    def test_turn_is_registered_while_loop_runs(self):
        from react_loop import _StepResult, react_loop

        ctx, supervisor = self._ctx_with_child()
        seen = []

        def step(*_args, **_kwargs):
            seen.append(supervisor.child_may_use_turn_grants("sa-loop"))
            return _StepResult(early_return="done")

        with patch("react_loop._build_system_prompt", return_value=("system", None)), \
             patch("react_loop._run_single_step", side_effect=step):
            react_loop(ctx, "task")
        assert seen == [True]

    def test_turn_is_unregistered_after_loop_returns(self):
        from react_loop import _StepResult, react_loop

        ctx, supervisor = self._ctx_with_child()
        with patch("react_loop._build_system_prompt", return_value=("system", None)), \
             patch("react_loop._run_single_step", return_value=_StepResult(early_return="done")):
            react_loop(ctx, "task")
        self._restage_child(supervisor, ctx.turn_id)
        assert supervisor.child_may_use_turn_grants("sa-loop") is False

    def test_turn_is_unregistered_when_drain_bookkeeping_raises(self):
        from react_loop import _StepResult, react_loop

        ctx, supervisor = self._ctx_with_child()
        with patch("react_loop._build_system_prompt", return_value=("system", None)), \
             patch("react_loop._run_single_step", return_value=_StepResult(early_return="done")), \
             patch.object(supervisor, "draining_children", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                react_loop(ctx, "task")
        self._restage_child(supervisor, ctx.turn_id)
        assert supervisor.child_may_use_turn_grants("sa-loop") is False


class TestCancellationApprovalRace:
    @pytest.mark.asyncio
    async def test_cancel_before_stale_approval_cannot_execute_file_write(
        self, executor, iface, tmp_path
    ):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        target = tmp_path / "cancelled-write.txt"
        prompted = threading.Event()
        token_holder = []
        result_holder = []
        local_registry = SubAgentRegistry()
        record = SubAgentRecord(
            agent_id="sa-cancel-race",
            label="on-demand",
            model="test-model",
            task_preview="write",
            started_at=0.0,
            source=SOURCE_ON_DEMAND,
        )
        local_registry.register(record)

        def prompt(token, _tool, _description, _caller_tag):
            token_holder.append(token)
            prompted.set()

        executor._subagent_confirm_prompt_fn = prompt

        with patch("sub_agent_registry.get_registry", return_value=local_registry):
            bridge_thread = threading.Thread(
                target=lambda: result_holder.append(
                    executor._headless_confirm_bridge(
                        "file_write",
                        {
                            "path": str(target),
                            "content": "must not exist",
                            "mode": "w",
                        },
                        "write cancelled file",
                        caller_tag=record.agent_id,
                        zone_path=str(target),
                    )
                )
            )
            bridge_thread.start()
            assert prompted.wait(timeout=1.0)

            record.cancel()
            bridge_thread.join(timeout=0.3)
            completed_after_cancel = not bridge_thread.is_alive()
            if bridge_thread.is_alive():
                coordinator.signal_headless_confirmation(token_holder[0], False)
                bridge_thread.join(timeout=2.0)

            query = _MockQuery(f"subconfirm_yes:{token_holder[0]}")
            await cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock())

        assert completed_after_cancel is True
        assert result_holder[0]["success"] is False
        assert executor._pending_confirmations.take(token_holder[0]) is None
        assert target.exists() is False
        assert coordinator.grant_ledger.check(
            "file_write", str(tmp_path), scope_owner=record.agent_id
        ) is False

    @pytest.mark.asyncio
    async def test_till_reset_pressed_after_child_cancel_records_no_session_grant(
        self, executor, iface, tmp_path
    ):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        target = tmp_path / "cancelled-session-write.txt"
        local_registry = SubAgentRegistry()
        record = SubAgentRecord(
            agent_id="sa-cancel-session",
            label="on-demand",
            model="test-model",
            task_preview="write",
            started_at=0.0,
            source=SOURCE_ON_DEMAND,
        )
        local_registry.register(record)
        loop = asyncio.get_running_loop()
        queries = []

        def prompt(token, _tool, _description, _caller_tag):
            # Cancel lands, then the operator presses "Till /reset" before the
            # waiting child observes the cancellation.
            record.cancel()
            query = _MockQuery(f"subconfirm_till_reset:{token}")
            queries.append(query)
            asyncio.run_coroutine_threadsafe(
                cb_subagent_confirm(iface, _FakeUpdate(query), MagicMock()), loop,
            ).result(timeout=2.0)

        executor._subagent_confirm_prompt_fn = prompt

        with patch("sub_agent_registry.get_registry", return_value=local_registry):
            result = await asyncio.to_thread(
                executor._headless_confirm_bridge,
                "file_write",
                {"path": str(target), "content": "must not exist", "mode": "w"},
                "write cancelled file",
                caller_tag=record.agent_id,
                zone_path=str(target),
            )

        assert result["success"] is False
        assert target.exists() is False
        assert coordinator.grant_ledger.check(
            "file_write", str(target), scope_owner=None
        ) is False
        assert "already resolved" in html.unescape(queries[0].edited_text or "")



class TestSinkVeto:
    """Crafted callbacks cannot create standing grants for shell/secret_get — the ledger sink refuses them."""

    @pytest.mark.asyncio
    async def test_crafted_callback_for_shell_denied_at_sink(self, iface, executor):
        coordinator = iface.agent._confirmation
        executor._coordinator = coordinator
        token = secrets.token_hex(12)
        event = threading.Event()
        coordinator._headless_confirm_events[token] = event
        executor._pending[token] = ("shell", {})

        query = _MockQuery(f"subconfirm_yes:{token}")
        update = _FakeUpdate(query)

        await cb_subagent_confirm(iface, update, MagicMock())

        # No shell grant exists; the callback still signalled the one-shot operation.
        assert coordinator.grant_ledger.check("shell", "/tmp/x", scope_owner=None) is False
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_direct_ledger_add_for_shell_refused(self, executor):
        """The sink veto also blocks a direct ledger.add('shell', ...) call."""
        from confirmation import GrantLedger

        ledger = GrantLedger()
        assert ledger.add("shell", "/data", GrantLifetime.SESSION) is False
        assert ledger.check("shell", "/data/x.txt") is False


# ---------------------------------------------------------------------------
# Scheduled-origin isolation (isolate-job-execution, design decision 2)
# ---------------------------------------------------------------------------


class _NoDiscovery:
    """Discovery tripwire: admission must never fetch a provider catalog."""

    def fetch(self, connection: object) -> object:
        raise AssertionError("discovery must not run during admission")


@pytest.fixture
def runtime_runner(tmp_path):
    """Strict admitted-pin factory ``(executor, profile, *, memory=None)``.

    Each call admits its own pin from a real catalog registry (active Main
    ``pa/model-a``) — ``admit_scheduled(None)`` for ``SCHEDULED_AGENT``,
    ``admit(None)`` otherwise — and builds the runner through the real
    ``AgentRuntime.create``. Every created runner is closed on teardown.
    """
    from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile
    from config_schema import (
        AgentConfig, EmbeddingsConfig, ProviderConfig, ResolvedProviderConnection,
    )
    from embedding_client import EmbeddingClient
    from model_registry import (
        CatalogDocument, ModelMetadata, ModelRecord, ProviderState,
    )
    from tests.model_stores import ModelStores

    model = ModelRecord(
        "pa/model-a", "pa", "model-a", "model-a display",
        ModelMetadata(
            5000, None, 2048, False, True, None,
        ),
    )
    provider = ProviderConfig("pa", "openai", "env:KEY_pa", "https://a.example.test/v1", 9)
    stores = ModelStores.at(tmp_path)
    stores.cache.write(CatalogDocument(
        1, 2, (ProviderState("pa", "openai", "2026-09-01T00:00:00Z", "fresh"),),
        (model,),
    ))

    def resolve(config: ProviderConfig) -> ResolvedProviderConnection:
        return ResolvedProviderConnection(
            provider_id=config.name, adapter=config.type, base_url=config.base_url,
            request_timeout=config.request_timeout, api_key="test-secret",
        )

    registry = stores.registry({"pa": provider}, _NoDiscovery(), resolve)
    registry.select(model.id)

    def unavailable_embedding(event: threading.Event) -> EmbeddingClient:
        return EmbeddingClient(EmbeddingsConfig(), cancel_event=event)

    runners: list = []

    def _make(executor, profile, *, memory=None):
        if profile is RuntimeProfile.SCHEDULED_AGENT:
            run_model = registry.admit_scheduled(None)
        else:
            run_model = registry.admit(None)
        runtime = AgentRuntime(
            agent_cfg=AgentConfig(), embedding_factory=unavailable_embedding,
            tool_index=MagicMock(), base_memory=memory if memory is not None else MagicMock(),
            builtin_executor=executor, skill_registry=MagicMock(), results=MagicMock(),
        )
        runner = runtime.create(profile, RuntimeOptions(run_model=run_model, label="nightly"))
        runners.append(runner)
        return runner

    try:
        yield _make
    finally:
        for runner in runners:
            runner.close()


def _runner_execute(runner, tool_name: str, args: dict) -> dict:
    """Dispatch a tool exactly as the runner's ReAct loop does."""
    tools = runner._agent.builtin_executor
    return tools.execute(
        tool_name, args, caller_depth=1, caller_tag=f"{runner.agent_id} r-deadbeef",
    )


@pytest.fixture
def operator_memory_executor(make_builtin_executor, tmp_path):
    """A shared executor wired to operator-owned KV and graph stores."""
    memory = MagicMock(name="operator_kv")
    memory.get.return_value = ["operator fact"]
    graph = MagicMock(name="operator_graph")
    graph.format_for_prompt.return_value = "operator graph context"
    writer = MagicMock(name="operator_graph_writer")
    ex = make_builtin_executor(memory=memory, data_dir=str(tmp_path))
    ex._graph_memory = graph
    ex._graph_memory_writer = writer
    return ex, memory, graph, writer


class TestScheduledMemoryToolsUnavailable:
    """Scheduled memory tools return scheduled_memory_unavailable without operator-store access."""

    @pytest.mark.parametrize("action", ["get", "set", "append", "delete"])
    def test_scheduled_memory_write_never_touches_operator_kv(self, operator_memory_executor, runtime_runner, action):
        from agent_runtime import RuntimeProfile

        ex, memory, _graph, _writer = operator_memory_executor
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT, memory=memory)

        result = _runner_execute(runner, "memory_write", {"action": action, "key": "k", "value": "v"})

        assert result["success"] is False
        assert result["error_type"] == "scheduled_memory_unavailable"
        assert memory.method_calls == []

    @pytest.mark.parametrize("tool_name,args", [
        ("memory_graph_search", {"query": "who"}),
        ("memory_graph_store", {"content": "a fact"}),
    ])
    def test_scheduled_graph_tool_never_touches_operator_graph(self, operator_memory_executor, runtime_runner, tool_name, args):
        from agent_runtime import RuntimeProfile

        ex, _memory, graph, writer = operator_memory_executor
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        result = _runner_execute(runner, tool_name, args)

        assert result["success"] is False
        assert result["error_type"] == "scheduled_memory_unavailable"
        assert graph.method_calls == [] and writer.method_calls == []


def _scheduled_policy(tmp_path):
    """Real PathPolicy: workspace is default-allowed, the vault is prohibited."""
    import logging
    import os

    from path_policy import PathPolicy

    root = str(tmp_path)
    dirs = {name: os.path.join(root, name) for name in (
        "workspace", "downloads", "skills", "results", "data", "state", "config",
    )}
    for directory in dirs.values():
        os.makedirs(directory, exist_ok=True)
    vault = os.path.join(root, "vault.toml")
    config = os.path.join(dirs["config"], "config.toml")
    for path, text in ((vault, "[secrets]\nk = \"v\"\n"), (config, "\n")):
        with open(path, "w") as fh:
            fh.write(text)
    return PathPolicy.create(
        agent_name="test-agent", workspace_dir=dirs["workspace"],
        downloads_dir=dirs["downloads"], tmp_dir="/tmp/test-agent",
        skills_dir=dirs["skills"], results_dir=dirs["results"],
        data_home=dirs["data"], state_home=dirs["state"], config_home=dirs["config"],
        vault_path=vault, config_path=config, logger=logging.getLogger("test"),
    )


@pytest.fixture
def active_operator(make_builtin_executor, tmp_path):
    """Shared executor during an active operator turn holding a session grant.

    The grant covers ``outside/`` (an unrecognised, otherwise confirmable
    directory); the Telegram prompt bridge is wired and recorded.
    """
    import os

    policy = _scheduled_policy(tmp_path)
    ex = make_builtin_executor(path_policy=policy, data_dir=str(tmp_path),
                               vault_path=str(tmp_path / "vault.toml"))
    coordinator = ConfirmationManager()
    coordinator.default_headless_timeout = 0  # an unanswered prompt never blocks the test
    ex._coordinator = coordinator
    ex._subagent_confirm_prompt_fn = MagicMock(name="telegram_prompt")
    outside = os.path.join(str(tmp_path), "outside")
    os.makedirs(outside, exist_ok=True)
    for name in ("a.txt", "b.txt"):
        with open(os.path.join(outside, name), "w") as fh:
            fh.write(f"{name} body\n")
    coordinator.grant_ledger.add("file_read", outside, GrantLifetime.SESSION)
    for tool in ("file_write", "file_patch", "file_send", "file_diff"):
        coordinator.grant_ledger.add(tool, outside, GrantLifetime.SESSION)
    return ex, coordinator, outside, policy


def _outside_call(tool_name: str, outside: str) -> dict:
    """Arguments for an otherwise-confirmable call under ``outside/``."""
    import os

    a, b = os.path.join(outside, "a.txt"), os.path.join(outside, "b.txt")
    return {
        "file_read": {"path": a},
        "file_write": {"path": a, "content": "scheduled overwrite"},
        "file_patch": {"path": a, "old_str": "body", "new_str": "patched"},
        "file_send": {"path": a},
        "file_diff": {"path_a": a, "path_b": b},
    }[tool_name]


_FILE_TOOLS = ["file_read", "file_write", "file_patch", "file_send", "file_diff"]


class TestScheduledApprovalDenied:
    """Scheduled confirmable operations fail noninteractively, even with an active operator grant."""

    @pytest.mark.parametrize("tool_name", _FILE_TOOLS)
    def test_scheduled_outside_boundary_file_op_is_denied(self, active_operator, runtime_runner, tool_name):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        result = _runner_execute(runner, tool_name, _outside_call(tool_name, outside))

        assert result["success"] is False
        assert result["error_type"] == "scheduled_approval_denied"

    @pytest.mark.parametrize("tool_name", _FILE_TOOLS)
    def test_scheduled_file_op_never_consults_grant_ledger(self, active_operator, runtime_runner, tool_name):
        from agent_runtime import RuntimeProfile

        ex, coordinator, outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)
        ledger = coordinator.grant_ledger

        with patch.object(ledger, "check", wraps=ledger.check) as check:
            _runner_execute(runner, tool_name, _outside_call(tool_name, outside))

        check.assert_not_called()

    @pytest.mark.parametrize("tool_name", _FILE_TOOLS)
    def test_scheduled_file_op_stages_no_prompt_or_token(self, active_operator, runtime_runner, tool_name):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        with patch.object(ex._pending_confirmations, "stage", wraps=ex._pending_confirmations.stage) as stage:
            _runner_execute(runner, tool_name, _outside_call(tool_name, outside))

        stage.assert_not_called()
        ex._subagent_confirm_prompt_fn.assert_not_called()

    def test_scheduled_outside_file_is_left_unmodified(self, active_operator, runtime_runner):
        import os

        from agent_runtime import RuntimeProfile

        ex, _coordinator, outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        _runner_execute(runner, "file_write", _outside_call("file_write", outside))

        with open(os.path.join(outside, "a.txt")) as fh:
            assert fh.read() == "a.txt body\n"

    def test_scheduled_secret_get_is_denied_without_prompt(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, _outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        with patch.object(ex._pending_confirmations, "stage", wraps=ex._pending_confirmations.stage) as stage:
            result = _runner_execute(runner, "secret_get", {"key": "k"})

        assert result["error_type"] == "scheduled_approval_denied"
        stage.assert_not_called()
        ex._subagent_confirm_prompt_fn.assert_not_called()

    def test_scheduled_graph_store_stages_no_prompt_or_token(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, _outside, _policy = active_operator
        ex._graph_memory = MagicMock(name="operator_graph")
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        with patch.object(ex._pending_confirmations, "stage", wraps=ex._pending_confirmations.stage) as stage:
            _runner_execute(runner, "memory_graph_store", {"content": "a fact"})

        stage.assert_not_called()
        ex._subagent_confirm_prompt_fn.assert_not_called()


class TestScheduledBoundariesUnchanged:
    """Default-allowed, prohibited-path, and depth-1 shell outcomes keep existing behavior."""

    def test_scheduled_default_allowed_write_succeeds_without_prompt(self, active_operator, runtime_runner):
        import os

        from agent_runtime import RuntimeProfile

        ex, _coordinator, _outside, policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)
        target = os.path.join(policy.workspace_dir, "job.txt")

        result = _runner_execute(runner, "file_write", {"path": target, "content": "ok"})

        assert result["success"] is True
        ex._subagent_confirm_prompt_fn.assert_not_called()

    def test_scheduled_default_allowed_diff_never_consults_grant_ledger(self, active_operator, runtime_runner):
        import os

        from agent_runtime import RuntimeProfile

        ex, coordinator, _outside, policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)
        a, b = (os.path.join(policy.workspace_dir, n) for n in ("a.txt", "b.txt"))
        for path in (a, b):
            with open(path, "w") as fh:
                fh.write(path)
        ledger = coordinator.grant_ledger

        with patch.object(ledger, "check", wraps=ledger.check) as check:
            result = _runner_execute(runner, "file_diff", {"path_a": a, "path_b": b})

        assert result["success"] is True
        check.assert_not_called()

    def _retargetable_diff(self, ex, policy, outside):
        """Workspace symlinks that are ALLOWED at the gate and retarget to ``outside`` afterwards."""
        import os

        safe = os.path.join(policy.workspace_dir, "safe.txt")
        with open(safe, "w") as fh:
            fh.write("safe\n")
        link_a = os.path.join(policy.workspace_dir, "link_a")
        link_b = os.path.join(policy.workspace_dir, "link_b")
        os.symlink(safe, link_a)
        os.symlink(safe, link_b)
        calls = {"n": 0}

        class RetargetingPolicy:
            """Delegates to the real policy; retargets the links after the initial gate."""

            def __getattr__(self, name):
                return getattr(policy, name)

            def classify(self, path, operation):
                calls["n"] += 1
                verdict = policy.classify(path, operation)
                if calls["n"] == 2:  # the initial gate has classified both paths
                    for link, name in ((link_a, "a.txt"), (link_b, "b.txt")):
                        os.remove(link)
                        os.symlink(os.path.join(outside, name), link)
                return verdict

        ex.path_policy = RetargetingPolicy()
        return link_a, link_b

    def test_scheduled_diff_retargeted_to_unrecognised_is_denied(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, outside, policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)
        link_a, link_b = self._retargetable_diff(ex, policy, outside)

        result = _runner_execute(runner, "file_diff", {"path_a": link_a, "path_b": link_b})

        assert result["success"] is False
        assert result["error_type"] == "scheduled_approval_denied"

    def test_scheduled_diff_retarget_never_consults_grant_ledger(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, coordinator, outside, policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)
        link_a, link_b = self._retargetable_diff(ex, policy, outside)
        ledger = coordinator.grant_ledger

        with patch.object(ledger, "check", wraps=ledger.check) as check:
            _runner_execute(runner, "file_diff", {"path_a": link_a, "path_b": link_b})

        check.assert_not_called()

    def test_scheduled_diff_retarget_does_not_open_outside_file(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, outside, policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)
        link_a, link_b = self._retargetable_diff(ex, policy, outside)
        opened: list[str] = []
        real_open = open

        def spy_open(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=spy_open):
            result = _runner_execute(runner, "file_diff", {"path_a": link_a, "path_b": link_b})

        assert result["success"] is False
        assert not [p for p in opened if p.startswith(outside)]

    def test_on_demand_diff_retarget_still_consults_grant_ledger(self, active_operator):
        ex, coordinator, outside, policy = active_operator
        link_a, link_b = self._retargetable_diff(ex, policy, outside)
        ledger = coordinator.grant_ledger

        with patch.object(ledger, "check", wraps=ledger.check) as check:
            result = ex.execute("file_diff", {"path_a": link_a, "path_b": link_b})

        check.assert_called()
        assert result["success"] is True

    def test_scheduled_prohibited_path_keeps_prohibited_denial(self, active_operator, runtime_runner, tmp_path):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, _outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        result = _runner_execute(runner, "file_read", {"path": str(tmp_path / "vault.toml")})

        assert result["error_type"] == "prohibited_path"

    def test_scheduled_dangerous_shell_keeps_depth1_block(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, _outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.SCHEDULED_AGENT)

        result = _runner_execute(runner, "shell", {"command": "rm -rf /tmp/test-agent-x"})

        assert result["success"] is False
        assert "Command blocked in headless mode" in result["error"]


class TestOnDemandApprovalUnchanged:
    """On-demand children keep using the active operator turn's grants and prompt bridge."""

    def test_on_demand_child_uses_operator_session_grant(self, active_operator, runtime_runner):
        from agent_runtime import RuntimeProfile

        ex, _coordinator, outside, _policy = active_operator
        runner = runtime_runner(ex, RuntimeProfile.ON_DEMAND_SUBAGENT)

        result = _runner_execute(runner, "file_read", _outside_call("file_read", outside))

        assert result["success"] is True
        assert "a.txt body" in result["output"]

    def test_on_demand_child_without_grant_prompts_operator(self, active_operator, runtime_runner, tmp_path):
        import os

        from agent_runtime import RuntimeProfile

        ex, _coordinator, _outside, _policy = active_operator
        ungranted = os.path.join(str(tmp_path), "ungranted")
        os.makedirs(ungranted, exist_ok=True)
        runner = runtime_runner(ex, RuntimeProfile.ON_DEMAND_SUBAGENT)

        _runner_execute(runner, "file_read", {"path": os.path.join(ungranted, "x.txt")})

        ex._subagent_confirm_prompt_fn.assert_called_once()
