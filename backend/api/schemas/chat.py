from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: Optional[str] = None
    model: Optional[str] = None
    mode: Literal["chat"] = "chat"
    messages: List[ChatMessage] = Field(default_factory=list)
    use_rag: bool = False
    attachment_ids: List[str] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)
    temporary_context_id: Optional[str] = None
    temporary_context: Optional[str] = ""
    run_id: Optional[str] = Field(
        default=None,
        pattern=r"^run_[A-Za-z0-9_-]{8,80}$",
    )
    retry_of_run_id: Optional[str] = Field(
        default=None,
        pattern=r"^run_[A-Za-z0-9_-]{8,80}$",
    )
