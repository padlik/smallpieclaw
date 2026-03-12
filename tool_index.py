# tool_index.py
import json
import numpy as np
from typing import List, Dict, Tuple
from llm_client import LLMClient

class ToolIndex:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self.index_file = Config.TOOL_INDEX_FILE
        self.llm = LLMClient()
        self.index: Dict[str, Dict] = {}
        self.load()
    
    def load(self):
        if os.path.exists(self.index_file):
            with open(self.index_file, 'r') as f:
                self.index = json.load(f)
    
    def save(self):
        with open(self.index_file, 'w') as f:
            json.dump(self.index, f, indent=2)
    
    def rebuild_index(self):
        """Generate embeddings for all tools"""
        print("Rebuilding tool index...")
        self.index = {}
        
        for tool in self.registry.list_tools():
            embedding = self.llm.get_embedding(tool['description'])
            self.index[tool['name']] = {
                'description': tool['description'],
                'embedding': embedding
            }
        
        self.save()
        print(f"Indexed {len(self.index)} tools")
    
    def search(self, query: str, top_k: int = 3) -> List[str]:
        """Find most relevant tools using cosine similarity"""
        if not self.index:
            self.rebuild_index()
        
        query_embedding = self.llm.get_embedding(query)
        
        similarities = []
        for name, data in self.index.items():
            tool_emb = np.array(data['embedding'])
            query_emb = np.array(query_embedding)
            
            # Cosine similarity
            similarity = np.dot(query_emb, tool_emb) / (
                np.linalg.norm(query_emb) * np.linalg.norm(tool_emb)
            )
            similarities.append((name, float(similarity)))
        
        # Sort by similarity descending
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        return [name for name, _ in similarities[:top_k]]

