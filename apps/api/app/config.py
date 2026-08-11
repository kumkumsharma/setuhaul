from functools import lru_cache
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


IST = ZoneInfo("Asia/Kolkata")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./data/runtime/setuhaul.db"
    redis_url: str = "fakeredis://"
    hold_ttl_seconds: int = 120
    idempotency_ttl_seconds: int = 600
    scenario_now: str | None = "2026-08-11T17:25:00+05:30"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def now(self) -> datetime:
        if self.scenario_now:
            return datetime.fromisoformat(self.scenario_now)
        return datetime.now(tz=IST)


@lru_cache
def get_settings() -> Settings:
    return Settings()
