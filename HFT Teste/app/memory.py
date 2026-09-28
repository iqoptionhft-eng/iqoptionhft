from __future__ import annotations

import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .config import get_settings
from .models import Experience, Features, MemoryStats


class ExperienceMemory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path(get_settings().data_dir) / "memory.jsonl")
        self._lock = threading.RLock()
        self._items: list[Experience] = []
        self._load()

    def _load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._items.append(Experience.model_validate_json(line))
                except Exception:
                    continue

    def record(self, exp: Experience) -> None:
        with self._lock:
            self._items.append(exp)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(exp.model_dump_json() + "\n")

    def stats(self) -> MemoryStats:
        with self._lock:
            closed = [e for e in self._items if e.result in ("WIN", "LOSS")]
        wins = sum(1 for e in closed if e.result == "WIN")
        losses = sum(1 for e in closed if e.result == "LOSS")
        total = wins + losses
        return MemoryStats(
            total=total,
            wins=wins,
            losses=losses,
            win_rate=(wins / total) if total else 0.0,
        )

    def similar(
        self, features: Features, direction: str, limit: int = 8, before: datetime | None = None
    ) -> list[Experience]:
        """`before`: so considera experiencias anteriores a esse instante (evita lookahead no backtest, M5)."""
        with self._lock:
            items = list(self._items)
        scored: list[tuple[float, Experience]] = []
        for exp in items:
            if exp.result not in ("WIN", "LOSS"):
                continue
            if exp.direction != direction:
                continue
            if before is not None:
                ts = exp.ts.replace(tzinfo=None) if exp.ts.tzinfo else exp.ts
                if ts >= before:
                    continue
            dist = abs(exp.rsi - features.rsi) / 14.0
            dist += abs(exp.ta_score - features.ta_score)
            if exp.trend != features.trend:
                dist += 0.35
            if exp.asset != features.asset:
                dist += 0.25
            scored.append((dist, exp))
        scored.sort(key=lambda x: x[0])
        return [e for _, e in scored[:limit]]

    def similar_win_rate(
        self, features: Features, direction: str, before: datetime | None = None
    ) -> tuple[float | None, int, str]:
        cases = self.similar(features, direction, limit=12, before=before)
        if len(cases) < 3:
            return None, len(cases), "poucos casos semelhantes na memoria"
        wins = sum(1 for c in cases if c.result == "WIN")
        rate = wins / len(cases)
        sample = "; ".join(f"{c.asset} RSI {c.rsi:.0f} {c.result}" for c in cases[:4])
        hint = f"{len(cases)} casos parecidos: {wins}/{len(cases)} wins ({rate:.0%}). Ex: {sample}"
        return rate, len(cases), hint

    def context_for_prompt(self, features: Features, direction: str) -> str:
        cases = self.similar(features, direction, limit=6)
        if not cases:
            return "Sem historico semelhante."
        lines = [
            "Historico de situacoes parecidas (use para evitar repetir erros):",
        ]
        for c in cases:
            lines.append(
                f"- {c.asset} {c.direction.upper()} RSI={c.rsi:.1f} score={c.ta_score:.2f} "
                f"tendencia={c.trend} IA={c.llm_action or 'n/a'} -> {c.result}"
                + (f" ({c.llm_reason})" if c.llm_reason else "")
            )
        rate, n, hint = self.similar_win_rate(features, direction)
        if rate is not None:
            lines.append(f"Resumo: {hint}")
            if rate < 0.4:
                lines.append(
                    "ATENCAO: nesse padrao o sinal costumou ser falso. Prefira skip "
                    "a menos que o setup atual seja claramente diferente."
                )
        elif n:
            lines.append(hint)
        return "\n".join(lines)


@lru_cache
def get_memory() -> ExperienceMemory:
    return ExperienceMemory()
