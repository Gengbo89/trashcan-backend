from functools import cached_property
import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "trashcan-backend"
    app_env: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "*"

    wechat_appid: str = ""
    wechat_secret: str = Field(default="", repr=False)
    wechat_message_template_id: str = "yrnDr2o4chCcJTxEcZm59BThRMrZ3rkt4oXZlHzakus"
    jwt_secret: str = Field(default="change-me", repr=False)
    admin_openids: str = ""
    database_url: str = "postgresql://trashcan:trashcan@127.0.0.1:5432/trashcan"

    dashscope_api_key: str = Field(default="", repr=False)
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    dashscope_service_url: str = "https://dashscope.aliyuncs.com/api/v1"
    dashscope_compatible_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ai_chat_models: str = "qwen-turbo,qwen-plus,qwen-long"
    ai_vision_models: str = "wanx2.1-t2i-turbo:textToImage,wanx2.1-imageedit:imageToImage"
    ai_transcription_models: str = "paraformer-v2"
    ai_image_size: str = "1024*1024"

    rustfs_endpoint_url: str = "https://rustfs.gengbo.top"
    rustfs_access_key: str = Field(default="", repr=False)
    rustfs_secret_key: str = Field(default="", repr=False)
    rustfs_bucket: str = "trashcan"
    rustfs_region: str = "us-east-1"
    rustfs_addressing_style: str = "path"

    max_upload_size_bytes: int = 10 * 1024 * 1024
    default_upload_dir: str = ""
    presigned_url_expires_seconds: int = 3600
    office_bin: str = "soffice"

    @cached_property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @cached_property
    def admin_openid_set(self) -> set[str]:
        return {item.strip() for item in self.admin_openids.split(",") if item.strip()}

    def model_list(self, value: str) -> list[str]:
        models = [item.strip() for item in value.split(",") if item.strip()]
        return models

    def default_model(self, value: str) -> str:
        models = self.model_list(value)
        return models[0] if models else ""

    def vision_model_list(self, capability: str) -> list[str]:
        models = []
        for item in self.ai_vision_models.split(","):
            entry = item.strip()
            if not entry:
                continue
            model, _, capability_text = entry.partition(":")
            model = model.strip()
            capabilities = {part.strip() for part in capability_text.split("|") if part.strip()}
            if model and (not capabilities or "*" in capabilities or capability in capabilities):
                models.append(model)
        return models

    def default_vision_model(self, capability: str) -> str:
        models = self.vision_model_list(capability)
        return models[0] if models else ""

    @property
    def logging_level(self) -> int:
        return getattr(logging, self.log_level.upper(), logging.INFO)


settings = Settings()
