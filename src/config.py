from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "trashcan-backend"
    app_env: str = "development"
    cors_origins: str = "*"

    rustfs_endpoint_url: str = "https://rustfs.gengbo.top"
    rustfs_access_key: str = Field(default="", repr=False)
    rustfs_secret_key: str = Field(default="", repr=False)
    rustfs_bucket: str = "trashcan"
    rustfs_public_base_url: str = "https://rustfs.gengbo.top/trashcan"
    rustfs_region: str = "us-east-1"
    rustfs_addressing_style: str = "path"

    max_upload_size_bytes: int = 10 * 1024 * 1024
    default_upload_dir: str = "trashcan"

    @cached_property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
