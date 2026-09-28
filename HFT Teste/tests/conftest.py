from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.engine import TradingEngine  # noqa: E402
from app.iq_client import BuyResult  # noqa: E402
from app.memory import ExperienceMemory  # noqa: E402
from app.models import BotSettings, Candle  # noqa: E402
from app.risk import RiskLedger  # noqa: E402


class FakeIQ:
    """Cliente falso da corretora: nenhuma conexao de rede."""

    def __init__(self, *, server_ts: float | None = None, payout: float = 85.0, allow_real: bool = False):
        self.connected = False
        self.api = None
        self.account = "PRACTICE"
        self.allow_real = allow_real
        self._server_ts = server_ts
        self._payout = payout
        self.connect_delay = 0.0
        self.connect_calls = 0
        self.buy_calls: list[tuple] = []
        self.buy_response = BuyResult("ok", 1001, "ok")
        self.results: dict[int, tuple[str, float]] = {}
        self.candle_calls = 0
        self._lock = threading.Lock()

    def connect(self, blocking: bool = True):
        with self._lock:
            self.connect_calls += 1
        time.sleep(self.connect_delay)
        self.connected = True
        self.api = SimpleNamespace()
        return True, "ok"

    def check_connect(self):
        return self.connected

    def change_account(self, mode):
        if mode == "REAL" and not self.allow_real:
            raise RuntimeError("Conta REAL bloqueada (ALLOW_REAL=0).")
        self.account = mode
        return 1000.0

    def balance(self):
        return 10000.0 if self.connected else None

    def currency(self):
        return "USD"

    def server_time(self):
        return self._server_ts if self._server_ts is not None else time.time()

    def set_time(self, ts: float):
        self._server_ts = ts

    def open_assets(self, preferred):
        return list(preferred)

    def payout(self, asset):
        return self._payout

    def candles(self, asset, size=60, count=80):
        self.candle_calls += 1
        out = []
        price = 1.0
        for i in range(count):
            price += 0.001  # tendencia de alta -> TA indica CALL
            out.append(Candle(ts=1_700_000_000 + 60 * i, open=price - 0.0008, high=price + 0.0002,
                              low=price - 0.001, close=price, volume=1))
        return out

    def buy(self, asset, amount, direction, duration):
        self.buy_calls.append((asset, amount, direction, duration))
        return self.buy_response

    def check_result(self, order_id):
        return self.results.get(order_id)


class FakeBrain:
    def __init__(self):
        self.last_models = []
        self.decision = ("call", 0.9, "ok", "fake", False)

    def set_models(self, *a, **k):
        pass

    def health(self, model=None):
        return True, "fake", ["fake"]

    def list_models(self):
        return ["fake"]

    def warmup(self, *a, **k):
        pass

    def decide(self, *a, **k):
        return self.decision


@pytest.fixture
def cfg(tmp_path):
    return Settings(_env_file=None, iq_email="x@y.z", iq_password="p", data_dir=str(tmp_path))


@pytest.fixture
def make_engine(tmp_path, cfg):
    def _make(iq=None, settings=None, day="2026-09-28", cfg_override=None):
        today = {"v": day}
        ledger = RiskLedger(tmp_path / "ledger.json", today=lambda: today["v"])
        eng = TradingEngine(
            iq=iq or FakeIQ(),
            brain=FakeBrain(),
            store=ExperienceMemory(tmp_path / "memory.jsonl"),
            ledger=ledger,
            cfg=cfg_override or cfg,
            settings=settings or BotSettings(use_llm=False, poll_interval_sec=0.15),
            start_health=False,
        )
        eng._today = today
        return eng

    return _make
