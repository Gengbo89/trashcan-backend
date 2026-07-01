from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "trashcan-backend"
    app_env: str = "development"
    cors_origins: str = "*"

    wechat_appid: str = ""
    wechat_secret: str = Field(default="", repr=False)
    wechat_message_template_id: str = ""
    jwt_secret: str = Field(default="change-me", repr=False)
    admin_openids: str = ""
    database_url: str = "postgresql://trashcan:trashcan@127.0.0.1:5432/trashcan"

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


settings = Settings()
