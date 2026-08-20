from typing import Optional

from pydantic import BaseModel, Field, SecretStr, model_validator


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
    prompt: str = Field(default="", max_length=4000)
    # The NVIDIA hosted direct-upload API requires base64 content below
    # 180,000 characters. Runtime validation also checks MIME and magic bytes.
    image_data_url: str = Field(default="", max_length=180_032)

    @model_validator(mode="after")
    def validate_model_test_input(self):
        is_nvidia_ocr_v2 = (
            self.provider_type.strip().casefold() == "nvidia"
            and self.model.strip().casefold() == "nvidia/nemotron-ocr-v2"
        )
        if is_nvidia_ocr_v2:
            if not self.image_data_url.strip():
                raise ValueError("image_data_url is required for Nemotron OCR v2.")
        elif not self.prompt.strip():
            raise ValueError("prompt must not be empty for non-OCR model tests.")
        return self
