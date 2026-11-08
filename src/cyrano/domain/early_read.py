from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EARLY_READ_MIN_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class EarlyReadRequest:
    contact_id: int
    due_at: datetime
