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
    host: str = "0.0.0.0"
    port: int = Field(default=5302, ge=1, le=65535)
    database_url: str = "sqlite:///./data/tarkov.db"

    tarkov_source_base_url: str = "https://json.tarkov.dev"
    tarkov_game_mode: GameMode = "regular"
    tarkov_language: Language = "ru"
    tarkov_translation_languages: str = "ru,en,zh"

    sync_interval_seconds: int = Field(default=3600, ge=60)
    sync_on_startup: bool = True
    request_timeout_seconds: float = Field(default=90.0, gt=0)
    admin_api_key: str | None = None

    redis_cache_enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl_seconds: int = Field(default=3600, ge=60)
    redis_cache_prefix: str = "eft-price-api:v1"
    redis_cache_max_response_bytes: int = Field(
        default=25_000_000,
        ge=1024,
    )
    redis_socket_timeout_seconds: float = Field(default=1.0, gt=0)

    max_page_size: int = Field(default=200, ge=1, le=1000)
    user_agent: str = "tarkov-price-server/0.1 (+local-cache)"

    @property
    def source_base_url(self) -> str:
        return self.tarkov_source_base_url.rstrip("/")

    @property
    def translation_languages(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                language.strip().lower()
                for language in self.tarkov_translation_languages.split(",")
                if language.strip()
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
