"""Curated local model catalog for the Ollama-backed Model Manager.

The Ollama library contains thousands of community and quantization variants.
Workbench intentionally lists verified, local, generative tags from official
library namespaces and lets advanced users install another safe Ollama tag in
the UI. Cloud-only tags, community namespaces, embeddings, rerankers, guards,
and classifiers stay out of this primary-chat catalog.
"""

from __future__ import annotations

from typing import Any


def _memory_requirements(size_gb: float) -> tuple[int, int, int]:
    """Return conservative minimum RAM, recommended RAM, and VRAM tiers."""

    if size_gb <= 1.0:
        return 4, 8, 2
    if size_gb <= 2.5:
        return 6, 8, 4
    if size_gb <= 5.0:
        return 8, 16, 6
    if size_gb <= 8.0:
        return 12, 16, 10
    if size_gb <= 10.0:
        return 16, 24, 12
    if size_gb <= 16.0:
        return 24, 32, 16
    if size_gb <= 20.0:
        return 32, 48, 24
    if size_gb <= 24.0:
        return 32, 64, 32
    if size_gb <= 70.0:
        return 64, 128, 80
    return 128, 192, 120


def _model(
    name: str,
    display_name: str,
    category: tuple[str, ...],
    size_gb: float,
    context_window: int,
    publisher: str,
    license_name: str,
    family: str,
    *,
    aliases: tuple[str, ...] = (),
    recommendation_priority: int = 1000,
) -> dict[str, Any]:
    min_ram, recommended_ram, recommended_vram = _memory_requirements(size_gb)
    return {
        "name": name,
        "display_name": display_name,
        "family": family,
        "publisher": publisher,
        "license": license_name,
        "source_url": f"https://ollama.com/library/{family}",
        "category": list(category),
        "capabilities": list(category),
        "size_gb_estimated": size_gb,
        "min_ram_gb": min_ram,
        "recommended_ram_gb": recommended_ram,
        "min_vram_gb": 0,
        "recommended_vram_gb": recommended_vram,
        "context_window": context_window,
        "aliases": list(aliases),
        "recommendation_priority": recommendation_priority,
    }


MODEL_CATALOG: list[dict[str, Any]] = [
    # Current multilingual and multimodal general models.
    _model("qwen3.5:0.8b", "Qwen 3.5 0.8B", ("chat", "multilingual", "vision", "tools"), 1.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.5", recommendation_priority=18),
    _model("qwen3.5:2b", "Qwen 3.5 2B", ("chat", "multilingual", "vision", "tools"), 2.7, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.5", recommendation_priority=15),
    _model("qwen3.5:4b", "Qwen 3.5 4B", ("chat", "multilingual", "vision", "tools", "reasoning"), 3.4, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.5", recommendation_priority=12),
    _model("qwen3.5:9b", "Qwen 3.5 9B", ("chat", "multilingual", "vision", "tools", "reasoning"), 6.6, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.5", aliases=("qwen3.5:latest",), recommendation_priority=10),
    _model("qwen3.5:27b", "Qwen 3.5 27B", ("chat", "multilingual", "vision", "tools", "reasoning"), 17.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.5"),
    _model("qwen3.5:35b", "Qwen 3.5 35B", ("chat", "multilingual", "vision", "tools", "reasoning"), 24.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.5"),
    _model("qwen3.6:27b", "Qwen 3.6 27B", ("chat", "code", "vision", "tools", "reasoning"), 17.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.6"),
    _model("qwen3.6:35b", "Qwen 3.6 35B", ("chat", "code", "vision", "tools", "reasoning"), 24.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3.6", aliases=("qwen3.6:latest",)),
    _model("gemma4:e2b", "Gemma 4 E2B", ("chat", "vision", "tools", "reasoning"), 7.2, 131_072, "Google DeepMind", "Apache-2.0", "gemma4"),
    _model("gemma4:e4b", "Gemma 4 E4B", ("chat", "vision", "tools", "reasoning"), 9.6, 131_072, "Google DeepMind", "Apache-2.0", "gemma4", aliases=("gemma4:latest",), recommendation_priority=11),
    _model("gemma4:12b", "Gemma 4 12B", ("chat", "vision", "code", "tools", "reasoning"), 7.6, 262_144, "Google DeepMind", "Apache-2.0", "gemma4", recommendation_priority=21),
    _model("gemma4:26b", "Gemma 4 26B", ("chat", "vision", "code", "tools", "reasoning"), 18.0, 262_144, "Google DeepMind", "Apache-2.0", "gemma4"),
    _model("gemma4:31b", "Gemma 4 31B", ("chat", "vision", "code", "tools", "reasoning"), 20.0, 262_144, "Google DeepMind", "Apache-2.0", "gemma4"),

    # Efficient and general-purpose families.
    _model("granite4:350m", "IBM Granite 4 350M", ("chat", "rag", "tools", "multilingual"), 0.708, 32_768, "IBM", "Apache-2.0", "granite4"),
    _model("granite4:3b", "IBM Granite 4 3B", ("chat", "rag", "code", "tools", "multilingual"), 2.1, 131_072, "IBM", "Apache-2.0", "granite4", aliases=("granite4:latest",), recommendation_priority=35),
    _model("granite4:7b-a1b-h", "IBM Granite 4 Tiny 7B-A1B", ("chat", "rag", "code", "tools", "multilingual"), 4.2, 1_048_576, "IBM", "Apache-2.0", "granite4"),
    _model("granite4:32b-a9b-h", "IBM Granite 4 Small 32B-A9B", ("chat", "rag", "code", "tools", "multilingual"), 19.0, 1_048_576, "IBM", "Apache-2.0", "granite4"),
    _model("ministral-3:3b", "Ministral 3 3B", ("chat", "vision", "tools", "multilingual"), 3.0, 262_144, "Mistral AI", "Apache-2.0", "ministral-3"),
    _model("ministral-3:8b", "Ministral 3 8B", ("chat", "vision", "tools", "multilingual"), 6.0, 262_144, "Mistral AI", "Apache-2.0", "ministral-3", aliases=("ministral-3:latest",), recommendation_priority=16),
    _model("ministral-3:14b", "Ministral 3 14B", ("chat", "vision", "tools", "multilingual"), 9.1, 262_144, "Mistral AI", "Apache-2.0", "ministral-3"),
    _model("smollm2:135m", "SmolLM2 135M", ("chat", "edge"), 0.271, 8_192, "Hugging Face", "Apache-2.0", "smollm2"),
    _model("smollm2:360m", "SmolLM2 360M", ("chat", "edge"), 0.726, 8_192, "Hugging Face", "Apache-2.0", "smollm2"),
    _model("smollm2:1.7b", "SmolLM2 1.7B", ("chat", "edge", "tools"), 1.8, 8_192, "Hugging Face", "Apache-2.0", "smollm2", aliases=("smollm2:latest",)),
    _model("olmo2:7b", "OLMo 2 7B", ("chat", "research"), 4.5, 4_096, "Ai2", "Apache-2.0", "olmo2", aliases=("olmo2:latest",)),
    _model("olmo2:13b", "OLMo 2 13B", ("chat", "research"), 8.4, 4_096, "Ai2", "Apache-2.0", "olmo2"),
    _model("phi4-mini:3.8b", "Phi-4 Mini 3.8B", ("chat", "tools", "reasoning", "multilingual"), 2.5, 131_072, "Microsoft", "MIT", "phi4-mini", aliases=("phi4-mini:latest",), recommendation_priority=17),
    _model("phi4:14b", "Phi-4 14B", ("chat", "math", "reasoning"), 9.1, 16_384, "Microsoft", "MIT", "phi4", aliases=("phi4:latest",)),
    _model("mistral:7b", "Mistral 7B Instruct", ("chat", "tools", "multilingual"), 4.4, 32_768, "Mistral AI", "Apache-2.0", "mistral", aliases=("mistral:latest",)),
    _model("mistral-nemo:12b", "Mistral Nemo 12B", ("chat", "tools", "multilingual"), 7.1, 1_024_000, "Mistral AI / NVIDIA", "Apache-2.0", "mistral-nemo", aliases=("mistral-nemo:latest",)),
    _model("mistral-small3.2:24b", "Mistral Small 3.2 24B", ("chat", "vision", "tools", "multilingual"), 15.0, 131_072, "Mistral AI", "Apache-2.0", "mistral-small3.2", aliases=("mistral-small3.2:latest",)),

    # Reasoning models.
    _model("deepseek-r1:1.5b", "DeepSeek R1 Distill 1.5B", ("chat", "reasoning", "math", "code"), 1.1, 131_072, "DeepSeek", "MIT / base-model terms", "deepseek-r1"),
    _model("deepseek-r1:8b", "DeepSeek R1 0528 8B", ("chat", "reasoning", "math", "code"), 5.2, 131_072, "DeepSeek", "MIT / base-model terms", "deepseek-r1", aliases=("deepseek-r1:latest",), recommendation_priority=30),
    _model("deepseek-r1:14b", "DeepSeek R1 Distill 14B", ("chat", "reasoning", "math", "code"), 9.0, 131_072, "DeepSeek", "MIT / base-model terms", "deepseek-r1"),
    _model("deepseek-r1:32b", "DeepSeek R1 Distill 32B", ("chat", "reasoning", "math", "code"), 20.0, 131_072, "DeepSeek", "MIT / base-model terms", "deepseek-r1"),
    _model("gpt-oss:20b", "OpenAI gpt-oss 20B", ("chat", "reasoning", "code", "tools"), 14.0, 131_072, "OpenAI", "Apache-2.0", "gpt-oss", aliases=("gpt-oss:latest",)),
    _model("gpt-oss:120b", "OpenAI gpt-oss 120B", ("chat", "reasoning", "code", "tools"), 65.0, 131_072, "OpenAI", "Apache-2.0", "gpt-oss"),

    # Small Meta models and the existing general catalog entry.
    _model("llama3.2:1b", "Llama 3.2 1B", ("chat", "rag", "multilingual", "edge"), 1.3, 131_072, "Meta", "Llama 3.2 Community", "llama3.2"),
    _model("llama3.2:3b", "Llama 3.2 3B", ("chat", "rag", "multilingual", "tools"), 2.0, 131_072, "Meta", "Llama 3.2 Community", "llama3.2", aliases=("llama3.2:latest",)),
    _model("llama3.1:8b", "Llama 3.1 8B", ("chat", "rag", "tools"), 4.9, 131_072, "Meta", "Llama 3.1 Community", "llama3.1", aliases=("llama3.1:latest",)),

    # Coding and software-engineering models.
    _model("qwen2.5-coder:1.5b", "Qwen2.5 Coder 1.5B", ("code", "chat", "rag"), 0.986, 32_768, "Alibaba Qwen", "Apache-2.0", "qwen2.5-coder"),
    _model("qwen2.5-coder:3b", "Qwen2.5 Coder 3B", ("code", "chat", "rag"), 1.9, 32_768, "Alibaba Qwen", "Apache-2.0", "qwen2.5-coder"),
    _model("qwen2.5-coder:7b", "Qwen2.5 Coder 7B", ("code", "chat", "rag"), 4.7, 32_768, "Alibaba Qwen", "Apache-2.0", "qwen2.5-coder", aliases=("qwen2.5-coder:latest",), recommendation_priority=13),
    _model("qwen2.5-coder:14b", "Qwen2.5 Coder 14B", ("code", "chat", "rag"), 9.0, 32_768, "Alibaba Qwen", "Apache-2.0", "qwen2.5-coder"),
    _model("qwen2.5-coder:32b", "Qwen2.5 Coder 32B", ("code", "chat", "rag"), 20.0, 32_768, "Alibaba Qwen", "Apache-2.0", "qwen2.5-coder"),
    _model("qwen3-coder:30b", "Qwen3 Coder 30B", ("code", "chat", "tools", "reasoning"), 19.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3-coder", aliases=("qwen3-coder:latest",)),
    _model("qwen3-coder-next:latest", "Qwen3 Coder Next", ("code", "chat", "tools", "reasoning"), 52.0, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3-coder-next"),
    _model("devstral-small-2:24b", "Devstral Small 2 24B", ("code", "chat", "tools", "vision"), 15.0, 393_216, "Mistral AI", "Apache-2.0", "devstral-small-2", aliases=("devstral-small-2:latest",)),
    _model("deepseek-coder-v2:16b", "DeepSeek Coder V2 Lite 16B", ("code", "chat", "reasoning"), 8.9, 163_840, "DeepSeek", "DeepSeek Model License", "deepseek-coder-v2", aliases=("deepseek-coder-v2:latest",)),
    _model("starcoder2:3b", "StarCoder2 3B", ("code",), 1.7, 16_384, "BigCode", "BigCode OpenRAIL-M", "starcoder2", aliases=("starcoder2:latest",)),
    _model("starcoder2:7b", "StarCoder2 7B", ("code",), 4.0, 16_384, "BigCode", "BigCode OpenRAIL-M", "starcoder2"),
    _model("starcoder2:15b-instruct", "StarCoder2 15B Instruct", ("code", "chat"), 9.1, 16_384, "BigCode", "BigCode OpenRAIL-M", "starcoder2"),

    # Multimodal generative models. These are installable and image-capable;
    # embeddings, OCR-only endpoints, rerankers, and safety classifiers are not.
    _model("qwen3-vl:2b", "Qwen3-VL 2B", ("vision", "chat", "tools", "multilingual"), 1.9, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3-vl"),
    _model("qwen3-vl:4b", "Qwen3-VL 4B", ("vision", "chat", "tools", "multilingual"), 3.3, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3-vl", recommendation_priority=14),
    _model("qwen3-vl:8b", "Qwen3-VL 8B", ("vision", "chat", "tools", "multilingual"), 6.1, 262_144, "Alibaba Qwen", "Apache-2.0", "qwen3-vl", aliases=("qwen3-vl:latest",)),
    _model("llama3.2-vision:11b", "Llama 3.2 Vision 11B", ("vision", "chat"), 7.8, 131_072, "Meta", "Llama 3.2 Community", "llama3.2-vision", aliases=("llama3.2-vision:latest",)),
    _model("gemma3:1b", "Gemma 3 1B", ("chat", "multilingual", "edge"), 0.815, 32_768, "Google DeepMind", "Gemma Terms", "gemma3"),
    _model("gemma3:4b", "Gemma 3 4B", ("vision", "chat", "multilingual"), 3.3, 131_072, "Google DeepMind", "Gemma Terms", "gemma3", aliases=("gemma3:latest",)),
    _model("gemma3:12b", "Gemma 3 12B", ("vision", "chat", "multilingual"), 8.1, 131_072, "Google DeepMind", "Gemma Terms", "gemma3"),
    _model("gemma3:27b", "Gemma 3 27B", ("vision", "chat", "multilingual"), 17.0, 131_072, "Google DeepMind", "Gemma Terms", "gemma3"),
    _model("moondream:1.8b", "Moondream 2 1.8B", ("vision", "chat", "edge"), 1.7, 2_048, "Moondream", "Apache-2.0", "moondream", aliases=("moondream:latest",)),
    _model("granite3.2-vision:2b", "IBM Granite 3.2 Vision 2B", ("vision", "chat", "documents"), 2.4, 16_384, "IBM", "Apache-2.0", "granite3.2-vision", aliases=("granite3.2-vision:latest",)),
    _model("llava:7b", "LLaVA 1.6 7B", ("vision", "chat"), 4.7, 32_768, "LLaVA", "LLaVA / base-model terms", "llava", aliases=("llava:latest", "llava:v1.6")),
]
