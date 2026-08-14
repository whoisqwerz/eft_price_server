from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


GameMode = Literal["regular", "pve", "pvp-season"]
Language = Literal[
    "cs",
    "de",
    "en",
    "es",
    "fr",
    "hu",
    "id",
    "it",
    "ja",
    "ko",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "th",
    "tr",
    "vn",
    "zh",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Tarkov Price Server"
    app_version: str = "0.1.0"
    database_url: str = "sqlite:///./data/tarkov.db"

    tarkov_source_base_url: str = "https://json.tarkov.dev"
    tarkov_game_mode: GameMode = "regular"
    tarkov_language: Language = "ru"

    sync_interval_seconds: int = Field(default=1800, ge=60)
    sync_on_startup: bool = True
    request_timeout_seconds: float = Field(default=90.0, gt=0)
    admin_api_key: str | None = None

    max_page_size: int = Field(default=200, ge=1, le=1000)
    user_agent: str = "tarkov-price-server/0.1 (+local-cache)"

    @property
    def source_base_url(self) -> str:
        return self.tarkov_source_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
