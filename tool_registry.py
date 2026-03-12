# tool_registry.py
import os
import re
from typing import List, Dict, Optional

class ToolRegistry:
    def __init__(self):
        self.tools_dir = Config.TOOLS_DIR
        self.generated_dir = Config.GENERATED_TOOLS_DIR
        self.tools: Dict[str, Dict] = {}
        self._ensure_dirs()
        self.scan_tools()
    
    def _ensure_dirs(self):
        os.makedirs(self.tools_dir, exist_ok=True)
        os.makedirs(self.generated_dir, exist_ok=True)
    
    def scan_tools(self):
        """Scan both tool directories for scripts"""
        self.tools = {}
        for directory in [self.tools_dir, self.generated_dir]:
            if os.path.exists(directory):
                for filename in os.listdir(directory):
                    filepath = os.path.join(directory, filename)
                    if os.path.isfile(filepath) and (filename.endswith('.sh') or filename.endswith('.py')):
                        tool_info = self._extract_metadata(filepath, filename)
                        if tool_info:
                            self.tools[tool_info['name']] = tool_info
    
    def _extract_metadata(self, filepath: str, filename: str) -> Optional[Dict]:
        """Extract metadata from script comments"""
        try:
            with open(filepath, 'r') as f:
                content = f.read()
            
            # Default metadata
            name = filename.replace('.sh', '').replace('.py', '')
            description = "No description provided"
            lang = 'bash' if filename.endswith('.sh') else 'python'
            
            # Look for description in comments
            desc_match = re.search(r'description:\s*(.+)', content, re.IGNORECASE)
            if desc_match:
                description = desc_match.group(1).strip()
            
            return {
                'name': name,
                'description': description,
                'path': filepath,
                'language': lang,
                'content': content
            }
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            return None
    
    def get_tool(self, name: str) -> Optional[Dict]:
        return self.tools.get(name)
    
    def list_tools(self) -> List[Dict]:
        return list(self.tools.values())
    
    def register_tool(self, name: str, description: str, filepath: str, language: str):
        self.tools[name] = {
            'name': name,
            'description': description,
            'path': filepath,
            'language': language
        }

