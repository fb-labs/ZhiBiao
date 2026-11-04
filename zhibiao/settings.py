from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "智标"
    app_version: str = "2.0"
    debug: bool = True

    host: str = "127.0.0.1"
    port: int = 8000
    web_dist: str = "../web_v2_dist"
    allowed_origins: str = ""

    db_type: str = "postgresql"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/zhibiao"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_database: str = "zhibiao"

    data_root: str = "./var"
    upload_root: str = "./var/uploads"
    delivery_root: str = "./var/deliveries"
    template_root: str = "./var/templates"

    ai_base_url: str = "https://token.sensenova.cn/v1"
    ai_api_key: str = ""
    ai_model: str = "glm-5.2"
    ai_timeout: int = 60

    bootstrap_username: str = "admin"
    bootstrap_password: str = "sxit@123456"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ZHIBIAO_",
        extra="ignore",
    )

    def database_dsn(self) -> str:
        if self.db_type == "mysql":
            return (
                f"mysql+aiomysql://{quote_plus(self.mysql_user)}:{quote_plus(self.mysql_password)}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
            )
        return self.database_url

    def path(self, configured: str) -> Path:
        target = Path(configured)
        if target.is_absolute():
            return target
        return Path(__file__).resolve().parent.parent / target

    def web_dist_path(self) -> Path | None:
        if not self.web_dist:
            return None
        path = self.path(self.web_dist)
        return path if (path / "index.html").is_file() else None

    def ensure_dirs(self) -> None:
        for directory in (self.data_root, self.upload_root, self.delivery_root, self.template_root):
            self.path(directory).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
