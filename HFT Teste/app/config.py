from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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
    # M7: o robo SEMPRE inicia em PRACTICE. DEFAULT_ACCOUNT=REAL e ignorado.
    default_account: str = "PRACTICE"
    # Trava extra: sem ALLOW_REAL=1 nenhuma troca para REAL e aceita.
    allow_real: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    # A2: token exigido no header X-Token. Vazio = gerado aleatoriamente no start.
    panel_token: str = ""
    data_dir: str = str(ROOT / "data")

    @field_validator("default_account")
    @classmethod
    def _norm_account(cls, v: str) -> str:
        return (v or "PRACTICE").strip().upper()

    @field_validator("host")
    @classmethod
    def _local_only(cls, v: str) -> str:
        v = (v or "127.0.0.1").strip()
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("HOST precisa ser 127.0.0.1/localhost: o painel controla dinheiro e nao pode ficar exposto na rede")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
