from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    iq_email: str = ""
    iq_password: str = ""
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:31b-it-qat"
    ollama_fast_model: str = ""
    default_account: str = "PRACTICE"
    host: str = "127.0.0.1"
    port: int = 8787


@lru_cache
def get_settings() -> Settings:
    return Settings()
