"""P3: request-scoped trace ID tests.

Covers per-run trace IDs that correlate one run across ReAct, LLM, and tool logs:

- trace_context helpers generate distinct IDs and propagate a parent trace.
- ReactContext.log_prefix / caller_tag include the label and trace ID.
- LLMClient.set_trace_id weaves the trace into the caller tag and restores it.
- AgentController.run() mints a distinct trace per run, applies it to the LLM,
  passes it into ReactContext, and restores the LLM trace afterward.
- react_loop start logs carry the trace ID.
- SubAgentRunner forwards a parent trace into its inner AgentController.
"""

from __future__ import annotations

import logging
import threading
from contextvars import Context
from unittest.mock import MagicMock, patch

import agent_controller
from react_loop import ReactContext, react_loop
from trace_context import child_trace_id, new_trace_id


class TestTraceContextHelpers:
    def test_new_trace_id_format(self):
        tid = new_trace_id()
        assert tid.startswith("r-")
        assert len(tid) == len("r-") + 8

    def test_new_trace_ids_are_distinct(self):
        assert new_trace_id() != new_trace_id()

    def test_child_reuses_parent_when_present(self):
        assert child_trace_id("r-deadbeef") == "r-deadbeef"

    def test_child_generates_fresh_when_absent(self):
        tid = child_trace_id(None)
        assert tid.startswith("r-")
        assert child_trace_id("") != ""


class TestReactContextPrefix:
    def _ctx(self, label="main", trace_id=""):
        return ReactContext(
            llm=MagicMock(), tool_index=MagicMock(), executor=MagicMock(),
            creator=MagicMock(), memory=MagicMock(), builtin_executor=None,
            mcp_manager=None, skill_registry=None, label=label, trace_id=trace_id,
        )

    def test_log_prefix_with_trace(self):
        assert self._ctx("main", "r-abc12345").log_prefix == "[main r-abc12345] "

    def test_log_prefix_without_trace(self):
        assert self._ctx("main", "").log_prefix == "[main] "

    def test_caller_tag_with_trace(self):
        assert self._ctx("sa-1a2b", "r-abc12345").caller_tag == "sa-1a2b r-abc12345"

    def test_caller_tag_without_trace(self):
        assert self._ctx("sa-1a2b", "").caller_tag == "sa-1a2b"


class TestLLMSetTraceId:
    def _client(self):
        from llm_client import LLMClient
        cfg = {
            "models": [{"name": "m", "provider": "openai", "model": "gpt-4o-mini",
                        "api_key": "sk", "base_url": "https://api.openai.com/v1"}],
            "agent": {},
        }
        return LLMClient(cfg, caller_tag="main")

    def test_set_trace_id_weaves_into_caller_tag(self):
        c = self._client()
        c.set_trace_id("r-abc12345")
        assert c._caller_tag == "main r-abc12345"

    def test_clear_trace_id_restores_base(self):
        c = self._client()
        c.set_trace_id("r-abc12345")
        c.set_trace_id("")
        assert c._caller_tag == "main"

    def test_trace_tag_is_context_local(self):
        c = self._client()

        def set_and_read(trace_id):
            c.set_trace_id(trace_id)
            return c._caller_tag

        ctx_one = Context()
        ctx_two = Context()

        assert ctx_one.run(set_and_read, "r-one1111") == "main r-one1111"
        assert ctx_two.run(set_and_read, "r-two2222") == "main r-two2222"
        assert ctx_one.run(lambda: c._caller_tag) == "main r-one1111"
        assert ctx_two.run(lambda: c._caller_tag) == "main r-two2222"
        assert c._caller_tag == "main"


class TestAgentControllerTrace:
    def _controller(self, trace_id=None):
        llm = MagicMock()
        llm._active_idx = 0
        llm._trace_id = ""
        applied = []
        llm.set_trace_id.side_effect = lambda t: applied.append(t)
        ctrl = agent_controller.AgentController(
            llm=llm, tool_index=MagicMock(), executor=MagicMock(),
            creator=MagicMock(), memory=MagicMock(), trace_id=trace_id,
        )
        return ctrl, llm, applied

    def test_run_generates_distinct_traces(self):
        ctrl, _llm, _ = self._controller()
        seen = []
        with patch.object(agent_controller, "react_loop",
                          side_effect=lambda ctx, *a, **k: seen.append(ctx.trace_id) or "ok"):
            ctrl.run("goal one")
            ctrl.run("goal two")
        assert len(seen) == 2
        assert seen[0] != seen[1]
        assert all(t.startswith("r-") for t in seen)

    def test_run_applies_and_restores_llm_trace(self):
        ctrl, llm, applied = self._controller()
        with patch.object(agent_controller, "react_loop", side_effect=lambda ctx, *a, **k: "ok"):
            ctrl.run("goal")
        # set_trace_id called once with the run trace and once to restore "".
        assert len(applied) == 2
        assert applied[0].startswith("r-")
        assert applied[1] == ""

    def test_run_reuses_propagated_parent_trace(self):
        ctrl, _llm, _ = self._controller(trace_id="r-parent01")
        seen = []
        with patch.object(agent_controller, "react_loop",
                          side_effect=lambda ctx, *a, **k: seen.append(ctx.trace_id) or "ok"):
            ctrl.run("goal")
        assert seen == ["r-parent01"]


class TestReactLoopTraceLogging:
    def test_start_log_contains_trace_id(self, monkeypatch):
        llm = MagicMock()
        llm.chat_with_fallback.return_value = '{"action": "finish", "result": "done"}'
        llm.chat_with_tools_fallback.side_effect = NotImplementedError("native tools not mocked")
        llm.llm_cfg = {"model": "test-model"}
        ctx = ReactContext(
            llm=llm, tool_index=MagicMock(), executor=MagicMock(), creator=MagicMock(),
            memory=MagicMock(), builtin_executor=None, mcp_manager=None, skill_registry=None,
            cancel_event=threading.Event(), label="main", trace_id="r-trace777",
        )
        # Run identity is bound into structlog contextvars at run entry now, not
        # embedded in the raw log message text.
        import agent_logging
        seen: dict = {}
        monkeypatch.setattr(agent_logging, "bind_run_context", lambda **kw: seen.update(kw))
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            react_loop(ctx, "task")
        assert seen.get("trace") == "r-trace777"
        assert seen.get("agent") == "main"


class TestFallbackLogTraceCorrelation:
    """chat_with_fallback orchestration logs carry the request trace tag."""

    def _client(self):
        from llm_client import LLMClient
        cfg = {
            "models": [
                {"name": "m1", "provider": "openai", "model": "gpt-4o-mini",
                 "api_key": "sk", "base_url": "https://api.openai.com/v1"},
                {"name": "m2", "provider": "openai", "model": "gpt-4o",
                 "api_key": "sk", "base_url": "https://api.openai.com/v1"},
            ],
            "agent": {},
        }
        return LLMClient(cfg, caller_tag="main")

    def test_fallback_logs_include_trace_tag(self, caplog):
        from llm_client import LLMError

        c = self._client()
        c.set_trace_id("r-fb123456")
        # Primary fails transiently, fallback also fails -> all three orchestration
        # log lines fire (falling-back, will-try-next, all-failed).
        c.chat = MagicMock(side_effect=LLMError("boom"))
        with caplog.at_level(logging.WARNING, logger="llm_client"):
            try:
                c.chat_with_fallback([{"role": "user", "content": "hi"}])
            except LLMError:
                pass
        messages = [rec.getMessage() for rec in caplog.records]
        fallback_logs = [
            m for m in messages
            if "Falling back to model" in m or "will try next fallback" in m
            or "candidate model(s) failed" in m
        ]
        assert fallback_logs, "expected fallback orchestration logs"
        assert all("r-fb123456" in m for m in fallback_logs)


class TestSubAgentTracePropagation:
    def test_subagent_forwards_trace_to_controller(self):
        captured = {}
        real_init = agent_controller.AgentController.__init__

        def spy_init(self, *args, **kwargs):
            captured["trace_id"] = kwargs.get("trace_id")
            return real_init(self, *args, **kwargs)

        cfg = {
            "models": [{"name": "m", "provider": "openai", "model": "gpt-4o-mini",
                        "api_key": "sk", "base_url": "https://api.openai.com/v1"}],
            "agent": {},
        }
        with patch.object(agent_controller.AgentController, "__init__", spy_init):
            runner = agent_controller.SubAgentRunner(
                model_cfg=cfg["models"][0], config=cfg,
                tool_index=MagicMock(), executor=MagicMock(), creator=MagicMock(),
                base_memory=MagicMock(), builtin_executor=MagicMock(),
                trace_id="r-parent99",
            )
        assert captured["trace_id"] == "r-parent99"
        assert runner._agent._trace_id == "r-parent99"
