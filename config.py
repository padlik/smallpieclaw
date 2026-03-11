"""
Configuration for the Telegram AI Agent.
Copy to config_local.py and fill in your values, or set environment variables.
"""

import os

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# Security: comma-separated list of allowed Telegram user IDs (integers).
# Leave empty to use PAIRING mode instead.
ALLOWED_USER_IDS: list[int] = [
    # int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x
]

# Pairing mode: if ALLOWED_USER_IDS is empty, users must send /pair <PIN>
PAIRING_PIN = os.getenv("PAIRING_PIN", "changeme123")

# ── LLM Provider ──────────────────────────────────────────────────────────────
# Supported: "openai" | "claude" | "google" | "openrouter"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY",    "")
OPENAI_MODEL      = os.getenv("OPENAI_MODEL",      "gpt-4o-mini")

CLAUDE_API_KEY    = os.getenv("CLAUDE_API_KEY",    "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL",      "claude-3-5-haiku-20241022")

GOOGLE_API_KEY    = os.getenv("GOOGLE_API_KEY",    "")
GOOGLE_MODEL      = os.getenv("GOOGLE_MODEL",      "gemini-1.5-flash")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "mistralai/mistral-7b-instruct")

# Embedding provider for semantic search (openai recommended; falls back to simple TF-IDF)
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai")   # "openai" | "google" | "none"
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL",    "text-embedding-3-small")

# ── Agent ─────────────────────────────────────────────────────────────────────
MAX_STEPS          = int(os.getenv("MAX_STEPS", "8"))
TOOL_TIMEOUT_SEC   = int(os.getenv("TOOL_TIMEOUT_SEC", "10"))
TOOL_OUTPUT_MAX    = int(os.getenv("TOOL_OUTPUT_MAX", "2000"))   # chars
MAX_TOOL_FILE_SIZE = int(os.getenv("MAX_TOOL_FILE_SIZE", "65536"))  # bytes

# ── Paths ─────────────────────────────────────────────────────────────────────
import pathlib
BASE_DIR         = pathlib.Path(__file__).parent
TOOLS_DIR        = BASE_DIR / "tools"
TOOLS_GEN_DIR    = BASE_DIR / "tools_generated"
# Allow Docker (or any deployment) to redirect persistent files via env vars
MEMORY_FILE       = pathlib.Path(os.getenv("MEMORY_FILE",       str(BASE_DIR / "memory.json")))
TOOL_INDEX_FILE   = pathlib.Path(os.getenv("TOOL_INDEX_FILE",   str(BASE_DIR / "tool_index.json")))
PAIRED_USERS_FILE = pathlib.Path(os.getenv("PAIRED_USERS_FILE", str(BASE_DIR / "paired_users.json")))

# ── Scheduler ─────────────────────────────────────────────────────────────────
SCHEDULER_JOBS = [
    # Each entry: (cron_expression, goal_string, job_id)
    # Uses APScheduler cron syntax
    ("0 3 * * *",  "Check system health and report any issues",    "nightly_health"),
    ("0 * * * *",  "Check disk usage and alert if above 85%",      "hourly_disk"),
]
