"""Local MLOps workspace APIs."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mlops_service import MLOpsService


class DatasetCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)


class DatasetRow(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    label: str = Field(min_length=1, max_length=120)


class DatasetVersionCreate(BaseModel):
    rows: List[DatasetRow] = Field(min_length=4, max_length=10000)


class ExperimentCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    dataset_version_id: str = Field(min_length=1, max_length=160)
    parameters: Dict[str, Any] = Field(default_factory=dict)


def build_mlops_router(*, service: MLOpsService, require_local: Callable[[Request], None],
                       require_project: Callable[[str], Any]) -> APIRouter:
    router = APIRouter(tags=["mlops"])

    def project(project_id: str) -> str:
        if require_project(project_id) is None:
            raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND", "message": "找不到指定專案。"})
        return project_id

    @router.get("/api/mlops/overview")
    def overview(request: Request, project_id: str):
        require_local(request); return service.overview(project(project_id))

    @router.post("/api/mlops/datasets")
    def create_dataset(payload: DatasetCreate, request: Request):
        require_local(request); return service.create_dataset(project_id=project(payload.project_id), name=payload.name, description=payload.description)

    @router.post("/api/mlops/datasets/{dataset_id}/versions")
    def add_version(dataset_id: str, payload: DatasetVersionCreate, request: Request):
        require_local(request)
        try: return service.add_dataset_version(dataset_id, [row.model_dump() for row in payload.rows])
        except KeyError: raise HTTPException(status_code=404, detail={"code": "DATASET_NOT_FOUND", "message": "找不到資料集。"})
        except ValueError as exc: raise HTTPException(status_code=422, detail={"code": "DATASET_INVALID", "message": str(exc)})

    @router.post("/api/mlops/experiments")
    def create_experiment(payload: ExperimentCreate, request: Request):
        require_local(request)
        try: return service.create_experiment(project_id=project(payload.project_id), name=payload.name, dataset_version_id=payload.dataset_version_id, parameters=payload.parameters)
        except ValueError as exc: raise HTTPException(status_code=422, detail={"code": "EXPERIMENT_INVALID", "message": str(exc)})

    @router.post("/api/mlops/experiments/{experiment_id}/train")
    def train(experiment_id: str, request: Request):
        require_local(request)
        try: return service.start_training(experiment_id)
        except KeyError: raise HTTPException(status_code=404, detail={"code": "EXPERIMENT_NOT_FOUND", "message": "找不到實驗。"})
        except ValueError as exc: raise HTTPException(status_code=409, detail={"code": "EXPERIMENT_NOT_READY", "message": str(exc)})

    return router
