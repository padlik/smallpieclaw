# config.py
import os
from typing import List

class Config:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x]
    ADMIN_USER = int(os.getenv("ADMIN_USER", "0"))
    
    # LLM Configuration (supports OpenAI, Anthropic, Google, OpenRouter)
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")  # openai, anthropic, google, openrouter
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", None)  # For OpenRouter or custom endpoints
    
    # Embedding model
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    
    # Paths
    TOOLS_DIR = "tools"
    GENERATED_TOOLS_DIR = "tools_generated"
    MEMORY_FILE = "memory.json"
    TOOL_INDEX_FILE = "tool_index.json"
    
    # Limits
    MAX_AGENT_STEPS = 8
    TOOL_TIMEOUT = 10
    MAX_TOOL_OUTPUT = 4000
    MAX_FILE_SIZE = 10000  # bytes
    
    # Safety
    DANGEROUS_PATTERNS = [
        r'rm\s+-rf\s+/',
        r'>/dev/null.*mkfs',
        r':\(\)\s*{\s*:\|&\s*};',
        r'dd\s+if=.*of=/dev/[sh]d[a-z]',
        r'curl.*\|.*sh',
    ]

