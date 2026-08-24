"""
builtin_tools/schemas.py
------------------------
OpenAI-format JSON Schema parameter definitions for all built-in tools,
pseudo-tools (plan), and a builder that merges them with
MCP tool schemas into a single tool definitions array.

Co-located with descriptors.py per ADR-0008. The arg descriptions in
descriptors.py and the JSON Schema properties here describe the same
tool interfaces — keep them in sync when adding or changing tools.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in tool schemas (21 tools, matching BUILTIN_TOOLS in descriptors.py)
# ---------------------------------------------------------------------------

BUILTIN_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "secret_get": {
        "description": "Retrieve a value from the vault by key. Requires user confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The vault key to retrieve (case-sensitive).",
                },
            },
            "required": ["key"],
        },
    },
    "shell": {
        "description": "Execute a shell command on the host system.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30).",
                },
            },
            "required": ["command"],
        },
    },
    "file_read": {
        "description": "Read a file from the filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read (default: 50000).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line offset from start (0-based). Negative counts from end of file.",
                },
            },
            "required": ["path"],
        },
    },
    "file_write": {
        "description": "Write content to a file on the filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file.",
                },
                "mode": {
                    "type": "string",
                    "description": "Write mode: 'w' (overwrite) or 'a' (append). Default: 'w'.",
                    "enum": ["w", "a"],
                },
            },
            "required": ["path", "content"],
        },
    },
    "file_diff": {
        "description": "Compare two files and return a unified diff. Read-only and non-destructive.",
        "parameters": {
            "type": "object",
            "properties": {
                "path_a": {
                    "type": "string",
                    "description": "Path to the first (old) file.",
                },
                "path_b": {
                    "type": "string",
                    "description": "Path to the second (new) file.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context around changes (default: 3).",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Per-file read cap in bytes (default: 200000).",
                },
            },
            "required": ["path_a", "path_b"],
        },
    },
    "file_patch": {
        "description": "Make a surgical search-and-replace edit to a file. Requires operator confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact text to find in the file.",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement text (may be empty to delete).",
                },
                "occurrence": {
                    "type": "integer",
                    "description": "Which occurrence to replace (1 = first, 0 = all). Default: 1.",
                },
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    "file_send": {
        "description": "Send a local file or photo from the server to the Telegram chat.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional text shown below the file/photo.",
                },
            },
            "required": ["path"],
        },
    },
    "spawn_agent": {
        "description": (
            "Spawn an isolated sub-agent in the background for a long-running or "
            "model-specific task. Returns immediately with agent_id — use "
            "get_agent_result(agent_id) to retrieve the result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Self-contained instructions for the sub-agent.",
                },
                "model": {
                    "type": "string",
                    "description": "Model id from AVAILABLE MODELS (default: background_model).",
                },
                "response_format": {
                    "type": "string",
                    "description": "Expected output format: 'text', 'json', or 'file'. Default: 'text'.",
                    "enum": ["text", "json", "file"],
                },
                "context_payload": {
                    "type": "object",
                    "description": "Parent context to inject into the sub-agent's system prompt.",
                },
                "context_key": {
                    "type": "string",
                    "description": "Key for persisting conversation history between calls.",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Override maximum tokens in the sub-agent's response.",
                },
                "temperature": {
                    "type": "number",
                    "description": "Override sampling temperature (0.0–2.0).",
                },
                "top_p": {
                    "type": "number",
                    "description": "Override nucleus sampling probability (0.0–1.0).",
                },
            },
            "required": ["task"],
        },
    },
    "get_agent_result": {
        "description": "Wait for a sub-agent to finish and retrieve its result.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The id returned by spawn_agent.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait (default: configured subagent_result_timeout).",
                },
                "cancel_on_timeout": {
                    "type": "boolean",
                    "description": "Cancel the sub-agent on timeout (default: true).",
                },
            },
            "required": ["agent_id"],
        },
    },
    "wait_for_any_agent": {
        "description": "Wait for the first of a set of sub-agents to finish and return its result.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of sub-agent IDs to wait on.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: configured subagent_result_timeout).",
                },
            },
            "required": ["agent_ids"],
        },
    },
    "cancel_agent": {
        "description": "Cancel a spawned sub-agent or all managed sub-agents.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Sub-agent id, 'managed', or 'all'.",
                },
            },
            "required": ["agent_id"],
        },
    },
    "memory_write": {
        "description": "Read or write the agent's persistent memory (data/memory.json).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Operation: 'set', 'append', 'delete', or 'get'.",
                    "enum": ["set", "append", "delete", "get"],
                },
                "key": {
                    "type": "string",
                    "description": "Memory key.",
                },
                "value": {
                    "description": "Value to store (for 'set' and 'append' actions).",
                },
            },
            "required": ["action", "key"],
        },
    },
    "memory_graph_search": {
        "description": "Search the knowledge graph for facts, entities, people, preferences, or past events.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in the knowledge graph.",
                },
            },
            "required": ["query"],
        },
    },
    "memory_graph_store": {
        "description": "Store an important fact, preference, or relationship in the knowledge graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact or information to remember.",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Type hint: person, tool, concept, preference, other.",
                    "enum": ["person", "tool", "concept", "preference", "other"],
                },
                "user_id": {
                    "type": "string",
                    "description": "User identifier (default: 'agent').",
                },
            },
            "required": ["content"],
        },
    },
    "schedule": {
        "description": "Manage scheduled jobs and reminders.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Operation: list, add, remove, pause, resume, or run_now.",
                    "enum": ["list", "add", "remove", "pause", "resume", "run_now"],
                },
                "tag": {
                    "type": "string",
                    "description": "Unique job name.",
                },
                "task": {
                    "type": "string",
                    "description": "Natural-language goal or reminder text (required for add).",
                },
                "cron": {
                    "type": "string",
                    "description": "5-field cron expression in local time (e.g. '0 */6 * * *').",
                },
                "schedule_type": {
                    "type": "string",
                    "description": "Schedule type: 'cron' or 'once'.",
                    "enum": ["cron", "once"],
                },
                "run_at": {
                    "type": "string",
                    "description": "Run time in HH:MM format (for schedule_type='once').",
                },
                "notify": {
                    "type": "boolean",
                    "description": "Whether to notify on completion (default: true).",
                },
                "model": {
                    "type": "string",
                    "description": "Model identifier for this job's sub-agent.",
                },
                "preserve_context": {
                    "type": "boolean",
                    "description": "Keep conversation history between runs (default: false).",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Override step limit (0 = unlimited).",
                },
            },
            "required": ["action"],
        },
    },
    "log_query": {
        "description": "Query the agent's own structured run log (the active JSONL sink).",
        "parameters": {
            "type": "object",
            "properties": {
                "trace": {
                    "type": "string",
                    "description": "Run trace id (e.g. 'r-1a2b3c4d'). Defaults to current run. Use '*' for all traces.",
                },
                "level": {
                    "type": "string",
                    "description": "Minimum level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
                "event_type": {
                    "type": "string",
                    "description": "Exact event type to match (e.g. TOOL_START, LLM_CALL, ERROR).",
                },
                "tool": {
                    "type": "string",
                    "description": "Exact tool name to match (e.g. 'shell', 'file_read').",
                },
                "since": {
                    "type": "string",
                    "description": "ISO timestamp; only include records at or after this time.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (default: 50).",
                },
                "text": {
                    "type": "string",
                    "description": "Case-insensitive substring search against record JSON. Alias 'query' also accepted.",
                },
                "prompt_id": {
                    "type": "string",
                    "description": "Filter records by the operator-facing prompt ID (globally-unique ULID string).",
                },
            },
            "required": [],
        },
    },
    "vision_query": {
        "description": "Ask the active LLM to analyse a local image file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the image file on disk.",
                },
                "question": {
                    "type": "string",
                    "description": "What to ask about the image (e.g. 'Who is in this photo?').",
                },
            },
            "required": ["path", "question"],
        },
    },
    "shell_env_set": {
        "description": "Set a session-scoped environment variable for shell commands.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Environment variable name."},
                "value": {"type": "string", "description": "Environment variable value."},
            },
            "required": ["key", "value"],
        },
    },
    "shell_env_unset": {
        "description": "Remove a session-scoped environment variable.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Environment variable name to remove."},
            },
            "required": ["key"],
        },
    },
    "shell_env_list": {
        "description": "List all session-scoped shell environment variables.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    "shell_env_get": {
        "description": "Get the value of a session-scoped shell environment variable.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Environment variable name."},
            },
            "required": ["key"],
        },
    },
    "context_profile": {
        "description": "Return a JSON snapshot of the current context-window consumption by category (system prompt, chat history, tool definitions, completion reserve), with per-MCP-server tool-def grouping, danger level, and headroom.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


# ---------------------------------------------------------------------------
# Pseudo-tool schemas (not in BUILTIN_TOOLS — handled by ReAct loop intercepts)
# ---------------------------------------------------------------------------

PSEUDO_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "plan": {
        "description": (
            "Execute a multi-step plan as a DAG of tool calls run as parallel/sequential "
            "sub-agents. Use only when the task genuinely benefits from parallel or "
            "dependent sub-tasks; for a single action, use the tool directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the plan does.",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Unique step identifier."},
                            "tool": {"type": "string", "description": "Tool name to invoke."},
                            "args": {"type": "object", "description": "Arguments for the tool."},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Step ids this step depends on.",
                            },
                            "description": {"type": "string", "description": "One-line description of this step."},
                        },
                        "required": ["id", "tool"],
                    },
                    "description": "Ordered list of plan steps.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Total plan execution timeout in seconds (default: 300).",
                },
            },
            "required": ["description", "steps"],
        },
    },
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_tool_definitions(
    mcp_manager: object | None = None,
) -> list[dict[str, Any]]:
    """Assemble OpenAI-format tool definitions from built-in, pseudo, and MCP schemas.

    Args:
        mcp_manager: Optional MCPManagerProtocol for MCP tool schemas.

    Returns:
        A list of tool definition dicts in OpenAI function-calling format:
        ``{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}``
    """
    tools: list[dict[str, Any]] = []

    # Built-in tools
    for name, schema in BUILTIN_TOOL_SCHEMAS.items():
        tools.append({
            "type": "function",
            "function": {"name": name, **schema},
        })

    # Pseudo-tools
    for name, schema in PSEUDO_TOOL_SCHEMAS.items():
        tools.append({
            "type": "function",
            "function": {"name": name, **schema},
        })

    # MCP tools — skip any whose name collides with a built-in, a pseudo-tool,
    # or an already-added MCP tool. Duplicate ``function.name`` entries confuse
    # providers (two "shell" tools in one array), and a rogue MCP tool must
    # never shadow a trusted built-in.
    if mcp_manager is not None:
        seen_names = {t["function"]["name"] for t in tools}
        mcp_tools = getattr(mcp_manager, "get_tools", lambda: [])()
        for tool in mcp_tools:
            name = tool.name
            if name in seen_names:
                logger.warning(
                    "MCP tool '%s' collides with an existing tool name — skipping.",
                    name,
                )
                continue
            seen_names.add(name)
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": getattr(tool, "input_schema", None) or {"type": "object", "properties": {}},
                },
            })

    return tools


def builtin_tool_names() -> set[str]:
    """Return the set of all built-in and pseudo-tool schema names."""
    return set(BUILTIN_TOOL_SCHEMAS.keys()) | set(PSEUDO_TOOL_SCHEMAS.keys())
