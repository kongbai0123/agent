from typing import Literal, Optional

from pydantic import BaseModel


class ModelInstallRequest(BaseModel):
    model: str


class BenchmarkRequest(BaseModel):
    model: str


class SelectModelRequest(BaseModel):
    model: str
    scope: Literal["turn", "session", "global"] = "global"
    session_id: Optional[str] = None
