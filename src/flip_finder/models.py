from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Listing:
    source: str
    url: str
    listing_id: str
    title: str
    price_bhd: float | None
    condition: str | None = None
    location: str | None = None
    posted_text: str | None = None
    description: str | None = None
    image_urls: list[str] = field(default_factory=list)
    raw_text: str = ""
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

