from typing import List, Literal, Optional

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    session_id: Optional[str] = None
    title: Optional[str] = "New chat"
    mode: Literal["chat", "rag", "code", "mixed"] = "chat"
    model: Optional[str] = None
    project_id: Optional[str] = None


class PatchSessionRequest(BaseModel):
    title: Optional[str] = None
    mode: Optional[Literal["chat", "rag", "code", "mixed"]] = None
    model: Optional[str] = None
    project_id: Optional[str] = None
    status: Optional[
        Literal["running", "waiting", "failed", "generating", "completed"]
    ] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None


class ReorderSessionsRequest(BaseModel):
    session_ids: List[str]
    project_id: Optional[str] = None
