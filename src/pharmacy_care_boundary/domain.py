"""药店健康陪伴边界使用的基础领域记录。"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or datetime.now(timezone.utc).isoformat()
        return replace(self, created_at=value)
