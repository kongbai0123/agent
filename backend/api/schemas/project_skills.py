from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _ProjectSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectSkillRequest(_ProjectSkillRequest):
    slug: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    instructions: str = Field(min_length=1, max_length=131072)
    enabled: bool = True
    references: dict[str, str] = Field(default_factory=dict)


class UpdateProjectSkillRequest(_ProjectSkillRequest):
    expected_sha256: str = Field(pattern=SHA256_PATTERN)
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    version: Optional[str] = Field(default=None, min_length=1, max_length=32)
    instructions: Optional[str] = Field(default=None, min_length=1, max_length=131072)
    references: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None

    @field_validator("name", "description", "version", "instructions", "references", "enabled", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("Updated fields cannot be null.")
        return value


class ProjectSkillStateRequest(_ProjectSkillRequest):
    expected_sha256: str = Field(pattern=SHA256_PATTERN)
    enabled: bool


class DeleteProjectSkillRequest(_ProjectSkillRequest):
    expected_sha256: str = Field(pattern=SHA256_PATTERN)


class SessionProjectSkillStateRequest(_ProjectSkillRequest):
    mode: Literal["enabled", "disabled", "inherit"]
    scope: Literal["session", "turn"] = "session"
    expected_sha256: str = Field(pattern=SHA256_PATTERN)
