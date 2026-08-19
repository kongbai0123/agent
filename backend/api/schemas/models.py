from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


OLLAMA_MODEL_REFERENCE_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9._/-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)


class ModelInstallRequest(BaseModel):
    model: str = Field(
        min_length=1,
        max_length=200,
        pattern=OLLAMA_MODEL_REFERENCE_PATTERN,
    )

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model_reference(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        repository = normalized.split(":", 1)[0]
        if "//" in repository or any(
            segment in {"", ".", ".."} for segment in repository.split("/")
        ):
            raise ValueError("model must be a safe Ollama model reference")
        return normalized


class BenchmarkRequest(BaseModel):
    model: str


class SelectModelRequest(BaseModel):
    model: str
    scope: Literal["turn", "session", "global"] = "global"
    session_id: Optional[str] = None
