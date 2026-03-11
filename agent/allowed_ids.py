import json
from pathlib import Path
from typing import Set, List, Optional

class AllowedIDs:
    """
    Very light‑weight JSON‑backed store for allowed Telegram user IDs.
    """
    def __init__(self,
                 path: Path,
                 admin_id: Optional[int],
                 pair_secret: Optional[str]):
        self.path = path
        self.admin_id = admin_id
        self.pair_secret = pair_secret
        self._ids: Set[int] = set()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._ids = set(int(i) for i in data.get("allowed_ids", []))
            except Exception:
                self._ids = set()
        else:
            self._save()

    def _save(self):
        data = {"allowed_ids": sorted(list(self._ids))}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self._ids

    def add(self, user_id: int):
        self._ids.add(user_id)
        self._save()

    def remove(self, user_id: int):
        self._ids.discard(user_id)
        self._save()

    def list(self) -> List[int]:
        return sorted(list(self._ids))

