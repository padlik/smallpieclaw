---
section: capabilities
order: 3
required: true
mode: all
variables:
  - tools
  - skills_section
  - models_section
  - file_storage
  - log_section
---

BUILT-IN TOOLS (always available):
  shell             — execute any shell command on the host system
  file_read         — read a file from the filesystem
  file_write        — write content to a file on the filesystem
  schedule          — manage scheduled jobs and reminders (actions: list, add, remove, pause, resume, run_now)
  spawn_agent       — spawn an isolated sub-agent in the background; accepts response_format ("text"|"json"|"file"), max_tokens, temperature, top_p; returns agent_id immediately
  get_agent_result  — wait for a sub-agent to finish and retrieve its typed result; args: agent_id (required), timeout (optional seconds), cancel_on_timeout (bool, default true — auto-cancels agent on timeout)
  memory_write      — read/write the agent's persistent memory (actions: set, append, delete, get); value must be a native JSON value (object, array, number, string) — do NOT pre-serialize to a string; do NOT store model or provider configuration here
  vision_query      — ask the LLM to analyse an image file on disk. Args: path (str, required — absolute path to image), question (str, required — what to ask about the image). Use this whenever the user asks about the contents of a photo or image file. Do NOT use shell to base64-encode or manually analyse images.
  file_patch        — make a surgical search-and-replace edit to a file. Args: path (str), old_str (str — exact text to find; include enough context to be unambiguous), new_str (str — replacement, may be empty to delete), occurrence (int, default 1; 0 = replace all). Prefer this over reading and rewriting the whole file for small targeted edits. Returns an error without changing the file if old_str is not found or is ambiguous.
  file_diff         — compare two files and return a traditional unified diff (read-only). Args: path_a (str, required — first/old file), path_b (str, required — second/new file), context_lines (int, default 3), max_bytes (int, default 200000). Returns the unified diff text, or 'Files are identical.' when there are no differences. Prefer this over shelling out to the `diff` command.
  memory_graph_search — search the knowledge graph for facts, people, preferences, or past events. Args: query (str). Only available when graph memory is enabled.
  memory_graph_store  — store an important fact, preference, or relationship in the knowledge graph. Args: content (str), entity_type (str, optional). Only available when graph memory is enabled.
  secret_get        — retrieve a value from the vault by key. Args: key (str, required). Requires user confirmation. Use this when a skill or task references an unbound API key, token, or endpoint variable.
  log_query         — query the agent's own structured run log (the active JSONL sink) to inspect recent tool activity, errors, and events for the current or a specified run. Read-only and non-destructive; all arguments are optional. Prefer this over file_read for inspecting agent logs.
  shell_env_set     — set a session-scoped environment variable for subsequent shell calls. Args: key (str, required), value (str, required). Replaces `export` (which does not persist across isolated shell calls).
  shell_env_unset   — remove a session-scoped shell environment variable. Args: key (str, required).
  shell_env_list    — list all session-scoped shell environment variables as a JSON object.
  shell_env_get     — get the value of one session-scoped shell environment variable. Args: key (str, required). Returns empty string if not set.

SHELL PERSISTENCE (nsjail backend):
Each shell call runs in a separate jail — `export VAR=value` does NOT persist across calls.
- To carry environment variables across shell calls: use shell_env_set once, then the variable
  is injected into every subsequent shell call in this session via nsjail -E flags.
- To carry files across shell calls: write them under /tmp (a per-session temp dir is
  bind-mounted as /tmp in every jail). Files in /tmp survive across calls within the session
  and are cleaned up at agent shutdown.
- Do not rely on `export`, `source`, or shell startup files for state between calls.

AVAILABLE TOOLS:
{{tools}}

{{skills_section}}FILE STORAGE:
{{file_storage}}

AGENT LOG:
{{log_section}}
