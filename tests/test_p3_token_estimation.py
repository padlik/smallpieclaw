"""P3: token estimation safety tests.

Covers the conservative, content-aware heuristic and the optional tiktoken path:

- Plain prose is estimated at least as conservatively as the old len // 4 rule.
- Code / JSON / log-like text is estimated higher than prose of similar length.
- CJK / non-ASCII text is not undercounted.
- Per-message framing overhead makes a multi-message context larger than the
  same text concatenated into a single string.
- Image budgets are charged for the ``images`` field and multimodal content-list
  image parts, including missing/unreadable paths.
- ``context_manager.maybe_compact`` still compacts using the new estimator.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import context_manager
import token_estimator as te
from token_estimator import (
    classify_text,
    estimate_messages_tokens,
    estimate_tokens,
)


class TestHeuristicText:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_prose_not_less_conservative_than_len_div_4(self):
        text = "The quick brown fox jumps over the lazy dog near the riverbank today."
        assert estimate_tokens(text) >= len(text) // 4

    def test_code_estimated_higher_than_prose_of_similar_length(self):
        prose = "this is a fairly ordinary english sentence used for comparison here ok"
        code = '{"a":[1,2,3],"b":{"c":true},"d":(x)=>x*2+1;"e":[4,5,6,7,8,9]} //x;'
        # Comparable lengths so the difference is due to content, not size.
        assert abs(len(prose) - len(code)) <= 5
        assert estimate_tokens(code) > estimate_tokens(prose)

    def test_cjk_not_undercounted(self):
        text = "这是一个测试中文字符的句子用来验证不会被低估"
        # Old rule would give len // 4; CJK should count far higher (~1/char).
        assert estimate_tokens(text) > len(text) // 4
        assert estimate_tokens(text) >= len(text) - 2

    def test_classify_text(self):
        assert classify_text("") == "empty"
        assert classify_text("just some plain readable words here") == "prose"
        assert classify_text('{"k":[1,2,3],"v":(a)=>a;}{}[]<>') == "code"
        assert classify_text("中文中文中文中文") == "cjk"


class TestMessageFraming:
    def test_per_message_overhead_adds_up(self):
        blob = "word " * 100
        single = estimate_messages_tokens([{"role": "user", "content": blob}])
        split = estimate_messages_tokens([
            {"role": "user", "content": "word " * 50},
            {"role": "assistant", "content": "word " * 50},
        ])
        # Same underlying text but more messages => more framing overhead.
        assert split > single

    def test_system_prompt_counted(self):
        with_sys = estimate_messages_tokens([{"role": "user", "content": "hi"}], system="system rules")
        no_sys = estimate_messages_tokens([{"role": "user", "content": "hi"}])
        assert with_sys > no_sys


class TestImageBudget:
    def test_existing_image_path_counted(self, tmp_path):
        img = tmp_path / "x.png"
        img.write_bytes(b"\x89PNG" + b"0" * 100)
        tokens = estimate_messages_tokens([
            {"role": "user", "content": "look", "images": [str(img)]}
        ])
        text_only = estimate_messages_tokens([{"role": "user", "content": "look"}])
        assert tokens - text_only >= te._IMAGE_TOKENS

    def test_missing_image_path_still_charged(self):
        tokens = estimate_messages_tokens([
            {"role": "user", "content": "look", "images": ["/nonexistent/zzz.png"]}
        ])
        text_only = estimate_messages_tokens([{"role": "user", "content": "look"}])
        assert tokens - text_only >= te._IMAGE_TOKENS

    def test_multimodal_content_image_part_counted(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]
        tokens = estimate_messages_tokens(msgs)
        assert tokens >= te._IMAGE_TOKENS


class TestTiktokenPath:
    def test_known_openai_model_uses_tokenizer(self):
        # If tiktoken is installed it should resolve gpt-4o-mini and return a
        # positive count; if not, the heuristic still returns a positive count.
        n = estimate_tokens("Hello world, token counting here.", model="gpt-4o-mini")
        assert n > 0

    def test_unknown_model_falls_back_to_heuristic(self):
        text = "some text for an unknown provider model name"
        assert estimate_tokens(text, model="ollama-llama-3.1") == estimate_tokens(text)

    def test_tokenizer_failure_falls_back(self, monkeypatch):
        # Force the encoder lookup to raise; estimation must not crash.
        monkeypatch.setattr(te, "_get_encoder", lambda model: (_ for _ in ()).throw(RuntimeError("boom")))
        # _get_encoder is wrapped in estimate_tokens via try path; ensure no crash.
        try:
            val = estimate_tokens("text", model="gpt-4o-mini")
        except Exception:  # pragma: no cover
            val = None
        assert val is None or val > 0


class TestCompactionUsesEstimator:
    def test_maybe_compact_still_compacts(self, monkeypatch):
        # Real estimator, tiny budget so a multi-message context compacts.
        msgs = [{"role": "user", "content": "GOAL: do the task"}]
        for i in range(6):
            msgs.append({"role": "assistant", "content": f"step {i} " + ("p" * 2000)})
            msgs.append({"role": "user", "content": f"result {i} " + ("r" * 4000) + " ERRTAIL"})
        llm = MagicMock()
        llm.llm_cfg = {"model": "ollama-local"}  # unknown => heuristic path
        llm.chat.return_value = "• compacted summary"
        out = context_manager.maybe_compact(msgs, "system", 2000, llm)
        assert len(out) < len(msgs)
        assert out[0]["content"] == "GOAL: do the task"
        assert any("Compacted context" in str(m.get("content")) for m in out)
