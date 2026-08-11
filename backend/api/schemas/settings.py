from typing import Optional

from pydantic import BaseModel, Field, SecretStr


class ProviderSecretRequest(BaseModel):
    provider_id: str = Field(min_length=1, max_length=48)
    api_key: str = Field(min_length=1, max_length=16_384)


class ProviderConnectionTestRequest(BaseModel):
    provider_id: str = Field(min_length=1, max_length=48)
    provider_type: str = Field(min_length=1, max_length=48)
    base_url: str = Field(min_length=1, max_length=2_048)
    source_url: str = Field(default="", max_length=1_000)
    selected_model: str = Field(default="", max_length=200)
    api_key: Optional[SecretStr] = None
    enabled: bool = False
    model_kind: str = Field(default="", max_length=20)
    supports_tools: bool = False
    language_pair: str = Field(default="", max_length=32)


class ProviderToolTestRequest(ProviderConnectionTestRequest):
    model: str = Field(min_length=1, max_length=200)


class ProviderModelTestRequest(ProviderConnectionTestRequest):
    model: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(default="", max_length=1000)
    prompt: str = Field(min_length=1, max_length=4000)
