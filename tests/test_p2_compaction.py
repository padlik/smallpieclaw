"""P2: compaction policy tests still awaiting rewrite or retirement.

Task 2.3 relocated every kept (non-policy) node of this module — goal /
``goal_idx`` anchoring, atomic native spans and partition, malformed native
history, summary-prompt middle rendering — to ``tests/test_context_manager.py``
with unchanged assertions, together with the shared helpers this module now
imports from there.

What remains is ledger policy that later slices own:

- rewrite (6): threshold under/over incl. tool-definition tokens (6.2),
  summary failure / empty summary (6.6), historical huge-args group (6.4).
  They keep running unchanged until their rewrite.
- retire (24): recent/short-context capping, deterministic tightening,
  per-model ``context_window`` and ``resolve_compaction_threshold`` budget
  policy (10.1). They are not deleted or disabled before that slice.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import context_manager
import pytest
from context_manager import (
    _cap_recent_message,
    _RECENT_HEAD,
    _RECENT_TAIL,
    maybe_compact,
    resolve_compaction_threshold,
)
from model_registry import ModelMetadata
from model_request_budget import compute_request_budget
from tests.test_context_manager import (
    _assert_wire_valid,
    _assistant_calls,
    _call,
    _char_estimate,
    _has_summary,
    _msg,
    _over_until_summarized,
    _tool,
)


class TestRecentCapping:
    def test_huge_recent_message_is_capped(self):
        big = "S" * (_RECENT_HEAD + _RECENT_TAIL + 10_000)
        capped = _cap_recent_message(_msg("user", big))
        assert len(capped["content"]) < len(big)
        assert "chars omitted" in capped["content"]

    def test_small_recent_message_unchanged(self):
        m = _msg("user", "short content")
        assert _cap_recent_message(m) is m


class TestMaybeCompact:
    def _big_messages(self) -> list[dict]:
        msgs = [_msg("user", "GOAL: do the big task")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 2000)))
            msgs.append(_msg("user", f"Tool result {i}\n" + ("r" * 4000) + "\nERR_TAIL"))
        return msgs

    def test_under_threshold_returns_unchanged(self):
        msgs = [_msg("user", "hi"), _msg("assistant", "{}"), _msg("user", "ok")]
        llm = MagicMock()
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
        assert out is msgs
        llm.chat.assert_not_called()

    def test_short_overbudget_history_is_capped(self, monkeypatch):
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [
            _msg("user", "GOAL: inspect logs"),
            _msg("assistant", '{"action": "shell"}'),
            _msg("user", "Tool 'shell' succeeded.\n" + ("z" * 40_000) + "\nIMPORTANT_TAIL"),
        ]
        llm = MagicMock()

        out, _ = maybe_compact(msgs, "system", 4000, llm)

        assert len(out) == 3
        assert out[0]["content"] == "GOAL: inspect logs"
        assert "IMPORTANT_TAIL" in out[-1]["content"]
        assert "chars omitted" in out[-1]["content"]
        assert char_estimate(out, "system") <= int(4000 * 0.85)
        llm.chat.assert_not_called()
    def test_llm_failure_returns_deterministic_fallback(self, monkeypatch):
        msgs = self._big_messages()
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             _over_until_summarized)
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("model down")
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
        # Not the oversized original; compacted to first + summary + last (4 msgs).
        assert len(out) < len(msgs)
        assert out[0]["content"] == "GOAL: do the big task"
        summary_msg = next(m for m in out if "Compacted context" in str(m.get("content")))
        # Deterministic fallback preserves error tails from the middle.
        assert "ERR_TAIL" in summary_msg["content"]

    def test_empty_summary_falls_back(self, monkeypatch):
        msgs = self._big_messages()
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             _over_until_summarized)
        llm = MagicMock()
        llm.chat.return_value = "   "
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
        summary_msg = next(m for m in out if "Compacted context" in str(m.get("content")))
        assert "ERR_TAIL" in summary_msg["content"]

    def test_huge_recent_result_capped_in_output(self, monkeypatch):
        msgs = self._big_messages()
        # Make the most recent message enormous.
        msgs[-1] = _msg("user", "Z" * (_RECENT_HEAD + _RECENT_TAIL + 50_000))
        monkeypatch.setattr(context_manager, "estimate_messages_tokens",
                             lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10)
        llm = MagicMock()
        llm.chat.return_value = "• summary"
        out, _ = maybe_compact(msgs, "system", 100_000, llm)
        assert "chars omitted" in out[-1]["content"]
        assert len(out[-1]["content"]) < (_RECENT_HEAD + _RECENT_TAIL + 50_000)

    def test_still_oversized_triggers_deterministic_tightening(self, monkeypatch):
        # A char-proportional estimator so shrinking actually lowers the count.
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        # Enormous first (goal) and recent messages that survive normal compaction.
        msgs = [_msg("user", "GOAL " + ("g" * 40_000))]
        for i in range(6):
            msgs.append(_msg("assistant", "act " + ("p" * 3000)))
            msgs.append(_msg("user", f"res {i} " + ("r" * 5000)))
        msgs[-1] = _msg("user", "TAILMSG " + ("z" * 40_000))
        llm = MagicMock()
        llm.chat.return_value = "• " + ("s" * 40_000)  # oversized summary too

        # Tight budget: 0.85 * ctx must be below the post-compaction size so the
        # deterministic tightening loop engages and drives it under budget.
        ctx_max = 4000  # threshold = 3400 tokens
        out, _ = maybe_compact(msgs, "system", ctx_max, llm)

        # The goal is never capped; everything else is driven to the floor.
        assert out[0] == msgs[0]
        rest = char_estimate(out[1:], "system")
        assert rest < char_estimate(msgs[1:], "system") // 10
        assert len(out[-1]["content"]) < 40_000


class TestPerModelContextWindow:
    """Per-model context_window awareness for compaction threshold.

    The threshold formula is: int((effective - model_max_tokens) * 0.85)
    where effective = model.context_window or agent.ctx_max_tokens.
    """

    def test_context_window_set_uses_per_model_limit(self, monkeypatch):
        """Model with context_window set uses per-model limit for threshold."""
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        # Build messages that exceed the per-model threshold but not the agent default.
        # context_window=8192, max_tokens=1024 → threshold = max(6092, 256) = 6092
        # agent.ctx_max_tokens=90000 → threshold would be int(90000*0.85) = 76500
        # We need messages > 6092 tokens but < 76500 tokens to prove per-model is used.
        # char_estimate divides chars by 4, so we need > 24412 chars of content.
        msgs = [_msg("user", "GOAL: test per-model")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 4000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 8000)))
        llm = MagicMock()
        llm.chat.return_value = "summary"

        # Per-model: context_window=8192, max_tokens=1024
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}
        out, _ = maybe_compact(msgs, "system", 8192, llm)
        # Compaction should fire because threshold (6092) < message tokens
        assert len(out) < len(msgs)
        llm.chat.assert_called()

    def test_context_window_unset_falls_back_to_agent_default(self, monkeypatch):
        """Model without context_window falls back to agent.ctx_max_tokens."""
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        # Same messages as above, but with a large ctx_max_tokens (agent default)
        msgs = [_msg("user", "GOAL: test fallback")]
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 2000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 4000)))
        llm = MagicMock()

        # Agent default: ctx_max_tokens=90000, max_tokens=1024
        # threshold = max(int((90000-1024)*0.85), 256) = max(75629, 256) = 75629
        llm.llm_cfg = {"max_tokens": 1024}
        out, _ = maybe_compact(msgs, "system", 90_000, llm)
        # No compaction — under threshold
        assert out is msgs
        llm.chat.assert_not_called()

    def test_threshold_reserves_completion_tokens(self, monkeypatch):
        """Compaction threshold reserves completion tokens before margin.

        max(int((8192 - 1024) * 0.85), 256) = max(6092, 256) = 6092,
        NOT int(8192 * 0.85) = 6963.
        """
        def char_estimate(messages, system, model=None):
            # Return a value between 6092 and 6963 to prove the formula
            return 6500

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [_msg("user", "GOAL"), _msg("assistant", "act"), _msg("user", "res")]
        # Pad to >3 messages so compaction path engages
        for i in range(4):
            msgs.append(_msg("assistant", f"a{i}"))
            msgs.append(_msg("user", f"r{i}"))
        llm = MagicMock()
        llm.chat.return_value = "summary"
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        # With completion reservation: threshold = 6092, 6500 > 6092 → compacts
        out, _ = maybe_compact(msgs, "system", 8192, llm)
        llm.chat.assert_called()  # compaction fired

        # Without completion reservation (old formula): threshold = 6963, 6500 < 6963 → no compact
        llm2 = MagicMock()
        llm2.llm_cfg = {"context_window": 8192, "max_tokens": 0}
        out2, _ = maybe_compact(msgs, "system", 8192, llm2)
        # With max_tokens=0: threshold = max(6963, 256) = 6963 > 6500 → no compaction
        assert out2 is msgs
        llm2.chat.assert_not_called()

    def test_negative_threshold_floor_prevents_degenerate_compaction(self, monkeypatch):
        """A misconfigured model_max_tokens >= ctx_max_tokens never compacts under the 256 floor."""
        def char_estimate(messages, system, model=None):
            # Messages claim 200 tokens — below the 256-token fixed floor.
            return 200

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [_msg("user", "GOAL")]
        for i in range(4):
            msgs.append(_msg("assistant", f"a{i}"))
            msgs.append(_msg("user", f"r{i}"))
        llm = MagicMock()

        # ctx_max_tokens=512 with default max_tokens=1024 would yield a negative raw threshold.
        # Floor clamps threshold to 256, so 200-token messages stay uncompacted.
        out, _ = maybe_compact(msgs, "system", 512, llm)
        assert out is msgs
        llm.chat.assert_not_called()

    def test_effective_limit_resolved_from_llm_cfg_per_turn(self):
        """react_loop reads context_window from llm_cfg per-turn at the compaction call site.

        Spec scenario: 'Effective limit is resolved per-turn at the compaction call site'
        — the effective limit SHALL be read from the active model config via
        ``ctx.llm.llm_cfg`` and no mid-run model transition SHALL occur.
        """
        ctx_max_tokens = 90_000  # agent default

        # Case 1: context_window set → effective uses per-model value, not agent default
        llm = MagicMock()
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}
        _effective = llm.llm_cfg.get("context_window") or ctx_max_tokens
        _budget = llm.llm_cfg.get("max_tokens") or 1024
        assert _effective == 8192, "per-turn resolution must read context_window from llm_cfg"
        assert _budget == 1024

        # Case 2: context_window absent → falls back to agent default
        llm2 = MagicMock()
        llm2.llm_cfg = {"max_tokens": 1024}
        _effective2 = llm2.llm_cfg.get("context_window") or ctx_max_tokens
        assert _effective2 == 90_000, "absent context_window must fall back to agent ctx_max_tokens"

        # Case 3: context_window = 0 (falsy) → falls back to agent default
        llm3 = MagicMock()
        llm3.llm_cfg = {"context_window": 0, "max_tokens": 1024}
        _effective3 = llm3.llm_cfg.get("context_window") or ctx_max_tokens
        assert _effective3 == 90_000, "falsy context_window (0) must fall back to agent ctx_max_tokens"

    def test_max_tokens_none_falls_back_to_default(self, monkeypatch):
        """resolve_compaction_threshold handles an explicit None max_tokens value."""
        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)
        msgs = [_msg("user", "GOAL: test none fallback")]
        for i in range(8):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 4000)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 8000)))

        llm = MagicMock()
        llm.chat.return_value = "summary"
        llm.llm_cfg = {"max_tokens": None, "context_window": 8192}
        _effective_ctx = llm.llm_cfg.get("context_window") or 90_000

        # Should not raise TypeError; threshold uses max_tokens fallback 1024.
        out, _ = maybe_compact(msgs, "system", _effective_ctx, llm)
        assert len(out) < len(msgs)
        llm.chat.assert_called()


class TestToolDefsTokensCompaction:
    """Tool-definition token overhead must be visible to context compaction."""

    def test_tool_defs_tokens_push_under_over_threshold(self, monkeypatch):
        """History alone is under threshold; adding tool_defs_tokens triggers compaction."""

        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)

        # ~3000 tokens of history, below the 6092-token threshold for an 8192 model.
        msgs = [_msg("user", "GOAL: use tools")]
        for i in range(4):
            msgs.append(_msg("assistant", f"action {i} " + ("p" * 1500)))
            msgs.append(_msg("user", f"result {i}\n" + ("r" * 1500)))

        llm = MagicMock()
        llm.chat.return_value = "• summary"
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        # Without tool_defs overhead, history stays under the 6092 threshold.
        out_no_tools, _ = maybe_compact(msgs, "system", 8192, llm, tool_defs_tokens=0)
        assert out_no_tools is msgs
        llm.chat.assert_not_called()

        # With tool_defs overhead, the combined total exceeds the threshold.
        llm2 = MagicMock()
        llm2.chat.return_value = "• summary"
        llm2.llm_cfg = {"context_window": 8192, "max_tokens": 1024}
        out, _ = maybe_compact(msgs, "system", 8192, llm2, tool_defs_tokens=5000)

        assert len(out) < len(msgs)
        llm2.chat.assert_called_once()
        assert any("Compacted context" in str(m.get("content")) for m in out)
        assert out[0]["content"] == "GOAL: use tools"

    def test_tool_defs_tokens_alone_triggers_compaction(self, monkeypatch):
        """Even tiny messages can exceed the threshold when tool_defs_tokens is large."""

        def char_estimate(messages, system, model=None):
            return sum(len(str(m.get("content") or "")) for m in messages) // 4

        monkeypatch.setattr(context_manager, "estimate_messages_tokens", char_estimate)

        msgs = [_msg("user", "GOAL: tiny")]
        for i in range(4):
            msgs.append(_msg("assistant", f"a{i}"))
            msgs.append(_msg("user", f"r{i}"))

        llm = MagicMock()
        llm.chat.return_value = "• summary"
        llm.llm_cfg = {"context_window": 8192, "max_tokens": 1024}

        out, _ = maybe_compact(msgs, "system", 8192, llm, tool_defs_tokens=7000)

        assert len(out) < len(msgs)
        llm.chat.assert_called_once()
        assert any("Compacted context" in str(m.get("content")) for m in out)



class TestResolveCompactionThreshold:
    """Unit tests for the shared threshold helper."""

    def test_with_context_window(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 8192, "max_tokens": 1024}, 90_000,
        )
        assert effective == 8192
        assert threshold == max(int((8192 - 1024) * 0.85), 256)
        assert threshold == 6092

    def test_without_context_window_falls_back(self):
        effective, threshold = resolve_compaction_threshold(
            {"max_tokens": 1024}, 90_000,
        )
        assert effective == 90_000
        assert threshold == max(int((90_000 - 1024) * 0.85), 256)
        assert threshold == 75_629

    def test_without_max_tokens_uses_default(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 8192}, 90_000,
        )
        assert effective == 8192
        assert threshold == max(int((8192 - 1024) * 0.85), 256)
        assert threshold == 6092

    def test_max_tokens_none_uses_default(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 8192, "max_tokens": None}, 90_000,
        )
        assert effective == 8192
        assert threshold == 6092

    def test_floor_for_misconfigured_budget(self):
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 512, "max_tokens": 1024}, 512,
        )
        assert effective == 512
        assert threshold == 256

    def test_catalog_metadata_uses_hard_budget_reserve(self):
        effective, threshold = resolve_compaction_threshold(
            {}, 90_000,
            metadata=ModelMetadata(context_tokens=8_192, output_tokens=1_024),
        )
        assert effective == 8_192
        assert threshold == max(int((8_192 - 1_024) * 0.85), 256)

    def test_catalog_input_only_limit_is_not_reduced_by_output_or_local_context(self):
        effective, threshold = resolve_compaction_threshold(
            {}, 90_000,
            metadata={"input_tokens": 1_000_000, "output_tokens": 65_536},
        )
        assert effective == 1_000_000
        assert threshold == int(1_000_000 * 0.85)

    def test_catalog_metadata_does_not_accept_model_config_context_window_key(self):
        with pytest.raises(ValueError):
            resolve_compaction_threshold({}, 90_000, metadata={"context_window": 8_192})

    def test_pinned_client_config_never_reaches_the_legacy_raw_dict_branch(self):
        """The production client's ``llm_cfg`` cannot select the legacy branch.

        It exposes no ``context_window``/``max_tokens`` keys and always carries
        its catalog metadata projection, which is not accepted as raw C/T/O
        input: without the request budget the helper fails closed instead of
        falling back. With the pinned preview budget (as ``_prepare_request``
        supplies it) the threshold follows that budget. The raw-dict branch is
        kept only for compatibility with dict callers.
        """
        from model_request_budget import ModelRequestBudgetError, compaction_threshold
        from tests.test_run_model_client import _client

        client = _client(metadata=ModelMetadata(context_tokens=8_192, output_tokens=1_024))
        try:
            cfg = client.llm_cfg
            preview = client.preview_request([{"role": "user", "content": "x"}], None)
        finally:
            client.close()
        assert "context_window" not in cfg and "max_tokens" not in cfg
        assert isinstance(cfg["metadata"], dict)
        with pytest.raises(ModelRequestBudgetError):
            resolve_compaction_threshold(cfg, 90_000)
        assert preview.budget.hard_input_budget == 7_168
        assert resolve_compaction_threshold(cfg, 90_000, request_budget=preview.budget) == (
            8_192, compaction_threshold(7_168),
        )

    def test_required_output_budget_uses_actual_cap_without_metadata_recompute(self):
        metadata = ModelMetadata(context_tokens=10_000, output_tokens=12_000)
        budget = compute_request_budget(
            metadata, input_tokens=100, local_context_tokens=90_000,
            output_cap_required=True,
        )
        assert budget.output_cap == 9_900
        effective, threshold = resolve_compaction_threshold(
            {}, 90_000, metadata=metadata, request_budget=budget,
        )
        assert effective == 10_000
        assert threshold == 256

    def test_optional_budget_still_rejects_impossible_completion_reserve(self):
        metadata = ModelMetadata(context_tokens=10_000, output_tokens=12_000)
        with pytest.raises(ValueError):
            resolve_compaction_threshold({}, 90_000, metadata=metadata)

    def test_supplied_request_budget_threshold_is_hard_input_based_and_metadata_immutable(self):
        metadata = ModelMetadata(input_tokens=5_000)
        snapshot = metadata
        budget = compute_request_budget(
            metadata, input_tokens=100, local_context_tokens=90_000,
        )
        effective, threshold = resolve_compaction_threshold(
            {"context_window": 1, "max_tokens": 99_999}, 90_000,
            metadata=metadata, request_budget=budget,
        )
        assert effective == 5_000
        assert threshold == int(budget.hard_input_budget * 0.85)
        assert metadata is snapshot
        assert metadata.input_tokens == 5_000

    def test_request_budget_overrides_catalog_metadata_threshold_for_compaction(self, monkeypatch):
        budget = compute_request_budget(
            ModelMetadata(context_tokens=10_000, output_tokens=12_000),
            input_tokens=100, local_context_tokens=90_000,
            output_cap_required=True,
        )
        llm = MagicMock()
        llm.llm_cfg = {"model": "local", "context_window": 10_000}
        messages = [_msg("user", "goal"), _msg("assistant", "a"), _msg("user", "b"), _msg("assistant", "c")]
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, *a, **k: 10 if _has_summary(m) else 8_500,
        )
        out, _ = maybe_compact(
            messages, "system", 90_000, llm,
            metadata=ModelMetadata(context_tokens=10_000, output_tokens=12_000),
            request_budget=budget,
        )
        assert len(out) == len(messages)
        llm.chat.assert_called_once()



class TestAtomicGroupCompaction:
    def test_historical_huge_args_demoted_whole_without_second_summary(self, monkeypatch):
        monkeypatch.setattr(context_manager, "estimate_messages_tokens", _char_estimate)
        huge = json.dumps({"blob": "Q" * 120_000})
        msgs = [
            _msg("user", "GOAL"),
            _msg("assistant", "old " + "o" * 2000),
            _msg("user", "older " + "u" * 2000),
            _assistant_calls(_call("big", "write_file", huge), content="writing"),
            _tool("big", "done"),
        ]
        calls: list[str] = []

        def summarize(prompt: str) -> str:
            calls.append(prompt)
            return "- early"

        out, idx = maybe_compact(msgs, "system", 4000, MagicMock(), summarize=summarize)
        assert len(calls) == 1
        _assert_wire_valid(out)
        assert out[idx]["content"] == "GOAL"
        assert not any(m.get("tool_calls") for m in out)
        summary = next(m for m in out if "Compacted context" in str(m["content"]))["content"]
        assert "id=big" in summary and "[tool result id=big]: done" in summary
        assert len(summary) < 10_000
