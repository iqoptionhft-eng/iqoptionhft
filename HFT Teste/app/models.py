from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AccountMode = Literal["PRACTICE", "REAL"]
Direction = Literal["call", "put"]
EngineStatus = Literal["idle", "connecting", "running", "paused", "error"]
FallbackMode = Literal["ta_pure", "safe", "skip"]
LlmOutcome = Literal["agree", "reject", "skip", "timeout", "error", "unused"]


class BotSettings(BaseModel):
    account: AccountMode = "PRACTICE"
    amount: float = Field(default=2.0, ge=1.0, le=10000.0)
    duration_min: int = Field(default=1, ge=1, le=5)
    assets: list[str] = Field(
        default_factory=lambda: [
            "EURUSD-OTC",
            "GBPUSD-OTC",
            "EURGBP-OTC",
            "USDJPY-OTC",
            "AUDUSD-OTC",
        ]
    )
    min_payout: float = Field(default=80.0, ge=50.0, le=100.0)
    min_confidence: float = Field(default=0.72, ge=0.5, le=0.99)
    use_llm: bool = True
    llm_timeout_sec: float = Field(default=4.0, ge=1.0, le=20.0)
    ollama_model: str = "gemma4:31b-it-qat"
    ollama_fast_model: str = ""
    use_fast_model_first: bool = False
    fallback_mode: FallbackMode = "safe"
    memory_enabled: bool = True
    poll_interval_sec: float = Field(default=0.35, ge=0.15, le=2.0)
    max_open_trades: int = Field(default=1, ge=1, le=5)
    max_daily_loss: float = Field(default=40.0, ge=1.0)
    max_consecutive_losses: int = Field(default=3, ge=1, le=10)
    cooldown_sec: float = Field(default=8.0, ge=0.0)
    enter_last_seconds: int = Field(default=8, ge=3, le=20)


class Candle(BaseModel):
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Features(BaseModel):
    asset: str
    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    macd: float
    macd_signal: float
    stoch_k: float
    atr: float
    momentum: float
    body_ratio: float
    trend: str
    ta_score: float
    ta_direction: Direction | None
    ta_reason: str


class Signal(BaseModel):
    asset: str
    direction: Direction
    confidence: float
    payout: float
    source: str
    reason: str
    features: Features
    ta_direction: Direction | None = None
    ta_score: float = 0.0
    llm_action: str = ""
    llm_reason: str = ""
    llm_outcome: LlmOutcome = "unused"
    used_model: str = ""
    fallback_used: bool = False
    memory_hint: str = ""


class TradeRecord(BaseModel):
    id: str
    asset: str
    direction: Direction
    amount: float
    payout: float
    opened_at: datetime
    closed_at: datetime | None = None
    result: Literal["WIN", "LOSS", "EQUAL", "OPEN", "ERROR"] = "OPEN"
    profit: float = 0.0
    source: str = ""
    reason: str = ""
    ta_score: float = 0.0
    ta_direction: Direction | None = None
    rsi: float = 0.0
    stoch_k: float = 0.0
    trend: str = ""
    llm_action: str = ""
    llm_reason: str = ""
    llm_outcome: LlmOutcome = "unused"
    used_model: str = ""
    fallback_used: bool = False
    memory_hint: str = ""


class Experience(BaseModel):
    id: str
    ts: datetime
    asset: str
    direction: Direction
    result: str
    profit: float = 0.0
    ta_score: float
    ta_direction: Direction | None
    rsi: float
    stoch_k: float
    trend: str
    llm_action: str = ""
    llm_reason: str = ""
    llm_outcome: str = ""
    used_model: str = ""
    source: str = ""
    reason: str = ""


class DecisionLog(BaseModel):
    ts: datetime
    asset: str
    action: str
    detail: str
    ta_direction: str = ""
    ta_score: float = 0.0
    llm_outcome: str = ""
    used_model: str = ""
    fallback_used: bool = False


class HealthStatus(BaseModel):
    iq_ok: bool = False
    iq_detail: str = "desconectado"
    ollama_ok: bool = False
    ollama_detail: str = "nao verificado"
    models: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None
    ready_to_trade: bool = False


class MemoryStats(BaseModel):
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0


class LogEvent(BaseModel):
    ts: datetime
    level: str
    message: str


class BacktestResult(BaseModel):
    asset: str
    candles: int = 0
    signals: int = 0
    taken: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    skipped_memory: int = 0
    notes: list[str] = Field(default_factory=list)


class RuntimeState(BaseModel):
    status: EngineStatus = "idle"
    connected: bool = False
    account: AccountMode = "PRACTICE"
    email: str = ""
    balance: float = 0.0
    currency: str = "USD"
    ollama_ok: bool = False
    ollama_model: str = ""
    last_error: str = ""
    wins: int = 0
    losses: int = 0
    equals: int = 0
    profit_today: float = 0.0
    consecutive_losses: int = 0
    open_trades: int = 0
    last_signal: str = ""
    settings: BotSettings = Field(default_factory=BotSettings)
    trades: list[TradeRecord] = Field(default_factory=list)
    logs: list[LogEvent] = Field(default_factory=list)
    decisions: list[DecisionLog] = Field(default_factory=list)
    health: HealthStatus = Field(default_factory=HealthStatus)
    memory: MemoryStats = Field(default_factory=MemoryStats)
    ollama_models: list[str] = Field(default_factory=list)
    last_backtest: BacktestResult | None = None
