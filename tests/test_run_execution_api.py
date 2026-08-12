from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402
from chat import runtime as chat_runtime  # noqa: E402
from hermes_approval_store import (  # noqa: E402
    PersistentHermesApprovalStore,
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _scope(*, project: bool = True):
    project_id = _id("project") if project else None
    if project_id:
        app_module.database.create_project(project_id, project_id, str(ROOT))
    session_id = _id("session")
    app_module.database.create_session(session_id, project_id=project_id)
    return project_id, session_id


def _failed_run(
    *,
    project_id: str | None,
    session_id: str,
    recoverable: bool = True,
):
    run_id = _id("run")
    turn_id = _id("turn")
    message_id = app_module.database.add_message(
        session_id, "user", "retry the original input", turn_id=turn_id
    )
    app_module.database.upsert_run(
        run_id,
        session_id,
        turn_id,
        "route-test-model",
        "chat",
        "failed",
        project_id=project_id,
        tasks=[
            {
                "id": "generate",
                "label": "Provider failed with sk-abcdefghijklmnop1234",
                "status": "failed",
                "command": "never expose this command",
                "args": {"path": "D:/private"},
            }
        ],
        metrics={
            "runtime": "basic_chat",
            "error": {
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "The provider is temporarily unavailable.",
                "recoverable": recoverable,
                "detail": "private upstream detail",
            },
        },
        input_manifest={
            "version": 1,
            "reproducible": True,
            "user_message_id": message_id,
            "user_message": "retry the original input",
            "prompt_sha256": hashlib.sha256(
                b"retry the original input"
            ).hexdigest(),
            "history_snapshot": [],
            "project_id": project_id,
            "attachment_ids": [],
            "temporary_context_id": None,
            "temporary_context": "private context",
            "inline_image_count": 0,
            "project_skill_context": "private skill context",
            "project_skill_provenance": [],
            "runtime_route": "basic",
        },
    )
    app_module.database.append_run_event(
        run_id,
        "error",
        {
            "code": "UPSTREAM_UNAVAILABLE",
            "message": "The provider is temporarily unavailable.",
            "recoverable": recoverable,
        },
    )
    return run_id, turn_id


class _Response:
    status_code = 200
    text = ""

    def iter_lines(self):
        yield json.dumps(
            {"message": {"content": "retried answer"}, "done": False}
        ).encode()
        yield json.dumps({"message": {}, "done": True}).encode()

    def close(self):
        return None


def test_latest_and_execution_snapshots_are_project_bound_and_redacted():
    project_id, session_id = _scope()
    run_id, _turn_id = _failed_run(
        project_id=project_id, session_id=session_id
    )

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        latest = client.get(f"/api/sessions/{session_id}/runs?limit=1")
        execution = client.get(f"/api/runs/{run_id}/execution")
        public_run = client.get(f"/api/runs/{run_id}")

    assert latest.status_code == 200
    assert latest.json() == {
        "success": True,
        "session_id": session_id,
        "runs": [
            {
                "run_id": run_id,
                "session_id": session_id,
                "project_id": project_id,
                "status": "failed",
                "model": "route-test-model",
                "mode": "chat",
                "created_at": latest.json()["runs"][0]["created_at"],
                "completed_at": None,
                "retry_of_run_id": None,
            }
        ],
    }
    body = execution.json()
    assert body["success"] is True
    assert body["project_id"] == project_id
    assert body["error"] == {
        "code": "UPSTREAM_UNAVAILABLE",
        "message": "The provider is temporarily unavailable.",
        "recoverable": True,
    }
    assert body["retry"] == {"allowed": True, "reason": None}
    assert body["tasks"] == [
        {"id": "generate", "label": "Provider failed with [redacted]", "status": "failed"}
    ]
    assert body["revision"] == 1
    serialized = json.dumps(public_run.json())
    assert "private context" not in serialized
    assert "private skill context" not in serialized
    assert "private upstream detail" not in serialized
    assert "storage_path" not in serialized


def test_whole_run_retry_replays_persisted_input_into_a_new_run(monkeypatch):
    project_id, session_id = _scope()
    source_run_id, _turn_id = _failed_run(
        project_id=project_id, session_id=session_id
    )
    new_run_id = _id("run")
    real_settings = app_module.load_settings()
    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {**real_settings, "hermes_enabled": False},
    )
    monkeypatch.setattr(
        app_module, "loaded_models_snapshot", lambda *_args, **_kwargs: set()
    )
    captured = {}

    def provider(_settings, payload, **_kwargs):
        captured["messages"] = payload["messages"]
        return _Response()

    monkeypatch.setattr(chat_runtime, "provider_post_chat", provider)
    app_module.database.add_message(
        session_id, "user", "later question", turn_id=_id("turn")
    )
    later_user = app_module.database.get_messages_by_session(session_id)[-1]
    app_module.database.add_message(
        session_id,
        "assistant",
        "later answer",
        turn_id=later_user["turn_id"],
        parent_message_id=later_user["id"],
    )
    users_before = [
        item
        for item in app_module.database.get_messages_by_session(session_id)
        if item["role"] == "user"
    ]

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "run_id": new_run_id,
                "retry_of_run_id": source_run_id,
                "message": "untrusted replacement is ignored",
            },
        )

    assert response.status_code == 200, response.text
    run = app_module.database.get_run(new_run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["project_id"] == project_id
    assert run["retry_of_run_id"] == source_run_id
    user_messages = [
        item
        for item in app_module.database.get_messages_by_session(session_id)
        if item["role"] == "user"
    ]
    assert user_messages == users_before
    assert [item["role"] for item in captured["messages"]] == ["system", "user"]
    assert captured["messages"][-1]["content"] == "retry the original input"
    assert "later question" not in json.dumps(captured["messages"])
    assistant = app_module.database.get_messages_by_session(session_id)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["parent_message_id"] == users_before[0]["id"]


def test_active_run_blocks_session_project_move():
    project_id, session_id = _scope()
    other_project, _other_session = _scope()
    control = app_module.register_chat_run(
        _id("run"), session_id, _id("turn"), "route-test-model", "chat"
    )
    try:
        with TestClient(app_module.app) as client:
            assert client.get("/").status_code == 200
            response = client.patch(
                f"/api/sessions/{session_id}",
                json={"project_id": other_project},
            )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "SESSION_RUN_ACTIVE"
        assert app_module.database.get_session(session_id)["project_id"] == project_id
    finally:
        app_module.release_chat_run(control.run_id, control)


def test_retry_rejects_nonrecoverable_and_cross_session_requests():
    project_id, session_id = _scope()
    source_run_id, _turn_id = _failed_run(
        project_id=project_id, session_id=session_id, recoverable=False
    )
    _other_project, other_session = _scope()

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.post(
            "/api/chat",
            json={
                "session_id": other_session,
                "run_id": _id("run"),
                "retry_of_run_id": source_run_id,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_RETRY_NOT_ALLOWED"


def test_attachment_loading_fails_closed_across_session_scope(tmp_path, monkeypatch):
    project_id, owner_session = _scope()
    other_session = _id("session")
    app_module.database.create_session(other_session, project_id=project_id)
    attachment_path = tmp_path / "image.png"
    attachment_path.write_bytes(b"not-an-image")
    attachment_id = _id("attachment")
    app_module.database.save_attachment(
        attachment_id,
        owner_session,
        "image.png",
        "image/png",
        str(attachment_path),
        attachment_path.stat().st_size,
        project_id=project_id,
    )
    monkeypatch.setattr(
        chat_runtime,
        "provider_post_chat",
        lambda *_args, **_kwargs: pytest.fail("provider must not be called"),
    )

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        outside_managed_root = client.post(
            "/api/chat",
            json={
                "session_id": owner_session,
                "message": "use the attachment",
                "attachment_ids": [attachment_id],
            },
        )
        cross_session = client.post(
            "/api/chat",
            json={
                "session_id": other_session,
                "message": "use the attachment",
                "attachment_ids": [attachment_id],
            },
        )

    assert outside_managed_root.status_code == 409
    assert outside_managed_root.json()["detail"]["code"] == "ATTACHMENT_SCOPE_MISMATCH"
    assert cross_session.status_code == 409
    assert cross_session.json()["detail"]["code"] == "ATTACHMENT_SCOPE_MISMATCH"


def test_attachment_authorization_rejects_unresolved_file_and_root_links(
    tmp_path, monkeypatch
):
    actual_root = tmp_path / "actual-attachments"
    actual_root.mkdir()
    actual_file = actual_root / "image.png"
    actual_file.write_bytes(b"image")

    file_link = actual_root / "linked-image.png"
    try:
        file_link.symlink_to(actual_file)
    except (NotImplementedError, OSError):
        pytest.skip("Symlink creation is unavailable on this platform.")

    monkeypatch.setattr(
        app_module, "project_attachments_dir", lambda *_args: actual_root
    )
    scoped = {
        "session_id": "session-one",
        "project_id": "project-one",
        "storage_path": str(file_link),
    }
    assert app_module._authorized_attachment_path(
        scoped,
        session_id="session-one",
        project_id="project-one",
    ) is None

    linked_root = tmp_path / "linked-attachments"
    try:
        linked_root.symlink_to(actual_root, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Directory symlink creation is unavailable on this platform.")
    monkeypatch.setattr(
        app_module, "project_attachments_dir", lambda *_args: linked_root
    )
    scoped["storage_path"] = str(linked_root / actual_file.name)
    assert app_module._authorized_attachment_path(
        scoped,
        session_id="session-one",
        project_id="project-one",
    ) is None


def test_execution_hydrates_terminal_hermes_approval_state():
    project_id, session_id = _scope()
    run_id, _turn_id = _failed_run(
        project_id=project_id, session_id=session_id
    )
    store = PersistentHermesApprovalStore()
    approval = store.create(
        approval_id=_id("approval"),
        event_fingerprint=uuid.uuid4().hex,
        workbench_run_id=run_id,
        workbench_session_id=session_id,
        project_id=project_id,
        capability="hermes.tool",
        resource="active_project",
        summary="Use one approved capability.",
    )
    app_module.database.append_run_event(
        run_id,
        "approval_required",
        {
            "approval_id": approval.approval_id,
            "capability": approval.capability,
            "message": approval.summary,
            "run_id": run_id,
            "risk": "high",
            "status": "pending",
            "choices": ["once", "deny"],
        },
    )
    store.claim(approval.approval_id, choice="deny", rationale="Not now.")
    store.finish(approval.approval_id, status="denied")

    with TestClient(app_module.app) as client:
        assert client.get("/").status_code == 200
        response = client.get(f"/api/runs/{run_id}/execution")

    assert response.status_code == 200
    approval_event = next(
        item
        for item in response.json()["events"]
        if item["event"] == "approval_required"
    )
    assert approval_event["payload"]["status"] == "denied"
    assert approval_event["payload"]["rationale"] == "Not now."


def test_public_event_contract_rejects_absolute_paths_and_raw_commands():
    project_id, session_id = _scope()
    run_id, _turn_id = _failed_run(
        project_id=project_id, session_id=session_id
    )
    with pytest.raises(ValueError):
        app_module.database.append_run_event(
            run_id,
            "file_written",
            {"relative_path": "D:/private/file.txt", "status": "completed"},
        )
    record = app_module.database.append_run_event(
        run_id,
        "test_result",
        {
            "name": "focused tests",
            "status": "passed",
            "passed": 12,
            "failed": 0,
            "command": "pytest --secret-token",
        },
    )
    assert "command" not in record["payload"]
