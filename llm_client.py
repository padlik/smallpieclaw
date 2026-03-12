# llm_client.py
import json
import requests
from typing import List, Dict, Optional
from openai import OpenAI

class LLMClient:
    def __init__(self):
        self.provider = Config.LLM_PROVIDER
        self.api_key = Config.LLM_API_KEY
        self.model = Config.LLM_MODEL
        
        if self.provider == "openrouter":
            self.client = OpenAI(
                base_url=Config.LLM_BASE_URL or "https://openrouter.ai/api/v1",
                api_key=self.api_key
            )
        elif self.provider == "openai":
            self.client = OpenAI(api_key=self.api_key)
        else:
            self.client = OpenAI(
                base_url=Config.LLM_BASE_URL,
                api_key=self.api_key
            )
    
    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            if json_mode:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.7,
                    max_tokens=1000
                )
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=1000
                )
            return response.choices[0].message.content
        except Exception as e:
            return json.dumps({"error": str(e), "action": "finish", "args": {}})
    
    def get_embedding(self, text: str) -> List[float]:
        """Get embedding vector for text"""
        try:
            response = self.client.embeddings.create(
                model=Config.EMBEDDING_MODEL,
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            print(f"Embedding error: {e}")
            # Return zero vector as fallback (fallback dimension)
            return [0.0] * 1536

