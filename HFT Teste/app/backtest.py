from __future__ import annotations

from .features import extract_features
from .memory import ExperienceMemory
from .models import BacktestResult, Candle


def run_ta_backtest(
    asset: str,
    candles: list[Candle],
    store: ExperienceMemory,
    min_score: float = 0.35,
    use_memory_filter: bool = True,
) -> BacktestResult:
    notes: list[str] = []
    signals = 0
    taken = 0
    wins = 0
    losses = 0
    skipped_memory = 0

    if len(candles) < 40:
        return BacktestResult(asset=asset, candles=len(candles), notes=["Poucas velas para backtest."])

    for i in range(30, len(candles) - 1):
        window = candles[: i + 1]
        feat = extract_features(asset, window)
        if feat.ta_direction is None or abs(feat.ta_score) < min_score:
            continue
        signals += 1
        nxt = candles[i + 1]
        won = (nxt.close > window[-1].close) if feat.ta_direction == "call" else (nxt.close < window[-1].close)

        if use_memory_filter:
            rate, n, _hint = store.similar_win_rate(feat, feat.ta_direction)
            if rate is not None and n >= 4 and rate < 0.4:
                skipped_memory += 1
                continue

        taken += 1
        if won:
            wins += 1
        else:
            losses += 1

    total = wins + losses
    if skipped_memory:
        notes.append(f"Memoria bloqueou {skipped_memory} sinais com historico ruim.")
    if total:
        notes.append("Backtest usa TA + filtro de memoria nas velas seguintes (sem enviar 31B em cada barra).")
    else:
        notes.append("Nenhum sinal passou nos filtros.")

    return BacktestResult(
        asset=asset,
        candles=len(candles),
        signals=signals,
        taken=taken,
        wins=wins,
        losses=losses,
        win_rate=(wins / total) if total else 0.0,
        skipped_memory=skipped_memory,
        notes=notes,
    )
