"""Built-in tool descriptors (name + model-facing description).

Stateless leaf module: defines the ``BuiltinTool`` dataclass and the
``BUILTIN_TOOLS`` registry (including the ``vision_query`` descriptor, which is
enumerated as a built-in but executed by the ReAct loop, not the executor's
dispatch). No imports back into ``builtin_executor`` or any handler module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BuiltinTool:
    name: str
    description: str
    language: str = "python"
    path: str = "<builtin>"
    is_generated: bool = False


BUILTIN_TOOLS: dict[str, BuiltinTool] = {
    "secret_get": BuiltinTool(
        name="secret_get",
        description="Retrieve a value from the vault by key. Args: key (str, REQUIRED). Requires user confirmation.",
    ),
    "shell": BuiltinTool(
        name="shell",
        description="Execute a shell command on the host system. Args: command (str), timeout (int, default 30).",
    ),
    "file_read": BuiltinTool(
        name="file_read",
        description="Read a file from the filesystem. Args: path (str), max_bytes (int, default 50000), offset (int, default 0). Negative offset counts from end of file (e.g. -5000 reads last 5000 bytes, like tail).",
    ),
    "file_write": BuiltinTool(
        name="file_write",
        description="Write content to a file on the filesystem. Args: path (str), content (str), mode (str: 'w' or 'a', default 'w').",
    ),
    "file_diff": BuiltinTool(
        name="file_diff",
        description=(
            "Compare two files and return a traditional unified diff. "
            "Read-only and non-destructive. "
            "Args: path_a (str, required — first/old file), "
            "path_b (str, required — second/new file), "
            "context_lines (int, default 3 — lines of context around changes), "
            "max_bytes (int, default 200000 — per-file read cap). "
            "Returns the unified diff text, or 'Files are identical.' when there are no differences."
        ),
    ),
    "file_send": BuiltinTool(
        name="file_send",
        description=(
            "Send a local file or photo from the server to the Telegram chat. "
            "Args: path (str, required — absolute or relative path to the file), "
            "caption (str, optional — text shown below the file/photo)."
        ),
    ),
    "schedule": BuiltinTool(
        name="schedule",
        description=(
            "Manage scheduled jobs and reminders. "
            "Args: action (str: list|add|remove|pause|resume|run_now), "
            "tag (str, unique job name), "
            "task (str, REQUIRED for add — the natural-language goal or reminder text), "
            "cron (str, 5-field cron expression in local time, e.g. '0 */6 * * *' = every 6h, "
            "'0 2 * * *' = daily at 02:00, '*/30 * * * *' = every 30 min). "
            "For one-time reminders use schedule_type='once' with run_at='HH:MM'. "
            "Legacy fields hours/minutes/time are still accepted and auto-converted to cron. "
            "notify (bool, default true). "
            "model (str, optional — model identifier to use for this job's sub-agent, e.g. 'gpt-4o'). "
            "preserve_context (bool, default false — if true, conversation history is kept between runs). "
            "max_iterations (int, optional — override the step limit for this job; "
            "default: scheduled_max_iterations from config, 0 = unlimited). "
            "Always provide a non-empty task when adding any job."
        ),
    ),
    "spawn_agent": BuiltinTool(
        name="spawn_agent",
        description=(
            "Spawn an isolated sub-agent in the background for a long-running or model-specific task. "
            "Returns immediately with agent_id — use get_agent_result(agent_id) to retrieve the result.\n"
            "\n"
            "WRITING A GOOD TASK — sub-agents run in complete isolation (no shared context, memory, or files):\n"
            "  • State the OBJECTIVE clearly in the first sentence.\n"
            "  • Include ALL context the sub-agent needs: file paths already on disk, data already extracted,\n"
            "    language requirements, relevant facts, constraints.\n"
            "  • Specify which TOOLS to use (shell, file_read, etc.) and the order if sequence matters.\n"
            "  • Specify the exact OUTPUT required: format, language, structure, length.\n"
            "  • Do NOT rely on sub-agent improvisation — be explicit and complete.\n"
            "  • Sub-agents cannot spawn further sub-agents.\n"
            "\n"
            "Args:\n"
            "  task            (str, REQUIRED) — self-contained instructions for the sub-agent.\n"
            "                  Must be named 'task', NOT 'prompt', 'goal', or 'description'.\n"
            "  model           (str, optional) — model id from AVAILABLE MODELS (default: background_model).\n"
            "  response_format (str, optional) — 'text' (default) | 'json' | 'file'.\n"
            "                  json → sub-agent must return a single valid JSON object.\n"
            "                  file → sub-agent writes output to a file and returns the absolute path.\n"
            "  context_payload (dict, optional) — parent context to inject into the sub-agent's system prompt.\n"
            "  context_key     (str, optional) — key for persisting conversation history between calls.\n"
            "  max_tokens      (int, optional) — override maximum tokens in the sub-agent's response.\n"
            "  temperature     (float, optional) — override sampling temperature (0.0–2.0).\n"
            "  top_p           (float, optional) — override nucleus sampling probability (0.0–1.0).\n"
            "\n"
            "Example (good task — self-contained):\n"
            "{\"task\": \"Summarise the podcast transcript already saved at /tmp/piclaw/clean_transcript.txt "
            "in Russian. Use file_read to load the file. Return a structured report with three sections: "
            "Key Topics, Main Arguments, Conclusions. Plain text, maximum 800 words.\", "
            "\"model\": \"kimi-k2.5:cloud\", \"response_format\": \"text\"}"
        ),
    ),
    "get_agent_result": BuiltinTool(
        name="get_agent_result",
        description=(
            "Wait for a sub-agent to finish and retrieve its result. "
            "Blocks until the sub-agent completes or the timeout is reached. "
            "Args: agent_id (str, REQUIRED — the id returned by spawn_agent), "
            "timeout (int, optional — seconds to wait, default: configured subagent_result_timeout), "
            "cancel_on_timeout (bool, optional — if true (default), the sub-agent is automatically cancelled "
            "when the timeout expires so it does not waste tokens or send a stale notification; "
            "set to false only if you intend to call get_agent_result again for the same agent). "
            "Returns: {status: 'done'|'failed'|'cancelled'|'timeout'|'not_found', "
            "result_type: 'text'|'json'|'file', result: <output>}. "
            "Example: {\"agent_id\": \"sa-abc123\"}"
        ),
    ),
    "wait_for_any_agent": BuiltinTool(
        name="wait_for_any_agent",
        description=(
            "Wait for the first of a set of sub-agents to finish and return its result. "
            "Call repeatedly to collect results in completion order (council pattern). "
            "Args: agent_ids (list[str], REQUIRED), timeout (int, optional — seconds to wait, "
            "default: configured subagent_result_timeout). "
            "Returns: {status: 'done'|'failed'|'cancelled'|'timeout', agent_id, result}. "
            "Example: {\"agent_ids\": [\"sa-abc123\", \"sa-def456\"]}"
        ),
    ),
    "cancel_agent": BuiltinTool(
        name="cancel_agent",
        description=(
            "Cancel a spawned sub-agent or all managed sub-agents. Not confirmation-gated. "
            "Args: agent_id (str, REQUIRED — sub-agent id, 'managed', or 'all'). "
            "Returns: {success, output}. "
            "Example: {\"agent_id\": \"sa-abc123\"} or {\"agent_id\": \"managed\"}"
        ),
    ),
    "memory_write": BuiltinTool(
        name="memory_write",
        description=(
            "Read or write the agent's persistent memory (data/memory.json). "
            "Actions: "
            "  set    — store any value under a key: args: key (str), value (any). "
            "  append — append an item to a list key (creates the list if needed): args: key (str), value (any). "
            "  delete — remove a key: args: key (str). "
            "  get    — retrieve a single key: args: key (str). "
            "Use 'append' on key 'notes' to add a persistent note. "
            "Examples: "
            "{\"action\":\"append\",\"key\":\"notes\",\"value\":\"Disk replaced 2025-04-01\"}, "
            "{\"action\":\"set\",\"key\":\"last_backup\",\"value\":\"2025-04-05\"}, "
            "{\"action\":\"delete\",\"key\":\"old_key\"}."
        ),
    ),
    "vision_query": BuiltinTool(
        name="vision_query",
        description=(
            "Ask the active LLM to analyse a local image file. "
            "Use this whenever the user asks about the contents of an image or photo. "
            "Args: path (str, required — absolute path to the image file on disk), "
            "question (str, required — what to ask about the image, e.g. 'Who is in this photo?'). "
            "Returns the LLM's text description/answer. "
            "Only works with vision-capable models (GPT-4o, Claude 3+, Gemini, LLaVA, etc.). "
            "Example: {\"path\": \"/home/pi/downloads/photo.jpg\", \"question\": \"What is in this image?\"}"
        ),
    ),
    "file_patch": BuiltinTool(
        name="file_patch",
        description=(
            "Make a surgical search-and-replace edit to a file. "
            "Prefer this over file_read + file_write when making small targeted changes. "
            "Args: "
            "  path       (str, required) — absolute path to the file. "
            "  old_str    (str, required) — exact text to find in the file; include enough surrounding "
            "                              context (e.g. the whole line) to be unambiguous. "
            "  new_str    (str, required) — replacement text (may be empty string to delete old_str). "
            "  occurrence (int, optional, default 1) — which occurrence to replace (1 = first); "
            "                                          0 = replace all occurrences. "
            "Returns an error (no changes made) if old_str is not found or matches more than one "
            "occurrence when occurrence=1. "
            "Always requires operator confirmation — confirmation shows a diff-style preview. "
            "Example: {\"path\": \"/etc/app/config.toml\", \"old_str\": \"port = 8080\", \"new_str\": \"port = 9090\"}"
        ),
    ),
    "memory_graph_search": BuiltinTool(
        name="memory_graph_search",
        description=(
            "Search the knowledge graph for facts, entities, people, preferences, or past events. "
            "Returns relevant entities and relationships from the graph memory. "
            "Args: query (str, required) — what to search for. "
            "Only available when graph memory is enabled ([graph_memory] enabled = true in config). "
            "ALWAYS call this before saying 'I don't have information about...' regarding past events "
            "or user preferences. "
            "Example: {\"query\": \"user preferred languages\"}"
        ),
    ),
    "memory_graph_store": BuiltinTool(
        name="memory_graph_store",
        description=(
            "Store an important fact, preference, or relationship in the knowledge graph. "
            "Use this when the user shares important facts or preferences that should be remembered. "
            "Args: "
            "  content     (str, required) — the fact or information to remember. "
            "  entity_type (str, optional) — type hint: person, tool, concept, preference, other. "
            "  user_id     (str, optional) — user identifier (default: 'agent'). "
            "Only available when graph memory is enabled ([graph_memory] enabled = true in config). "
            "Example: {\"content\": \"User prefers Python over JavaScript for automation scripts\", "
            "\"entity_type\": \"preference\"}"
        ),
    ),
    "log_query": BuiltinTool(
        name="log_query",
        description=(
            "Query the agent's own structured run log (the active JSONL sink) to inspect "
            "recent tool activity, errors, and events for the current or a specified run. "
            "Reads ONLY the recent TAIL of the active log (the most recent lines/bytes), so "
            "results reflect a recent window, not the entire run history. "
            "Read-only and non-destructive; all arguments are optional. "
            "Args: "
            "  trace       (str) — run trace id (e.g. 'r-1a2b3c4d'); defaults to the CURRENT run "
            "                      when no text/query is given. "
            "                      Use '*' (or '') to explicitly search across all runs/traces. "
            "  level       (str) — minimum level NAME to include: DEBUG|INFO|WARNING|ERROR|CRITICAL. "
            "  event_type  (str) — exact event type to match: TOOL_START|TOOL_END|TOOL_FAILED|"
            "LLM_CALL|LLM_FAILED|STEP_BEGIN|STEP_END|RUN_BEGIN|RUN_END|ERROR. "
            "  tool        (str) — exact tool name to match (e.g. 'shell', 'file_read'). "
            "  since       (str) — ISO timestamp; only include records at or after this time. "
            "  limit       (int, default 50) — max records to return (the most recent are kept if more match). "
            "  text        (str) — Unicode-aware case-insensitive (casefold) substring search "
            "                      against the compact JSON serialisation of each record "
            "                      (keys, string values, and all punctuation are all searchable). "
            "                      Alias 'query' is also accepted. "
            "                      When text/query is given without level or event_type, the "
            "                      high-signal default view is NOT applied, so all INFO records "
            "                      (including startup messages such as 'GraphMemoryStore initialised') "
            "                      are visible. "
            "                      When text/query is given and no explicit trace is provided, the "
            "                      scope is automatically widened to ALL traces so that startup "
            "                      records (which may carry no trace or a different trace) are found. "
            "                      Pass an explicit trace to restrict a text search to a single run. "
            "When neither level, event_type, nor text/query is given, a high-signal default view "
            "is returned: warnings/errors plus TOOL_START/TOOL_END/LLM_CALL events (routine "
            "STEP_* events are omitted). "
            "Returns a JSON object with 'records', 'count', 'truncated', 'total_matched', "
            "'window_saturated', and 'scanned_lines'. NOTE: the active log is shared across all "
            "traces, so 'total_matched' is a count within the scanned recent window (over "
            "'scanned_lines' lines), NOT a full-run total; when 'window_saturated' is true, older "
            "records fell outside the scanned window — narrow with 'since'/'tool'/'event_type' or "
            "treat counts as a recent-window lower bound. "
            "Examples: {\"tool\": \"shell\", \"limit\": 20}, "
            "{\"text\": \"GraphMemoryStore\", \"limit\": 10}"
        ),
    ),
    "shell_env_set": BuiltinTool(
        name="shell_env_set",
        description="Set a session-scoped environment variable for shell commands. "
        "The variable is injected via nsjail -E flags on subsequent shell calls. "
        "Args: key (str, REQUIRED), value (str, REQUIRED).",
    ),
    "shell_env_unset": BuiltinTool(
        name="shell_env_unset",
        description="Remove a session-scoped environment variable. "
        "Args: key (str, REQUIRED).",
    ),
    "shell_env_list": BuiltinTool(
        name="shell_env_list",
        description="List all session-scoped shell environment variables. "
        "Returns a JSON object with an 'env' field containing the variable dict.",
    ),
    "shell_env_get": BuiltinTool(
        name="shell_env_get",
        description="Get the value of a session-scoped shell environment variable. "
        "Args: key (str, REQUIRED). Returns empty string if not set.",
    ),
    "context_profile": BuiltinTool(
        name="context_profile",
        description="Return a JSON snapshot of the current context-window consumption: token counts by category (system, history, tool defs, completion), danger level, headroom, and per-MCP-server tool-def breakdown. No arguments.",
    ),
}
