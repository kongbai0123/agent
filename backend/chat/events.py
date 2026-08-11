from __future__ import annotations

import json
from typing import Any, Dict


def encode_sse(event: str, data: Dict[str, Any]) -> str:
    """Encode one named Server-Sent Event while preserving Unicode."""

    safe_event = str(event).replace("\r", "").replace("\n", "")
    return f"event: {safe_event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
