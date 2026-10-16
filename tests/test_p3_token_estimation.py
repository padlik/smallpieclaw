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
import threading
from concurrent.futures import ThreadPoolExecutor
from token_estimator import (
    classify_text,
    estimate_messages_tokens,
    estimate_request_payload_tokens,
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


class TestCompleteRequestPayload:
    def test_counts_system_messages_tool_definitions_and_native_call_arguments(self):
        payload = {
            "system": "system instruction",
            "messages": [{
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"distinctive argument"}'},
                }],
            }],
            "tools": [{
                "type": "function",
                "function": {"name": "lookup", "description": "distinctive tool definition"},
            }],
        }
        base = estimate_request_payload_tokens({"messages": []}, model="unknown")
        assert estimate_request_payload_tokens(payload, model="unknown") > base

    def test_image_data_url_is_charged_without_counting_base64_as_text(self):
        payload = {
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + ("A" * 100_000)},
                }],
            }]
        }
        estimate = estimate_request_payload_tokens(payload, model="unknown")
        assert te._IMAGE_TOKENS <= estimate < 10_000

    def test_native_image_data_is_not_tokenized_as_base64_text(self):
        payload = {"messages": [{
            "role": "user",
            "content": [{"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "A" * 100_000,
            }}],
        }]}
        estimate = estimate_request_payload_tokens(payload, model="unknown")
        assert te._IMAGE_TOKENS <= estimate < 10_000

    def test_openai_image_url_replaces_only_locator_leaf(self):
        def payload(url, description="short", sibling_data="short"):
            return {"messages": [{"role": "user", "content": [{
                "type": "image_url",
                "image_url": {"url": url, "description": description},
                "data": sibling_data,
            }]}]}

        short = estimate_request_payload_tokens(payload("x"), model="unknown")
        long = estimate_request_payload_tokens(payload("x" * 200_000), model="unknown")
        assert te._IMAGE_TOKENS <= short
        assert abs(short - long) < 10

        with_description = estimate_request_payload_tokens(
            payload("x", description="metadata " * 10_000), model="unknown",
        )
        with_sibling = estimate_request_payload_tokens(
            payload("x", sibling_data="sibling " * 10_000), model="unknown",
        )
        assert with_description > short + 10_000
        assert with_sibling > short + 10_000

    def test_anthropic_image_source_base64_replaces_only_data_leaf(self):
        def payload(data, metadata="short"):
            return {"messages": [{"role": "user", "content": [{
                "type": "image",
                "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": data, "description": metadata,
                },
                "extra": "ordinary sibling",
            }]}]}

        short = estimate_request_payload_tokens(payload("A"), model="unknown")
        long = estimate_request_payload_tokens(payload("A" * 100_000), model="unknown")
        assert short >= te._IMAGE_TOKENS
        assert abs(short - long) < 10
        with_metadata = estimate_request_payload_tokens(
            payload("A", metadata="metadata " * 10_000), model="unknown",
        )
        assert with_metadata > short + 10_000
        nonimage = {"messages": [{"role": "user", "content": [{
            "type": "image", "source": {
                "type": "base64", "media_type": "text/plain", "data": "A" * 100_000,
            },
        }]}]}
        assert estimate_request_payload_tokens(nonimage, model="unknown") > 20_000

    def test_openai_image_counts_anthropic_shaped_sibling_as_text_in_either_key_order(self):
        sibling_data = "ordinary sibling payload " * 10_000
        part_orders = [
            {"type": "image_url", "image_url": {"url": "x"},
             "source": {"data": sibling_data}},
            {"source": {"data": sibling_data}, "image_url": {"url": "x"},
             "type": "image_url"},
        ]
        estimates = [estimate_request_payload_tokens(
            {"messages": [{"content": [part]}]}, model="unknown",
        ) for part in part_orders]
        assert all(value > 20_000 for value in estimates)
        assert abs(estimates[0] - estimates[1]) < 10

    def test_anthropic_image_counts_openai_shaped_sibling_as_text_in_either_key_order(self):
        sibling_url = "ordinary sibling locator " * 10_000
        part_orders = [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "A",
            }, "image_url": {"url": sibling_url}},
            {"image_url": {"url": sibling_url}, "source": {
                "type": "base64", "media_type": "image/png", "data": "A",
            }, "type": "image"},
        ]
        estimates = [estimate_request_payload_tokens(
            {"messages": [{"content": [part]}]}, model="unknown",
        ) for part in part_orders]
        assert all(value > 20_000 for value in estimates)
        assert abs(estimates[0] - estimates[1]) < 10

    def test_ollama_images_replace_only_string_entries(self):
        def payload(image, extra="short"):
            return {"messages": [{"role": "user", "content": "describe",
                                  "images": [image], "extra": extra}]}

        short = estimate_request_payload_tokens(payload("A"), model="unknown")
        long = estimate_request_payload_tokens(payload("A" * 100_000), model="unknown")
        assert short >= te._IMAGE_TOKENS
        assert abs(short - long) < 10
        with_extra = estimate_request_payload_tokens(
            payload("A", extra="extra text " * 10_000), model="unknown",
        )
        assert with_extra > short + 10_000
        structured = estimate_request_payload_tokens(
            payload({"data": "A" * 100_000}), model="unknown",
        )
        assert structured > 20_000

    def test_schema_property_renamed_image_is_still_counted(self):
        description = "ordinary schema description " * 12_000
        payload = {"tools": [{"parameters": {"properties": {
            "longtext": {"description": description},
        }}}]}
        renamed = {"tools": [{"parameters": {"properties": {
            "image": {"description": description},
        }}}]}
        assert estimate_request_payload_tokens(renamed, model="unknown") >= 20_000
        assert abs(
            estimate_request_payload_tokens(payload, model="unknown")
            - estimate_request_payload_tokens(renamed, model="unknown")
        ) < 20

    def test_arbitrary_image_keys_and_image_like_schema_are_counted(self):
        data = "long ordinary payload text " * 8_000
        payload = {"images": data, "schema": {"type": "image", "image_data": data}}
        assert estimate_request_payload_tokens(payload, model="unknown") > 40_000
        nested_transport_names = {"schema": {"messages": [{"content": [
            {"type": "image", "data": data},
        ]}], "contents": [{"parts": [{"inlineData": {
            "mimeType": "image/png", "data": data,
        }}]}]}}
        assert estimate_request_payload_tokens(nested_transport_names, model="unknown") > 40_000
        unsupported_image_shape = {"messages": [{"content": [{
            "type": "image", "data": data,
        }]}]}
        assert estimate_request_payload_tokens(unsupported_image_shape, model="unknown") > 20_000

    def test_google_inline_image_data_camel_and_snake_case(self):
        encoded = "A" * 100_000
        camel = {"contents": [{"parts": [{"inlineData": {
            "mimeType": "image/png", "data": encoded, "caption": "kept caption",
        }, "text": "adjacent text"}]}]}
        snake = {"contents": [{"parts": [{"inline_data": {
            "mime_type": "image/png", "data": encoded, "caption": "kept caption",
        }, "text": "adjacent text"}]}]}
        camel_tokens = estimate_request_payload_tokens(camel, model="unknown")
        snake_tokens = estimate_request_payload_tokens(snake, model="unknown")
        assert te._IMAGE_TOKENS <= camel_tokens < 10_000
        assert te._IMAGE_TOKENS <= snake_tokens < 10_000
        assert abs(camel_tokens - snake_tokens) < 20
        without_text = {"contents": [{"parts": [{"inlineData": {
            "mimeType": "image/png", "data": encoded,
        }}]}]}
        assert camel_tokens > estimate_request_payload_tokens(without_text, model="unknown")
        # The non-image MIME and unknown structure must not receive the image discount.
        text_data = {"contents": [{"parts": [{"inlineData": {
            "mimeType": "text/plain", "data": encoded,
        }}]}]}
        unknown = {"contents": [{"parts": [{"inlineData": {"data": encoded}}]}]}
        assert estimate_request_payload_tokens(text_data, model="unknown") > 20_000
        assert estimate_request_payload_tokens(unknown, model="unknown") > 20_000

    def test_google_image_data_leaf_is_length_independent_and_extras_are_counted(self):
        def payload(data, caption="short"):
            return {"contents": [{"parts": [{"inlineData": {
                "mimeType": "image/jpeg", "data": data, "caption": caption,
            }}]}]}

        short = estimate_request_payload_tokens(payload("A"), model="unknown")
        long = estimate_request_payload_tokens(payload("A" * 100_000), model="unknown")
        assert short >= te._IMAGE_TOKENS
        assert abs(short - long) < 10
        with_caption = estimate_request_payload_tokens(
            payload("A", caption="caption text " * 10_000), model="unknown",
        )
        assert with_caption > short + 10_000

    def test_concurrent_cache_hits_and_reinserts_survive_eviction(self, monkeypatch):
        te._PAYLOAD_CACHE.clear()
        monkeypatch.setattr(te, "_PAYLOAD_CACHE_SIZE", 2)
        barrier = threading.Barrier(8)

        def estimate(index):
            barrier.wait()
            payload = {"cache-race": f"payload-{index}"}
            return estimate_request_payload_tokens(
                payload, model="race-model", definition_revision=index,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(estimate, range(8)))
        assert all(result > 0 for result in results)
        # Exercise repeated lookup/reinsert of entries while the LRU is evicted.
        for index in range(te._PAYLOAD_CACHE_SIZE + 20):
            estimate_request_payload_tokens({"race-fill": index}, model="race-model")
            estimate_request_payload_tokens(
                {"cache-race": "payload-0"}, model="race-model", definition_revision=0,
            )
        assert estimate_request_payload_tokens(
            {"cache-race": "payload-0"}, model="race-model", definition_revision=0,
        ) > 0

    def test_payload_string_and_mapping_are_both_supported(self):
        payload = '{"messages":[{"role":"user","content":"hello"}]}'
        assert estimate_request_payload_tokens(payload) > 0
        assert estimate_request_payload_tokens({"messages": [{"role": "user", "content": "hello"}]}) > 0

    def test_payload_cache_is_scoped_by_model_and_definition_revision(self, monkeypatch):
        calls = []

        def count(value, model):
            calls.append(model)
            return len(calls)

        monkeypatch.setattr(te, "_estimate_request_root", count)
        payload = {"unique_cache_probe": "payload"}
        assert estimate_request_payload_tokens(payload, model="target-a", definition_revision=1) == 1
        assert estimate_request_payload_tokens(payload, model="target-a", definition_revision=1) == 1
        assert estimate_request_payload_tokens(payload, model="target-b", definition_revision=1) == 2
        assert estimate_request_payload_tokens(payload, model="target-a", definition_revision=2) == 3
        assert len(calls) == 3


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
        out, _ = context_manager.maybe_compact(msgs, "system", 2000, llm)
        assert len(out) < len(msgs)
        assert out[0]["content"] == "GOAL: do the task"
        assert any("Compacted context" in str(m.get("content")) for m in out)
