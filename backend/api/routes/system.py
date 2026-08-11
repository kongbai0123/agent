from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

from fastapi import APIRouter


from extension_manifest import safe_settings_identifier
from model_capabilities import model_capability_profile


def configured_model_summaries(settings: Mapping[str, Any]) -> list[Dict[str, Any]]:
    """Describe saved API model choices without exposing endpoints or secrets."""
    summaries: list[Dict[str, Any]] = []
    for item in settings.get("model_providers") or []:
        if not isinstance(item, Mapping):
            continue
        provider_id = str(item.get("id") or "").strip().casefold()
        selected_model = str(item.get("selected_model") or "").strip()
        if not provider_id or not selected_model:
            continue
        try:
            profile = model_capability_profile(
                selected_model,
                model_kind=str(item.get("model_kind") or ""),
                supports_tools=bool(item.get("supports_tools", False)),
                language_pair=str(item.get("language_pair") or ""),
            )
        except ValueError:
            profile = model_capability_profile(
                selected_model,
                model_kind="unknown",
            )
        summaries.append({
            "name": f"{provider_id}::{selected_model}",
            "provider": provider_id,
            "provider_label": str(item.get("label") or provider_id),
            "selected_model": selected_model,
            "extension_id": f"provider.{safe_settings_identifier(provider_id)}",
            "model_kind": profile.kind,
            "eligible_for_chat": profile.eligible_for_primary,
            "eligible_roles": list(profile.eligible_roles),
        })
    return summaries


def build_system_router(
    *,
    app_version: str,
    model_inventory: Callable[[], list],
    settings_loader: Callable[[], Dict[str, Any]],
    startup_status: Callable[[], Dict[str, Any]],
) -> APIRouter:
    """Build the small health and model-discovery surface used by chat."""

    router = APIRouter(prefix="/api", tags=["system"])

    @router.get("/health")
    def health():
        # Public by design: the launcher needs this before it can receive a token.
        return {"success": True, "status": "ok", "version": app_version}

    @router.get("/status")
    def status():
        models = model_inventory()
        return {
            "status": "ok" if models else "warning",
            "backend": {
                "status": "ok",
                "version": app_version,
            },
            "ollama": {
                "status": "connected" if models else "disconnected",
                "url": settings_loader()["ollama_url"],
                "models_count": len(models),
            },
            "storage": {
                "db": "ok",
            },
            "ollama_legacy": "connected" if models else "disconnected",
        }

    @router.get("/startup/status")
    def startup():
        return {"success": True, **startup_status()}

    @router.get("/models")
    def models():
        return {
            "models": model_inventory(),
            "configured_models": configured_model_summaries(settings_loader()),
        }

    return router
