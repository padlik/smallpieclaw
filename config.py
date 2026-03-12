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
# Supported: "openai" | "claude" | "google" | "openrouter" | "openai_compatible"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL",   "gpt-4o-mini")
# Optional: override the API base URL for any OpenAI-compatible endpoint.
# When set, this takes effect even for LLM_PROVIDER=openai (e.g. Azure, proxies).
# Example: OPENAI_BASE_URL=http://localhost:11434/v1  (Ollama)
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "") or None   # None = use default

# ── Claude ────────────────────────────────────────────────────────────────────
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL",   "claude-3-5-haiku-20241022")

# ── Google ────────────────────────────────────────────────────────────────────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_MODEL   = os.getenv("GOOGLE_MODEL",   "gemini-1.5-flash")

# ── OpenRouter ────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "mistralai/mistral-7b-instruct")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"   # fixed; not overridable

# ── Generic OpenAI-compatible endpoint ────────────────────────────────────────
# Use LLM_PROVIDER=openai_compatible and set these three vars to point at
# any server that speaks the OpenAI Chat Completions API:
#   Ollama:      OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1
#   LM Studio:   OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234/v1
#   vLLM:        OPENAI_COMPATIBLE_BASE_URL=http://my-server:8000/v1
#   Groq:        OPENAI_COMPATIBLE_BASE_URL=https://api.groq.com/openai/v1
#   Together AI: OPENAI_COMPATIBLE_BASE_URL=https://api.together.xyz/v1
OPENAI_COMPATIBLE_BASE_URL = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "http://localhost:11434/v1")
OPENAI_COMPATIBLE_API_KEY  = os.getenv("OPENAI_COMPATIBLE_API_KEY",  "ollama")   # many local servers accept any string
OPENAI_COMPATIBLE_MODEL    = os.getenv("OPENAI_COMPATIBLE_MODEL",    "llama3")

# Embedding provider for semantic search (openai recommended; falls back to simple TF-IDF)
# "openai" | "google" | "openai_compatible" | "none"
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL",    "text-embedding-3-small")
# When EMBEDDING_PROVIDER=openai_compatible, uses OPENAI_COMPATIBLE_BASE_URL/KEY above.

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
