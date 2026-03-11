import json
import math
import requests
from pathlib import Path
from typing import List, Dict, Optional

import config

config_instance = config.config

INDEX_PATH = Path("tool_index.json")

class ToolIndex:
    def __init__(self, index_path: Path = INDEX_PATH):
        self.index_path = index_path
        self.embedding_model = config_instance.EMBEDDINGS_MODEL
        self.data = {"version": 1, "embedding_model": self.embedding_model, "tools": []}
        self._load()
        
    def _load(self):
        if self.index_path.exists():
            try:
                self.data = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        if "embedding_model" not in self.data:
            self.data["embedding_model"] = self.embedding_model
        if "tools" not in self.data:
            self.data["tools"] = []
    
    def save(self):
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)
    
    def _embeddings_api(self, texts: List[str]) -> List[List[float]]:
        """
        Dedicated embeddings API with separate configuration
        """
        url = f"{config_instance.EMBEDDINGS_BASE_URL.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {config_instance.EMBEDDINGS_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.embedding_model,
            "input": texts
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data.get("data", [])]
        except Exception as e:
            print(f"Embeddings API error: {e}")
            # Fallback to reasoning LLM if embeddings API fails
            return self._fallback_embeddings(texts)
    
    def _fallback_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Fallback embedding generation using reasoning LLM if dedicated embeddings API fails
        """
        url = f"{config_instance.LLM_BASE_URL.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {config_instance.LLM_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.embedding_model,
            "input": texts
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        return [item["embedding"] for item in data.get("data", [])]
    
    def cosine_sim(self, a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na * nb > 0 else 0.0
    
    def build_index(self, tools: List[Dict]):
        texts = [f"{t['name']}: {t['description']}" for t in tools]
        vectors = self._embeddings_api(texts)
        self.data["tools"] = [
            {"tool": tools[i]["name"], "description": tools[i]["description"], "embedding": vectors[i]}
            for i in range(len(tools))
        ]
        self.data["embedding_model"] = self.embedding_model
        self.save()
    
    def upsert_tool(self, tool: Dict):
        texts = [f"{tool['name']}: {tool['description']}"]
        vectors = self._embeddings_api(texts)
        emb = vectors[0]
        existing = next((i for i, t in enumerate(self.data["tools"]) if t["tool"] == tool["name"]), None)
        entry = {"tool": tool["name"], "description": tool["description"], "embedding": emb}
        if existing is not None:
            self.data["tools"][existing] = entry
        else:
            self.data["tools"].append(entry)
        self.save()
    
    def search(self, query: str, top_k: int = 3) -> List[Dict]:
        if not self.data["tools"]:
            return []
        qvecs = self._embeddings_api([query])
        qv = qvecs[0]
        sims = [
            (t["tool"], self.cosine_sim(qv, t["embedding"]), t["description"])
            for t in self.data["tools"]
        ]
        sims.sort(key=lambda x: x[1], reverse=True)
        return [{"name": s[0], "score": s[1], "description": s[2]} for s in sims[:top_k]]

index = ToolIndex()

