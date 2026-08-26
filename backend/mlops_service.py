"""Local-first MLOps service with a bounded text-classification adapter."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import stat
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from operations_core import OperationsCore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u3400-\u9fff]+", text.casefold(), flags=re.UNICODE)[:4096]


class LocalTextClassificationAdapter:
    adapter_id = "local.text_classification.baseline"
    display_name = "本機文字分類基準訓練"

    @staticmethod
    def train(rows: Sequence[Mapping[str, str]], *, seed: int = 42) -> dict[str, Any]:
        labels = sorted({str(row["label"]) for row in rows})
        if len(labels) < 2:
            raise ValueError("資料集至少需要兩個不同標籤。")
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        eval_count = max(1, min(len(shuffled) // 5, len(shuffled) - 2))
        eval_rows, train_rows = shuffled[:eval_count], shuffled[eval_count:]
        label_docs: Counter[str] = Counter()
        label_tokens: dict[str, Counter[str]] = defaultdict(Counter)
        vocabulary: set[str] = set()
        for row in train_rows:
            label = str(row["label"]); words = _tokens(str(row["text"]))
            label_docs[label] += 1; label_tokens[label].update(words); vocabulary.update(words)
        # Keep the serialized model bounded even when a dataset contains many
        # unique identifiers.  Selection is deterministic for reproducibility.
        if len(vocabulary) > 20000:
            global_counts = Counter()
            for counts in label_tokens.values():
                global_counts.update(counts)
            vocabulary = {word for word, _count in global_counts.most_common(20000)}
            for label in labels:
                label_tokens[label] = Counter({word: count for word, count in label_tokens[label].items() if word in vocabulary})
        vocab_size = max(1, len(vocabulary))
        total_docs = max(1, len(train_rows))

        def predict(text: str) -> str:
            words = _tokens(text)
            scores: dict[str, float] = {}
            for label in labels:
                scores[label] = math.log((label_docs[label] + 1) / (total_docs + len(labels)))
                denominator = sum(label_tokens[label].values()) + vocab_size
                for word in words:
                    scores[label] += math.log((label_tokens[label][word] + 1) / denominator)
            return max(scores, key=scores.get)

        correct = sum(1 for row in eval_rows if predict(str(row["text"])) == str(row["label"]))
        model = {
            "format": "workbench.multinomial_naive_bayes.v1",
            "adapter_id": LocalTextClassificationAdapter.adapter_id,
            "labels": labels,
            "vocabulary_size": vocab_size,
            "label_documents": dict(label_docs),
            "label_tokens": {label: dict(counts) for label, counts in label_tokens.items()},
        }
        return {
            "model": model,
            "metrics": {"accuracy": round(correct / len(eval_rows), 6), "train_rows": len(train_rows), "evaluation_rows": len(eval_rows)},
        }


class MLOpsService:
    def __init__(self, *, database_module: Any, operations: OperationsCore, storage_root: Path) -> None:
        self.database = database_module
        self.operations = operations
        self.storage_root = Path(storage_root).resolve()
        self._threads: dict[str, threading.Thread] = {}
        self._starting: set[str] = set()
        self._lock = threading.RLock()
        self.adapter = LocalTextClassificationAdapter()

    def initialize(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        with self.database.get_db_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mlops_datasets (
                    dataset_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mlops_dataset_versions (
                    version_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    row_count INTEGER NOT NULL, label_count INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(dataset_id, revision)
                );
                CREATE TABLE IF NOT EXISTS mlops_experiments (
                    experiment_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                    adapter_id TEXT NOT NULL, dataset_version_id TEXT NOT NULL, parameters_json TEXT NOT NULL,
                    execution_id TEXT, status TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mlops_model_versions (
                    model_version_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, experiment_id TEXT NOT NULL,
                    name TEXT NOT NULL, adapter_id TEXT NOT NULL, artifact_reference_id TEXT NOT NULL,
                    metrics_json TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mlops_datasets_project ON mlops_datasets(project_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mlops_experiments_project ON mlops_experiments(project_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mlops_models_project ON mlops_model_versions(project_id, created_at DESC);
                """
            )
            interrupted = conn.execute(
                "SELECT experiment_id,execution_id FROM mlops_experiments WHERE status IN ('queued','running')"
            ).fetchall()
            conn.execute(
                "UPDATE mlops_experiments SET status='failed',updated_at=? WHERE status IN ('queued','running')",
                (_now(),),
            )
        for row in interrupted:
            if row["execution_id"] and self.operations.get_execution(row["execution_id"]):
                self.operations.update_execution(
                    row["execution_id"], status="failed", progress=100,
                    error_code="TRAINING_INTERRUPTED_BY_RESTART",
                    error_reason="Workbench 重新啟動，本機訓練已安全停止。",
                )
        self.operations.report_health(component_type="training_adapter", component_id=self.adapter.adapter_id,
                                      status="healthy", reason_code="adapter_ready",
                                      detail={"scope": "local_only", "task": "text_classification", "arbitrary_code": False})

    def _safe_file(self, category: str, file_name: str) -> Path:
        folder = (self.storage_root / category).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        path = (folder / file_name).resolve()
        if os.path.commonpath([str(path), str(folder)]) != str(folder) or self._is_reparse(path) or self._is_reparse(folder):
            raise ValueError("不安全的 MLOps 儲存路徑。")
        return path

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except FileNotFoundError:
            return False

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(payload)
        os.replace(temp, path)

    def create_dataset(self, *, project_id: str, name: str, description: str = "") -> dict[str, Any]:
        dataset_id = f"dataset_{uuid.uuid4().hex}"
        now = _now()
        with self.database.get_db_conn() as conn:
            conn.execute("INSERT INTO mlops_datasets VALUES (?,?,?,?,?,?)", (dataset_id, project_id, name.strip()[:160], description.strip()[:1000], now, now))
        return self.get_dataset(dataset_id) or {}

    def add_dataset_version(self, dataset_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        dataset = self.get_dataset(dataset_id)
        if dataset is None:
            raise KeyError(dataset_id)
        if not 4 <= len(rows) <= 10000:
            raise ValueError("資料列數需介於 4 到 10,000 筆。")
        cleaned = []
        total_characters = 0
        for row in rows:
            text, label = str(row.get("text") or "").strip(), str(row.get("label") or "").strip()
            if not text or not label or len(text) > 16000 or len(label) > 120:
                raise ValueError("每筆資料都需要有效的 text 與 label。")
            cleaned.append({"text": text, "label": label})
            total_characters += len(text) + len(label)
            if total_characters > 8 * 1024 * 1024:
                raise ValueError("資料集內容超過 8 MiB 的本機訓練上限。")
        if len({row["label"] for row in cleaned}) < 2:
            raise ValueError("資料集至少需要兩個不同標籤。")
        with self.database.get_db_conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM mlops_dataset_versions WHERE dataset_id=?", (dataset_id,)).fetchone()
            revision = int(row[0])
        version_id = f"dataset_version_{uuid.uuid4().hex}"
        payload = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._safe_file("datasets", f"{version_id}.json")
        self._write_atomic(path, payload)
        with self.database.get_db_conn() as conn:
            conn.execute("INSERT INTO mlops_dataset_versions VALUES (?,?,?,?,?,?,?,?)",
                         (version_id, dataset_id, revision, len(cleaned), len({r['label'] for r in cleaned}), digest, str(path), _now()))
            conn.execute("UPDATE mlops_datasets SET updated_at=? WHERE dataset_id=?", (_now(), dataset_id))
        return self.get_dataset_version(version_id) or {}

    def create_experiment(self, *, project_id: str, name: str, dataset_version_id: str,
                          parameters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        version = self.get_dataset_version(dataset_version_id)
        if version is None or version["project_id"] != project_id:
            raise ValueError("資料集版本不屬於目前專案。")
        parameters = dict(parameters or {})
        seed = int(parameters.get("seed", 42))
        if not 0 <= seed <= 2147483647:
            raise ValueError("seed 超出允許範圍。")
        experiment_id = f"experiment_{uuid.uuid4().hex}"
        now = _now()
        with self.database.get_db_conn() as conn:
            conn.execute("INSERT INTO mlops_experiments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (experiment_id, project_id, name.strip()[:160], self.adapter.adapter_id,
                          dataset_version_id, json.dumps({"seed": seed}), None, "draft", "{}", now, now))
        return self.get_experiment(experiment_id) or {}

    def start_training(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        if experiment is None:
            raise KeyError(experiment_id)
        if experiment["status"] not in {"draft", "failed"}:
            raise ValueError("此實驗目前不能啟動。")
        with self._lock:
            if len(self._threads) + len(self._starting) >= 2:
                raise ValueError("目前已有兩個本機訓練工作，請等待其中一個完成。")
            if experiment_id in self._threads or experiment_id in self._starting:
                raise ValueError("此實驗的本機訓練已在執行。")
            self._starting.add(experiment_id)
        try:
            execution = self.operations.create_execution(kind="mlops.training", owner_type="mlops_experiment",
                owner_id=experiment_id, project_id=experiment["project_id"], status="queued",
                metadata={"adapter_id": self.adapter.adapter_id, "dataset_version_id": experiment["dataset_version_id"]})
            self.operations.record_policy_decision(policy_id="fixed.local_training", subject_type="training_adapter",
                subject_id=self.adapter.adapter_id, action="allow", reason_code="bounded_local_adapter",
                risk_level="low", execution_id=execution["execution_id"], project_id=experiment["project_id"],
                inputs={"network": False, "shell": False, "arbitrary_code": False})
            with self.database.get_db_conn() as conn:
                conn.execute("UPDATE mlops_experiments SET execution_id=?,status='queued',updated_at=? WHERE experiment_id=?",
                             (execution["execution_id"], _now(), experiment_id))
            thread = threading.Thread(target=self._train, args=(experiment_id, execution["execution_id"]), daemon=True, name=f"mlops-{experiment_id[-8:]}")
            with self._lock:
                self._starting.discard(experiment_id)
                self._threads[experiment_id] = thread
            thread.start()
            return {"experiment_id": experiment_id, "execution_id": execution["execution_id"], "status": "queued"}
        except Exception:
            with self._lock:
                self._starting.discard(experiment_id)
            raise

    def _train(self, experiment_id: str, execution_id: str) -> None:
        try:
            experiment = self.get_experiment(experiment_id) or {}
            version = self.get_dataset_version(str(experiment.get("dataset_version_id") or "")) or {}
            self.operations.update_execution(execution_id, status="running", progress=10)
            with self.database.get_db_conn() as conn:
                conn.execute("UPDATE mlops_experiments SET status='running',updated_at=? WHERE experiment_id=?", (_now(), experiment_id))
            path = Path(str(version["storage_path"])).resolve()
            if self._is_reparse(path) or os.path.commonpath([str(path), str(self.storage_root)]) != str(self.storage_root):
                raise ValueError("資料集路徑已離開受控儲存區。")
            rows = json.loads(path.read_text(encoding="utf-8"))
            result = self.adapter.train(rows, seed=int(experiment["parameters"].get("seed", 42)))
            self.operations.update_execution(execution_id, status="running", progress=80)
            model_version_id = f"model_version_{uuid.uuid4().hex}"
            payload = json.dumps(result["model"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            model_path = self._safe_file("models", f"{model_version_id}.json")
            self._write_atomic(model_path, payload)
            digest = hashlib.sha256(payload).hexdigest()
            reference = self.operations.register_artifact(artifact_kind="trained_model", display_name=experiment["name"],
                locator={"scheme": "local_mlops", "model_version_id": model_version_id}, execution_id=execution_id,
                project_id=experiment["project_id"], sha256=digest, size_bytes=len(payload),
                metadata={"adapter_id": self.adapter.adapter_id, "format": result["model"]["format"]})
            with self.database.get_db_conn() as conn:
                conn.execute("INSERT INTO mlops_model_versions VALUES (?,?,?,?,?,?,?,?,?)",
                    (model_version_id, experiment["project_id"], experiment_id, experiment["name"], self.adapter.adapter_id,
                     reference["reference_id"], json.dumps(result["metrics"]), "candidate", _now()))
                conn.execute("UPDATE mlops_experiments SET status='completed',metrics_json=?,updated_at=? WHERE experiment_id=?",
                             (json.dumps(result["metrics"]), _now(), experiment_id))
            self.operations.update_execution(execution_id, status="completed", progress=100, metadata={"metrics": result["metrics"], "model_version_id": model_version_id})
        except Exception as exc:
            with self.database.get_db_conn() as conn:
                conn.execute("UPDATE mlops_experiments SET status='failed',updated_at=? WHERE experiment_id=?", (_now(), experiment_id))
            self.operations.update_execution(execution_id, status="failed", progress=100, error_code="LOCAL_TRAINING_FAILED", error_reason=str(exc))
        finally:
            with self._lock: self._threads.pop(experiment_id, None)

    def get_dataset(self, dataset_id: str) -> Optional[dict[str, Any]]:
        with self.database.get_db_conn() as conn: row = conn.execute("SELECT * FROM mlops_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        return dict(row) if row else None

    def get_dataset_version(self, version_id: str) -> Optional[dict[str, Any]]:
        with self.database.get_db_conn() as conn:
            row = conn.execute("""SELECT v.*,d.project_id,d.name AS dataset_name FROM mlops_dataset_versions v
                                  JOIN mlops_datasets d ON d.dataset_id=v.dataset_id WHERE v.version_id=?""", (version_id,)).fetchone()
        return dict(row) if row else None

    def get_experiment(self, experiment_id: str) -> Optional[dict[str, Any]]:
        with self.database.get_db_conn() as conn: row = conn.execute("SELECT * FROM mlops_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        if not row: return None
        result = dict(row); result["parameters"] = _loads(result.pop("parameters_json"), {}); result["metrics"] = _loads(result.pop("metrics_json"), {}); return result

    def overview(self, project_id: str) -> dict[str, Any]:
        with self.database.get_db_conn() as conn:
            datasets = [dict(row) for row in conn.execute("""SELECT d.*,(SELECT COUNT(*) FROM mlops_dataset_versions v WHERE v.dataset_id=d.dataset_id) AS version_count FROM mlops_datasets d WHERE project_id=? ORDER BY updated_at DESC""", (project_id,)).fetchall()]
            experiments_raw = conn.execute("SELECT * FROM mlops_experiments WHERE project_id=? ORDER BY updated_at DESC", (project_id,)).fetchall()
            models_raw = conn.execute("SELECT * FROM mlops_model_versions WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
            for dataset in datasets:
                latest = conn.execute(
                    """SELECT version_id,dataset_id,revision,row_count,label_count,sha256,created_at
                       FROM mlops_dataset_versions WHERE dataset_id=? ORDER BY revision DESC LIMIT 1""",
                    (dataset["dataset_id"],),
                ).fetchone()
                dataset["latest_version"] = ({**dict(latest), "dataset_name": dataset["name"]} if latest else None)
        experiments = []
        for row in experiments_raw:
            item = dict(row); item["parameters"] = _loads(item.pop("parameters_json"), {}); item["metrics"] = _loads(item.pop("metrics_json"), {}); experiments.append(item)
        models = []
        for row in models_raw:
            item = dict(row); item["metrics"] = _loads(item.pop("metrics_json"), {}); models.append(item)
        return {"project_id": project_id, "adapters": [{"id": self.adapter.adapter_id, "name": self.adapter.display_name, "task": "text_classification", "local_only": True}], "datasets": datasets, "experiments": experiments, "models": models}
