import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class Config:
    # Telegram Configuration
    TELEGRAM_TOKEN: str
    
    # LLM Configuration (for reasoning)
    LLM_BASE_URL: str
    LLM_API_KEY: str
    LLM_MODEL: str
    
    # Embeddings Configuration (separate from reasoning LLM)
    EMBEDDINGS_BASE_URL: str
    EMBEDDINGS_API_KEY: str
    EMBEDDINGS_MODEL: str
    
    # Tool Configuration
    TOOLS_DIR: str
    TOOLS_GENERATED_DIR: str
    
    # System Limits
    MAX_STEPS: int = 8
    TOOL_TIMEOUT_SECONDS: int = 10
    MAX_OUTPUT_BYTES: int = 20000
    
    # Security/Access Control
    ALLOWED_IDS_PATH: str = "./allowed_ids.json"
    ADMIN_ID: Optional[int] = None
    PAIR_SECRET: Optional[str] = None
    
    @staticmethod
    def from_env():
        return Config(
            # Base config
            TELEGRAM_TOKEN=os.environ.get("TM_TELEGRAM_TOKEN", ""),
            
            # LLM config
            LLM_BASE_URL=os.environ.get("TM_LLM_BASE_URL", "https://api.openai.com/v1"),
            LLM_API_KEY=os.environ.get("TM_LLM_API_KEY", ""),
            LLM_MODEL=os.environ.get("TM_LLM_MODEL", "gpt-4o-mini"),
            
            # Embeddings config
            EMBEDDINGS_BASE_URL=os.environ.get("TM_EMBEDDINGS_BASE_URL", "https://api.openai.com/v1"),
            EMBEDDINGS_API_KEY=os.environ.get("TM_EMBEDDINGS_API_KEY", ""),
            EMBEDDINGS_MODEL=os.environ.get("TM_EMBEDDINGS_MODEL", "text-embedding-3-small"),
            
            # Tool directories
            TOOLS_DIR=os.environ.get("TM_TOOLS_DIR", "./tools"),
            TOOLS_GENERATED_DIR=os.environ.get("TM_TOOLS_GENERATED_DIR", "./tools/tools_generated"),
            
            # System limits
            MAX_STEPS=int(os.environ.get("TM_MAX_STEPS", "8")),
            TOOL_TIMEOUT_SECONDS=int(os.environ.get("TM_TOOL_TIMEOUT", "10")),
            MAX_OUTPUT_BYTES=int(os.environ.get("TM_MAX_OUTPUT_BYTES", "20000")),
            
            # Security settings
            ALLOWED_IDS_PATH=os.environ.get("TM_ALLOWED_IDS_PATH", "./allowed_ids.json"),
            ADMIN_ID=int(os.environ.get("TM_ADMIN_ID", "0")) if os.environ.get("TM_ADMIN_ID") else None,
            PAIR_SECRET=os.environ.get("TM_PAIR_SECRET"),
        )

config = Config.from_env()

