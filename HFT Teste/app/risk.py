"""Contadores de risco por conta e por data (conhecido #2, A3), persistidos em disco."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import AccountMode, DayCounters


def _today() -> str:
    # data local da maquina (o usuario opera em America/Sao_Paulo)
    return datetime.now().date().isoformat()


class RiskLedger:
    def __init__(self, path: Path | None, today: Callable[[], str] = _today) -> None:
        self.path = path
        self._today = today
        self._lock = threading.RLock()
        self._days: dict[str, DayCounters] = {}
        self._load()

    @staticmethod
    def _key(account: str, day: str) -> str:
        return f"{account}|{day}"

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self._days[k] = DayCounters.model_validate(v)
        except Exception:
            # arquivo corrompido: nao zera silenciosamente, preserva copia
            bad = self.path.with_suffix(".corrupt")
            try:
                self.path.replace(bad)
            except Exception:
                pass

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: v.model_dump() for k, v in self._days.items()}, indent=1),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def today(self) -> str:
        return self._today()

    def get(self, account: AccountMode, day: str | None = None) -> DayCounters:
        day = day or self._today()
        with self._lock:
            c = self._days.get(self._key(account, day))
            if c is None:
                return DayCounters(account=account, date=day)
            return c.model_copy()

    def record(self, account: AccountMode, result: str, profit: float, day: str | None = None) -> DayCounters:
        day = day or self._today()
        with self._lock:
            key = self._key(account, day)
            c = self._days.get(key) or DayCounters(account=account, date=day)
            c.profit = round(c.profit + float(profit), 2)
            if result == "WIN":
                c.wins += 1
                c.consecutive_losses = 0
            elif result == "LOSS":
                c.losses += 1
                c.consecutive_losses += 1
            elif result == "ERROR":
                # resultado nao apurado: conta como perda (conservador)
                c.errors += 1
                c.consecutive_losses += 1
            else:
                c.equals += 1
            self._days[key] = c
            self._save()
            return c.model_copy()


def can_open(
    counters: DayCounters,
    *,
    open_exposure: float,
    amount: float,
    max_daily_loss: float,
    max_consecutive_losses: int,
) -> tuple[bool, str]:
    """A4: limite diario considerando PnL realizado, exposicao aberta e a proxima ordem."""
    limit = -abs(max_daily_loss)
    if counters.profit <= limit:
        return False, f"limite diario atingido (PnL {counters.profit:.2f} <= {limit:.2f})"
    if counters.consecutive_losses >= max_consecutive_losses:
        return False, f"{counters.consecutive_losses} perdas seguidas (max {max_consecutive_losses})"
    worst = counters.profit - open_exposure - amount
    if worst < limit:
        return False, (
            f"ordem de {amount:.2f} com exposicao aberta {open_exposure:.2f} poderia levar o PnL a "
            f"{worst:.2f}, abaixo do limite {limit:.2f}"
        )
    return True, "ok"

