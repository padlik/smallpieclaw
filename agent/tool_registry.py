import os
import re
from pathlib import Path
from typing import List, Dict

import config

config_instance = config.config

TOOL_RE = re.compile(r'#\s*tool:\s*(?P<name>[A-Za-z0-9_]+)', re.IGNORECASE)
DESC_RE = re.compile(r'#\s*description:\s*(?P<desc>.*)', re.IGNORECASE)

class ToolRegistry:
    def __init__(self, base_dir: str, generated_dir: str):
        self.base_dir = Path(base_dir)
        self.generated_dir = Path(generated_dir)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self._tools: Dict[str, Dict] = {}
        self.scan()

    def _extract_metadata_from_file(self, path: Path):
        name = None
        description = ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m1 = TOOL_RE.match(line)
                if m1:
                    name = m1.group("name")
                m2 = DESC_RE.match(line)
                if m2:
                    if not description:
                        description = m2.group("desc").strip()
                if name and description:
                    break
        if not name:
            name = path.stem
        return {
            "name": name,
            "description": description.strip(),
            "path": str(path),
            "type": path.suffix.lstrip("."),
            "executable": os.access(path, os.X_OK)
        }

    def scan(self):
        self._tools.clear()
        dirs = [self.base_dir, self.generated_dir]
        for d in dirs:
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in [".sh", ".py"]:
                    meta = self._extract_metadata_from_file(p)
                    self._tools[meta["name"]] = meta

    def list_tools(self) -> List[Dict]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> Dict:
        return self._tools.get(name)

registry = ToolRegistry(config_instance.TOOLS_DIR, config_instance.TOOLS_GENERATED_DIR)

