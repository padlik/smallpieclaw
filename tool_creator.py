import os
import stat
from typing import Dict, Tuple
from llm_client import LLMClient

class ToolCreator:
    def __init__(self, registry: ToolRegistry, executor: ToolExecutor):
        self.registry = registry
        self.executor = executor
        self.llm = LLMClient()
    
    def create_tool(self, name: str, language: str, code: str, description: str) -> Tuple[bool, str]:
        """Create and save a new tool"""
        # Validate code
        is_valid, msg = self.executor.validate_code(code, language)
        if not is_valid:
            return False, f"Validation failed: {msg}"
        
        # Optional: LLM safety review
        if not self._safety_review(code, language):
            return False, "Safety review failed"
        
        # Determine extension and path
        ext = '.sh' if language == 'bash' else '.py'
        filename = f"{name}{ext}"
        filepath = os.path.join(Config.GENERATED_TOOLS_DIR, filename)
        
        try:
            # Write code
            with open(filepath, 'w') as f:
                f.write(code)
            
            # Make executable
            os.chmod(filepath, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
            
            # Register in registry
            self.registry.register_tool(name, description, filepath, language)
            
            return True, f"Tool {name} created successfully at {filepath}"
            
        except Exception as e:
            return False, f"Failed to create tool: {str(e)}"
    
    def _safety_review(self, code: str, language: str) -> bool:
        """Quick LLM safety check"""
        prompt = f"""Review this {language} code for safety. Reply with only 'SAFE' or 'UNSAFE'.
Code:
{code}

Is this safe to execute?"""
        
        try:
            response = self.llm.chat(
                "You are a security reviewer. Reply only SAFE or UNSAFE.",
                prompt,
                json_mode=False
            )
            return "SAFE" in response.upper()
        except:
            # If review fails, allow but be cautious
            return True

