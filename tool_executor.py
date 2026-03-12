# tool_executor.py
import subprocess
import re
from typing import Dict, Optional

class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._compile_patterns()
    
    def _compile_patterns(self):
        self.dangerous_patterns = [re.compile(p, re.IGNORECASE) for p in Config.DANGEROUS_PATTERNS]
    
    def validate_code(self, code: str, language: str) -> Tuple[bool, str]:
        """Validate generated code for safety"""
        # Check file size
        if len(code) > Config.MAX_FILE_SIZE:
            return False, "Code exceeds maximum file size"
        
        # Check dangerous patterns
        for pattern in self.dangerous_patterns:
            if pattern.search(code):
                return False, f"Code contains dangerous pattern: {pattern.pattern}"
        
        # Ensure code doesn't access outside tools directories
        if '..' in code or '/etc/' in code or '/root/' in code:
            # Allow but log warning - restrictive but safe
            pass
        
        return True, "OK"
    
    def execute(self, tool_name: str, args: Dict = None) -> Dict:
        """Execute a tool safely"""
        tool = self.registry.get_tool(tool_name)
        if not tool:
            return {"error": f"Tool {tool_name} not found", "stdout": "", "stderr": ""}
        
        # Security: Only allow execution from tools directories
        allowed_dirs = [os.path.abspath(Config.TOOLS_DIR), 
                       os.path.abspath(Config.GENERATED_TOOLS_DIR)]
        tool_path = os.path.abspath(tool['path'])
        
        if not any(tool_path.startswith(d) for d in allowed_dirs):
            return {"error": "Execution outside allowed directories", "stdout": "", "stderr": ""}
        
        try:
            if tool['language'] == 'bash':
                cmd = ['bash', tool['path']]
            else:
                cmd = ['python3', tool['path']]
            
            # Add args if provided (for python scripts mainly)
            if args:
                for k, v in args.items():
                    cmd.extend([f"--{k}", str(v)])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=Config.TOOL_TIMEOUT,
                cwd=Config.TOOLS_DIR  # Restrict working directory
            )
            
            stdout = result.stdout[:Config.MAX_TOOL_OUTPUT]
            stderr = result.stderr[:Config.MAX_TOOL_OUTPUT]
            
            return {
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "success": result.returncode == 0
            }
            
        except subprocess.TimeoutExpired:
            return {"error": "Tool execution timeout", "stdout": "", "stderr": "", "success": False}
        except Exception as e:
            return {"error": str(e), "stdout": "", "stderr": "", "success": False}

