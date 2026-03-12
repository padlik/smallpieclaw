# memory.py
import json
import os
from typing import Dict, Any
from datetime import datetime

class Memory:
    def __init__(self, file_path: str = Config.MEMORY_FILE):
        self.file_path = file_path
        self.data = self._load()
    
    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.file_path):
            with open(self.file_path, 'r') as f:
                return json.load(f)
        return {
            "last_backup_date": None,
            "known_services": [],
            "previous_diagnostics": [],
            "created_at": datetime.now().isoformat()
        }
    
    def save(self):
        with open(self.file_path, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    def get(self, key: str, default=None):
        return self.data.get(key, default)
    
    def set(self, key: str, value: Any):
        self.data[key] = value
        self.save()
    
    def get_context(self) -> str:
        """Get memory as context string for LLM"""
        lines = ["System Memory:"]
        for k, v in self.data.items():
            if k != "created_at":
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)

