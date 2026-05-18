"""Tests for AgentController JSON parsing — _parse_json and _extract_json_candidates."""

from __future__ import annotations

import json

from agent_controller import AgentController


class TestExtractJsonCandidates:
    """Test brace-counting extractor."""

    def test_single_object(self):
        text = '{"action": "finish", "result": "done"}'
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) == 1
        assert json.loads(candidates[0])["action"] == "finish"

    def test_multiple_objects(self):
        text = '{"a": 1} and {"b": 2}'
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) == 2

    def test_nested_braces(self):
        text = '{"a": {"nested": true}}'
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) == 1
        obj = json.loads(candidates[0])
        assert obj["a"]["nested"] is True

    def test_braces_in_strings(self):
        text = '{"code": "function() { return {}; }"}'
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) == 1
        obj = json.loads(candidates[0])
        assert "function()" in obj["code"]

    def test_prose_wrapped_json(self):
        text = "Here's my response:\n```json\n{\"action\": \"tool\"}\n```\nDone."
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) >= 1

    def test_empty_string(self):
        assert AgentController._extract_json_candidates("") == []

    def test_no_json(self):
        assert AgentController._extract_json_candidates("just plain text") == []

    def test_unbalanced_braces(self):
        text = '{"unclosed": true'
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) == 0

    def test_escaped_quotes(self):
        text = r'{"text": "he said \"hello\""}'
        candidates = AgentController._extract_json_candidates(text)
        assert len(candidates) == 1


class TestParseJson:
    """Test _parse_json fallback chain."""

    def test_direct_json(self):
        text = '{"action": "finish", "result": "done"}'
        obj = AgentController._parse_json(text)
        assert obj == {"action": "finish", "result": "done"}

    def test_json_with_whitespace(self):
        text = '  \n  {"action": "finish", "result": "ok"}  \n  '
        obj = AgentController._parse_json(text)
        assert obj["action"] == "finish"

    def test_markdown_fenced_json(self):
        text = '```json\n{"action": "tool", "tool": "shell"}\n```'
        obj = AgentController._parse_json(text)
        assert obj["action"] == "tool"
        assert obj["tool"] == "shell"

    def test_markdown_fence_without_lang(self):
        text = '```\n{"action": "finish", "result": "x"}\n```'
        obj = AgentController._parse_json(text)
        assert obj["action"] == "finish"

    def test_prose_wrapped_json(self):
        text = "Let me think about this.\n\n{\"action\": \"tool\", \"tool\": \"shell\", \"args\": {\"command\": \"ls\"}}\n\nThat should work."
        obj = AgentController._parse_json(text)
        assert obj["action"] == "tool"
        assert obj["tool"] == "shell"

    def test_prefers_action_key(self):
        # Two JSON objects: first without "action", second with
        text = '{"name": "test"} {"action": "finish", "result": "ok"}'
        obj = AgentController._parse_json(text)
        assert obj["action"] == "finish"

    def test_falls_back_to_first_valid_dict(self):
        # No "action" key in either — returns first valid dict
        text = '{"foo": 1} {"bar": 2}'
        obj = AgentController._parse_json(text)
        assert obj == {"foo": 1}

    def test_empty_string(self):
        assert AgentController._parse_json("") is None

    def test_whitespace_only(self):
        assert AgentController._parse_json("   \n\n  ") is None

    def test_invalid_json(self):
        assert AgentController._parse_json("this is not json at all") is None

    def test_array_ignored(self):
        # JSON arrays are not dicts — should not be returned
        text = '[1, 2, 3]'
        assert AgentController._parse_json(text) is None

    def test_deeply_nested(self):
        obj = {
            "action": "tool",
            "tool": "shell",
            "args": {"command": "echo 'test'"},
            "thought": "I need to run a command",
        }
        text = json.dumps(obj)
        result = AgentController._parse_json(text)
        assert result == obj

    def test_special_chars_in_values(self):
        obj = {"action": "finish", "result": "Line 1\nLine 2\t<tag>&amp;"}
        text = json.dumps(obj)
        result = AgentController._parse_json(text)
        assert result["result"] == "Line 1\nLine 2\t<tag>&amp;"

    def test_unicode_content(self):
        obj = {"action": "finish", "result": "Привет мир 🎉"}
        text = json.dumps(obj, ensure_ascii=False)
        result = AgentController._parse_json(text)
        assert "Привет" in result["result"]

    def test_json_with_trailing_comma_is_invalid(self):
        # Trailing commas are invalid JSON — should fail direct parse
        # but might be extracted by brace counter
        text = '{"action": "finish", "result": "ok",}'
        # json.loads fails on trailing comma, so _parse_json returns None
        result = AgentController._parse_json(text)
        assert result is None

    def test_multiple_fenced_blocks_first_wins(self):
        text = '```json\n{"action": "finish", "result": "first"}\n```\n```json\n{"action": "tool"}\n```'
        result = AgentController._parse_json(text)
        # First match from regex wins
        assert result["result"] == "first"
