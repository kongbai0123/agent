"""Chat settings and encrypted provider-secret routes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Request

from api.routes.provider_settings import build_provider_settings_router
from model_governance import ModelGovernanceService


_HERMES_SECRET_SETTING_KEYS = frozenset({
    "hermes_api_key",
    "hermes_api_key_secret",
    "hermes_api_key_ref",
    "hermes_bearer_token",
    "hermes_password",
    "hermes_secret",
    "hermes_token",
})


def _public_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Return the settings payload without any Hermes credential material."""

    return {
        key: value
        for key, value in settings.items()
        if key not in _HERMES_SECRET_SETTING_KEYS
    }


def build_settings_router(
    *,
    load_settings: Callable[[], Dict[str, Any]],
    save_settings: Callable[[Dict[str, Any]], Any],
    validate_settings: Callable[[Dict[str, Any]], Dict[str, Any]],
    effective_config: Callable[[Dict[str, Any]], Dict[str, Any]],
    normalize_modal_size: Callable[[Dict[str, Any]], Dict[str, int]],
    apply_configuration: Callable[[Dict[str, Any]], None],
    error_payload: Callable[..., Dict[str, Any]],
    require_local: Optional[Callable[[Request], None]] = None,
    hermes_rollout_guard: Optional[
        Callable[[Dict[str, Any], Dict[str, Any]], None]
    ] = None,
    model_governance: Optional[ModelGovernanceService] = None,
) -> APIRouter:
    router = APIRouter(tags=["settings"])
    router.include_router(build_provider_settings_router(
        load_settings=load_settings,
        error_payload=error_payload,
        require_local=require_local,
        model_governance=model_governance,
    ))

    @router.get("/api/settings")
    def get_settings():
        cfg = load_settings()
        public_cfg = _public_settings(cfg)
        return {
            **public_cfg,
            "success": True,
            "settings": public_cfg,
            "effective": effective_config(cfg),
            "reload_required": {"models": False, "rag_index": False},
        }

    @router.post("/api/settings")
    def post_settings(settings_data: Dict[str, Any]):
        if hermes_rollout_guard is not None:
            hermes_rollout_guard(load_settings(), settings_data)
        cfg = validate_settings(settings_data)
        save_settings(cfg)
        apply_configuration(cfg)
        public_cfg = _public_settings(cfg)
        return {
            "success": True,
            "settings": public_cfg,
            "effective": effective_config(cfg),
            "reload_required": {"models": True, "rag_index": True},
            **public_cfg,
        }

    @router.post("/api/settings/ui-state")
    def post_settings_ui_state(ui_state: Dict[str, Any]):
        cfg = load_settings()
        cfg.update(normalize_modal_size(ui_state))
        save_settings(cfg)
        return {
            "success": True,
            "settings_modal_width": cfg["settings_modal_width"],
            "settings_modal_height": cfg["settings_modal_height"],
        }

    return router
