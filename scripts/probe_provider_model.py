"""Safely probe one configured provider with the production request adapter.

``preflight`` uses the model's declared capability contract. ``chat`` first
checks primary-Agent eligibility and refuses specialized models locally. The
script never prints credentials or a raw upstream body; any provider reason is
passed through the same redaction policy used by the Workbench UI.

    python scripts\\probe_provider_model.py connection --shape preflight
    python scripts\\probe_provider_model.py connection --shape chat
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import requests  # noqa: E402
from secret_store import get_provider_secret  # noqa: E402
from model_capabilities import (  # noqa: E402
    build_openai_chat_payload,
    model_capability_profile,
    safe_upstream_error_reason,
)

AGENT_SYSTEM_PROMPT = (
    "你是 Local AI Workbench 的執行代理。請依照計畫執行工作與必要工具，"
    "並在回答中保留來源與驗證結果。"
)


def load_provider(provider_id: str) -> dict:
    settings = json.loads((ROOT / "backend" / "settings.json").read_text(encoding="utf-8"))
    for item in settings.get("model_providers") or []:
        if str(item.get("id") or "").strip().casefold() == provider_id.strip().casefold():
            return item
    raise SystemExit(f"settings.json has no model provider with id {provider_id!r}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider_id")
    parser.add_argument("--shape", choices=["preflight", "chat"], default="chat")
    parser.add_argument("--model", default="")
    parser.add_argument("--system", default="", help="override the system message")
    parser.add_argument("--prompt", default="The GRACE mission measured Earth's gravity field.")
    args = parser.parse_args(argv)

    provider = load_provider(args.provider_id)
    base_url = str(provider.get("base_url") or "").rstrip("/")
    model = args.model or str(provider.get("selected_model") or "")
    url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
    key = get_provider_secret(args.provider_id)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    language_pair = (
        args.system
        or str(provider.get("language_pair") or "")
        or ("en-zh-cn" if "translate" in model.casefold() else "")
    )
    profile = model_capability_profile(
        model,
        model_kind=str(provider.get("model_kind") or ""),
        supports_tools=bool(provider.get("supports_tools", False)),
        language_pair=language_pair,
    )
    if args.shape == "chat" and not profile.eligible_for_primary:
        print(json.dumps({
            "accepted": False,
            "reason": "specialized model is not eligible for primary Agent chat",
            "model_kind": profile.kind,
        }, ensure_ascii=True))
        return 2

    system = language_pair if profile.kind == "translation" else (args.system or AGENT_SYSTEM_PROMPT)
    payload = build_openai_chat_payload(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": args.prompt},
            ],
            "max_tokens": 512,
            "temperature": 0.2,
        },
        stream=args.shape == "chat",
        profile=profile,
    )

    print(f"POST {url}")
    print(json.dumps({"model": model, "kind": profile.kind, "shape": args.shape}, ensure_ascii=True))
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
        stream=False,
    )
    reason = safe_upstream_error_reason(response.text, secrets=(key,))
    print(f"HTTP {response.status_code}")
    if response.status_code == 200:
        print(json.dumps({"accepted": True}, ensure_ascii=True))
        if reason:
            print(json.dumps({"response_preview": reason}, ensure_ascii=True))
        return 0
    print(json.dumps({"accepted": False, "reason": reason or "provider rejected request"}, ensure_ascii=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
