"""Authoritative adapter that synchronizes integration policies to live gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


GateSetter = Callable[[str, str, list[dict[str, Any]]], Any]


class IntegrationPolicyApplyError(RuntimeError):
    """Raised only after a partial apply has been compensated as far as possible."""

    def __init__(self, message: str, *, compensation_incomplete: bool = False) -> None:
        super().__init__(message)
        self.compensation_incomplete = bool(compensation_incomplete)


@dataclass
class IntegrationPolicyApplyReceipt:
    _undos: list[Callable[[], Any]] = field(default_factory=list)
    _rolled_back: bool = False

    def add(self, undo: Optional[Callable[[], Any]]) -> None:
        if callable(undo):
            self._undos.append(undo)

    def rollback(self) -> None:
        if self._rolled_back:
            return
        errors: list[BaseException] = []
        failed: list[Callable[[], Any]] = []
        for undo in reversed(self._undos):
            try:
                undo()
            except BaseException as exc:  # best-effort compensation; caller remains fail-closed
                errors.append(exc)
                failed.append(undo)
        if errors:
            # Keep only failed compensation actions retryable. Successful
            # actions are not repeated, while a later reconciliation still has
            # a chance to close the remaining authority.
            self._undos = list(reversed(failed))
            raise IntegrationPolicyApplyError("整合權限方案的回復作業未完整完成。")
        self._rolled_back = True
        self._undos.clear()


class AuthoritativeIntegrationPolicyApplier:
    """Apply one Project policy without copying credentials or executable config.

    Extension permission levels and Connector Project bindings remain owned by
    their existing services.  Optional callbacks let the n8n, MCP and external
    API runtime gates update their own authoritative state.  Every mutation is
    paired with a compensation action.
    """

    _EXTENSION_IDS = {
        "gmail": "connector.gmail",
        "n8n": "builtin.n8n",
        "github": "connector.github",
        "notion": "connector.notion",
    }

    def __init__(
        self,
        *,
        extension_registry: Any = None,
        connector_service: Any = None,
        connector_gate_setter: Optional[GateSetter] = None,
        n8n_gate_setter: Optional[GateSetter] = None,
        mcp_gate_setter: Optional[GateSetter] = None,
        external_api_gate_setter: Optional[GateSetter] = None,
    ) -> None:
        self.extension_registry = extension_registry
        self.connector_service = connector_service
        self.connector_gate_setter = connector_gate_setter
        self.n8n_gate_setter = n8n_gate_setter
        self.mcp_gate_setter = mcp_gate_setter
        self.external_api_gate_setter = external_api_gate_setter

    @staticmethod
    def _mode_for(integration_id: str, policy: Mapping[str, Any]) -> str:
        selected = {
            str(item.get("integration_id") or "")
            for item in policy.get("grants") or []
            if isinstance(item, Mapping)
        }
        return str(policy.get("permission_mode") or "blocked") if integration_id in selected else "blocked"

    @staticmethod
    def _callback_undo(result: Any) -> Optional[Callable[[], Any]]:
        if callable(result):
            return result
        rollback = getattr(result, "rollback", None)
        return rollback if callable(rollback) else None

    def _extension_items(self, project_id: str) -> list[Mapping[str, Any]]:
        if self.extension_registry is None:
            return []
        catalog = self.extension_registry.catalog(project_id)
        return [item for item in catalog.get("extensions") or [] if isinstance(item, Mapping)]

    def _apply_extensions(
        self,
        project_id: str,
        policy: Mapping[str, Any],
        receipt: IntegrationPolicyApplyReceipt,
    ) -> None:
        if self.extension_registry is None:
            return
        items = self._extension_items(project_id)
        by_id = {str(item.get("id") or ""): item for item in items}
        target_modes: dict[str, str] = {}
        for integration_id, extension_id in self._EXTENSION_IDS.items():
            requested = self._mode_for(integration_id, policy)
            current = target_modes.get(extension_id)
            # Gmail and n8n share the same authoritative extension.  Either
            # explicit grant may open it; blocked never overrides a selected peer.
            if current is None or (current == "blocked" and requested != "blocked"):
                target_modes[extension_id] = requested
        for item in items:
            entrypoint = item.get("entrypoint") if isinstance(item.get("entrypoint"), Mapping) else {}
            if str(entrypoint.get("type") or "") == "mcp_settings":
                extension_id = str(item.get("id") or "")
                target_modes[extension_id] = "blocked"
        for grant in policy.get("grants") or []:
            if not isinstance(grant, Mapping) or grant.get("integration_id") != "mcp":
                continue
            extension_id = str(grant.get("connection_id") or "")
            if extension_id in by_id:
                target_modes[extension_id] = str(policy.get("permission_mode") or "blocked")

        for extension_id, desired in target_modes.items():
            item = by_id.get(extension_id)
            if item is None:
                continue
            previous_mode = str(item.get("project_override") or "inherit")
            digest = str(item.get("manifest_sha256") or "")
            selected = desired != "blocked"
            can_enable = bool(
                item.get("installed")
                and item.get("trusted")
                and item.get("global_enabled")
                and digest
            )
            if selected and not can_enable:
                raise IntegrationPolicyApplyError(
                    "選取的擴充尚未完成安裝、信任或全域啟用。"
                )
            desired_mode = (
                "enabled"
                if selected and can_enable
                else "disabled"
                if not selected
                else previous_mode
            )
            if desired_mode != previous_mode:
                updated_state = self.extension_registry.set_project_mode(
                    extension_id,
                    project_id,
                    desired_mode,
                    expected_sha256=digest if desired_mode == "enabled" else None,
                    actor="integration_policy",
                )
                next_mode = (
                    str(updated_state.get("project_override") or desired_mode)
                    if isinstance(updated_state, Mapping)
                    else desired_mode
                )

                def undo_extension_mode(
                    extension: str = extension_id,
                    mode: str = previous_mode,
                    approved_digest: str = digest,
                    current_mode: str = next_mode,
                ) -> Any:
                    if current_mode == mode:
                        return None
                    return self.extension_registry.set_project_mode(
                        extension,
                        project_id,
                        mode,
                        expected_sha256=approved_digest if mode == "enabled" else None,
                        actor="integration_policy_rollback",
                    )

                receipt.add(undo_extension_mode)
            current = item.get("project_permission")
            if not isinstance(current, Mapping):
                continue
            previous_level = str(current.get("level") or "restricted")
            previous_revision = int(current.get("revision") or 0)
            if previous_level == desired:
                continue
            updated = self.extension_registry.set_project_permission(
                extension_id,
                project_id,
                desired,
                expected_revision=previous_revision,
                actor="integration_policy",
            )
            next_permission = updated.get("project_permission") if isinstance(updated, Mapping) else None
            next_revision = int(next_permission.get("revision") or previous_revision + 1) if isinstance(next_permission, Mapping) else previous_revision + 1

            def undo_extension(
                extension: str = extension_id,
                level: str = previous_level,
                revision: int = next_revision,
            ) -> Any:
                return self.extension_registry.set_project_permission(
                    extension,
                    project_id,
                    level,
                    expected_revision=revision,
                    actor="integration_policy_rollback",
                )

            receipt.add(undo_extension)

    def _apply_connectors(
        self,
        project_id: str,
        policy: Mapping[str, Any],
        receipt: IntegrationPolicyApplyReceipt,
    ) -> None:
        if self.connector_service is None:
            return
        selected: set[str] = set()
        if str(policy.get("permission_mode") or "blocked") != "blocked":
            selected = {
                str(item.get("connection_id") or "")
                for item in policy.get("grants") or []
                if isinstance(item, Mapping)
                and item.get("integration_id") in {"github", "notion", "gmail"}
                and item.get("connection_id")
            }
        connections = self.connector_service.list_connections(project_id=project_id)
        for connection in connections or []:
            if not isinstance(connection, Mapping):
                continue
            binding = connection.get("binding")
            if not isinstance(binding, Mapping):
                continue
            connection_id = str(connection.get("connection_id") or "")
            previous_enabled = bool(binding.get("enabled"))
            previous_mode = str(binding.get("mode") or "read_write")
            desired_enabled = connection_id in selected
            if previous_enabled == desired_enabled:
                continue
            self.connector_service.put_project_binding(
                project_id=project_id,
                connection_id=connection_id,
                enabled=desired_enabled,
                mode=previous_mode,
            )

            def undo_connection(
                target: str = connection_id,
                enabled: bool = previous_enabled,
                mode: str = previous_mode,
            ) -> Any:
                return self.connector_service.put_project_binding(
                    project_id=project_id,
                    connection_id=target,
                    enabled=enabled,
                    mode=mode,
                )

            receipt.add(undo_connection)

    def _apply_callback(
        self,
        callback: Optional[GateSetter],
        project_id: str,
        integration_ids: set[str],
        old_policy: Mapping[str, Any],
        policy: Mapping[str, Any],
        receipt: IntegrationPolicyApplyReceipt,
    ) -> None:
        grants = [
            dict(item)
            for item in policy.get("grants") or []
            if isinstance(item, Mapping) and item.get("integration_id") in integration_ids
        ]
        previous_grants = [
            item
            for item in old_policy.get("grants") or []
            if isinstance(item, Mapping) and item.get("integration_id") in integration_ids
        ]
        affected = bool(grants or previous_grants)
        if callback is None:
            if affected:
                raise IntegrationPolicyApplyError(
                    "必要的執行期整合權限閘門尚未連接。"
                )
            return
        mode = str(policy.get("permission_mode") or "blocked") if grants else "blocked"
        result = callback(project_id, mode, grants)
        receipt.add(self._callback_undo(result))

    def __call__(
        self,
        project_id: str,
        old_policy: Mapping[str, Any],
        new_policy: Mapping[str, Any],
    ) -> IntegrationPolicyApplyReceipt:
        receipt = IntegrationPolicyApplyReceipt()
        try:
            self._apply_extensions(project_id, new_policy, receipt)
            self._apply_connectors(project_id, new_policy, receipt)
            self._apply_callback(
                self.connector_gate_setter,
                project_id,
                {"github", "notion", "gmail"},
                old_policy,
                new_policy,
                receipt,
            )
            self._apply_callback(
                self.n8n_gate_setter,
                project_id,
                {"n8n"},
                old_policy,
                new_policy,
                receipt,
            )
            self._apply_callback(
                self.mcp_gate_setter,
                project_id,
                {"mcp"},
                old_policy,
                new_policy,
                receipt,
            )
            self._apply_callback(
                self.external_api_gate_setter,
                project_id,
                {"external_api"},
                old_policy,
                new_policy,
                receipt,
            )
            return receipt
        except Exception as exc:
            try:
                receipt.rollback()
            except Exception as rollback_error:
                raise IntegrationPolicyApplyError(
                    "整合權限方案套用失敗，且回復作業未完整完成。",
                    compensation_incomplete=True,
                ) from rollback_error
            raise IntegrationPolicyApplyError(
                "整合權限方案無法安全套用。",
                compensation_incomplete=bool(
                    getattr(exc, "compensation_incomplete", False)
                ),
            ) from exc


__all__ = [
    "AuthoritativeIntegrationPolicyApplier",
    "IntegrationPolicyApplyError",
    "IntegrationPolicyApplyReceipt",
]
