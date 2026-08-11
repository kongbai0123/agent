from typing import Optional

from pydantic import BaseModel


class AttachmentRequest(BaseModel):
    data: str
    filename: Optional[str] = None
    mime_type: str = "image/png"
    session_id: Optional[str] = None
