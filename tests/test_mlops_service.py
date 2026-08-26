from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import database
from mlops_service import MLOpsService
from operations_core import OperationsCore


def test_local_training_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "mlops.db"))
    database.init_db()
    operations = OperationsCore(database_module=database)
    operations.initialize()
    service = MLOpsService(database_module=database, operations=operations, storage_root=tmp_path / "storage")
    service.initialize()

    dataset = service.create_dataset(project_id="project_1", name="意圖分類")
    version = service.add_dataset_version(dataset["dataset_id"], [
        {"text": "我要退款", "label": "退款"},
        {"text": "取消訂單", "label": "退款"},
        {"text": "包裹到哪裡", "label": "物流"},
        {"text": "查詢配送狀態", "label": "物流"},
        {"text": "退回商品", "label": "退款"},
        {"text": "尚未收到貨", "label": "物流"},
    ])
    experiment = service.create_experiment(
        project_id="project_1", name="本機基準", dataset_version_id=version["version_id"],
    )
    started = service.start_training(experiment["experiment_id"])
    for _ in range(100):
        execution = operations.get_execution(started["execution_id"])
        if execution and execution["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert execution["status"] == "completed", execution
    overview = service.overview("project_1")
    assert overview["experiments"][0]["status"] == "completed"
    assert overview["models"][0]["adapter_id"] == "local.text_classification.baseline"
    artifacts = operations.list_artifacts(execution_id=started["execution_id"])
    assert artifacts[0]["locator"]["scheme"] == "local_mlops"
    decisions = operations.list_policy_decisions(project_id="project_1")
    assert decisions[0]["action"] == "allow"


def test_training_dataset_rejects_single_label(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "mlops-invalid.db"))
    database.init_db()
    operations = OperationsCore(database_module=database); operations.initialize()
    service = MLOpsService(database_module=database, operations=operations, storage_root=tmp_path / "storage")
    service.initialize()
    dataset = service.create_dataset(project_id="project_1", name="無效資料")
    try:
        service.add_dataset_version(dataset["dataset_id"], [{"text": str(index), "label": "same"} for index in range(4)])
    except ValueError as exc:
        assert "兩個不同標籤" in str(exc)
    else:
        raise AssertionError("single-label dataset must be rejected")
