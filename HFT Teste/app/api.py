from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Sequence

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import EngineBusy
from .models import AccountMode, BotSettings, DurationMin, FallbackMode

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_HOSTS = ("127.0.0.1", "localhost")


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: AccountMode
    confirm: str = ""


class SettingsIn(BaseModel):
    # Conhecido #1: sem campo `account`; extra="forbid" devolve 422 se alguem mandar.
    model_config = ConfigDict(extra="forbid")
    amount: float | None = Field(default=None, ge=1, le=10000)
    duration_min: DurationMin | None = None
    assets: list[str] | None = None
    min_payout: float | None = Field(default=None, ge=50, le=100)
    min_confidence: float | None = Field(default=None, ge=0.5, le=0.99)
    use_llm: bool | None = None
    llm_timeout_sec: float | None = Field(default=None, ge=1, le=20)
    ollama_model: str | None = None
    ollama_fast_model: str | None = None
    use_fast_model_first: bool | None = None
    fallback_mode: FallbackMode | None = None
    memory_enabled: bool | None = None
    max_daily_loss: float | None = Field(default=None, ge=1, le=10000)
    max_consecutive_losses: int | None = Field(default=None, ge=1, le=10)
    cooldown_sec: float | None = Field(default=None, ge=0, le=3600)


class BacktestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str | None = None


def _err(status: int, reason: object) -> HTTPException:
    return HTTPException(status_code=status, detail=str(reason or "erro desconhecido"))


def create_app(engine: Any, token: str, allowed_hosts: Sequence[str] = DEFAULT_HOSTS) -> FastAPI:
    if not token or len(token) < 16:
        raise ValueError("token do painel precisa ter pelo menos 16 caracteres")

    app = FastAPI(title="HFT IQ Option + Gemma", docs_url=None, redoc_url=None, openapi_url=None)
    # A2: bloqueia DNS rebinding (Host diferente de 127.0.0.1/localhost -> 400)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def require_token(x_token: str | None = Header(default=None, alias="X-Token")) -> None:
        # A2: CSRF — um site externo nao consegue enviar header customizado sem preflight CORS
        if not x_token or not secrets.compare_digest(x_token, token):
            raise HTTPException(status_code=401, detail="X-Token ausente ou invalido")

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        # pagina estatica; o token vem do fragmento da URL (#token=...), que nunca chega ao servidor
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @api.get("/state")
    def state():
        return engine.snapshot().model_dump(mode="json")

    @api.post("/connect")
    def connect():
        ok, reason = engine.connect()
        if not ok:
            raise _err(400, reason)
        return engine.snapshot().model_dump(mode="json")

    @api.post("/start")
    def start():
        try:
            engine.start()
        except EngineBusy as exc:
            raise _err(409, exc) from exc
        except Exception as exc:
            raise _err(400, exc) from exc
        return engine.snapshot().model_dump(mode="json")

    @api.post("/stop")
    def stop():
        engine.stop()
        return engine.snapshot().model_dump(mode="json")

    @api.post("/account")
    def account(body: AccountIn):
        try:
            if not engine.iq.connected:
                ok, reason = engine.connect()
                if not ok:
                    raise _err(400, reason)
            engine.set_account(body.mode, body.confirm)
        except HTTPException:
            raise
        except PermissionError as exc:
            raise _err(403, exc) from exc
        except EngineBusy as exc:
            raise _err(409, exc) from exc
        except Exception as exc:
            raise _err(400, exc) from exc
        return engine.snapshot().model_dump(mode="json")

    @api.post("/settings")
    def settings(body: SettingsIn) -> BotSettings:
        try:
            return engine.update_settings(body.model_dump(exclude_none=True))
        except Exception as exc:
            raise _err(400, exc) from exc

    @api.get("/health")
    def health():
        engine._refresh_health()
        return engine.health.model_dump(mode="json")

    @api.get("/models")
    def models():
        return {"models": engine.brain.list_models()}

    @api.post("/backtest")
    def backtest(body: BacktestIn):
        try:
            return engine.run_backtest(body.asset).model_dump(mode="json")
        except Exception as exc:
            raise _err(400, exc) from exc

    app.include_router(api)
    return app
