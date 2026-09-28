from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import engine
from .models import AccountMode, BotSettings, FallbackMode

app = FastAPI(title="HFT IQ Option + Gemma")
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class AccountIn(BaseModel):
    mode: AccountMode
    confirm: str = ""


class SettingsIn(BaseModel):
    account: AccountMode | None = None
    amount: float | None = Field(default=None, ge=1)
    duration_min: int | None = Field(default=None, ge=1, le=5)
    assets: list[str] | None = None
    min_payout: float | None = None
    min_confidence: float | None = None
    use_llm: bool | None = None
    llm_timeout_sec: float | None = None
    ollama_model: str | None = None
    ollama_fast_model: str | None = None
    use_fast_model_first: bool | None = None
    fallback_mode: FallbackMode | None = None
    memory_enabled: bool | None = None
    max_daily_loss: float | None = None
    max_consecutive_losses: int | None = None
    cooldown_sec: float | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
def state():
    return engine.snapshot().model_dump(mode="json")


def _http_error(reason: object) -> HTTPException:
    if isinstance(reason, (dict, list)):
        text = str(reason)
    else:
        text = str(reason or "erro desconhecido")
    return HTTPException(status_code=400, detail=text)


@app.post("/api/connect")
def connect():
    ok, reason = engine.connect()
    if not ok:
        raise _http_error(reason)
    return engine.snapshot().model_dump(mode="json")


@app.post("/api/start")
def start():
    try:
        engine.start()
    except Exception as exc:
        raise _http_error(exc) from exc
    return engine.snapshot().model_dump(mode="json")


@app.post("/api/stop")
def stop():
    engine.stop()
    return engine.snapshot().model_dump(mode="json")


@app.post("/api/account")
def account(body: AccountIn):
    if body.mode == "REAL" and body.confirm.strip().upper() != "REAL":
        raise _http_error("Para conta REAL, confirme digitando REAL")
    if not engine.iq.connected:
        ok, reason = engine.connect()
        if not ok:
            raise _http_error(reason)
    engine.set_account(body.mode)
    return engine.snapshot().model_dump(mode="json")


@app.post("/api/settings")
def settings(body: SettingsIn) -> BotSettings:
    return engine.update_settings(body.model_dump(exclude_none=True))


class BacktestIn(BaseModel):
    asset: str | None = None


@app.get("/api/health")
def health():
    engine._refresh_health()
    return engine.health.model_dump(mode="json")


@app.get("/api/models")
def models():
    return {"models": engine.brain.list_models()}


@app.post("/api/backtest")
def backtest(body: BacktestIn | None = None):
    try:
        asset = body.asset if body else None
        return engine.run_backtest(asset).model_dump(mode="json")
    except Exception as exc:
        raise _http_error(exc) from exc
