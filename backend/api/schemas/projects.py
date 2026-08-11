from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    root_path: Optional[str] = None
    root_kind: Literal["managed", "linked"] = "managed"
    permission_mode: Literal[
        "read_only",
        "confirm_write",
        "workspace_write",
    ] = "read_only"


class PatchProjectRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    root_path: Optional[str] = None
    expanded: Optional[bool] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    permission_mode: Optional[
        Literal["read_only", "confirm_write", "workspace_write"]
    ] = None


class ValidateProjectPathRequest(BaseModel):
    root_path: str = Field(min_length=1)
    require_existing: bool = True


class RelinkProjectRequest(BaseModel):
    root_path: str = Field(min_length=1)


class BrowseDirectoriesRequest(BaseModel):
    path: Optional[str] = None


class ReorderProjectsRequest(BaseModel):
    project_ids: List[str]
