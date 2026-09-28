from __future__ import annotations

from datetime import datetime

from .features import extract_features, ta_confidence
from .memory import ExperienceMemory
from .models import BacktestResult, Candle


def run_ta_backtest(
    asset: str,
    candles: list[Candle],
    store: ExperienceMemory | None,
    *,
    min_confidence: float = 0.72,
    payout: float = 0.0,
    use_memory_filter: bool = True,
) -> BacktestResult:
    """M5: mesma regra de entrada do motor ao vivo (sem IA), com payout e sem lookahead da memoria."""
    signals = taken = wins = losses = equals = skipped_memory = 0

    if len(candles) < 40:
        return BacktestResult(asset=asset, candles=len(candles), payout=payout, notes=["Poucas velas para backtest."])

    for i in range(30, len(candles) - 1):
        window = candles[: i + 1]
        feat = extract_features(asset, window)
        if feat.ta_direction is None or ta_confidence(feat.ta_score) < min_confidence:
            continue
        signals += 1

        if use_memory_filter and store is not None:
            before = datetime.fromtimestamp(window[-1].ts) if window[-1].ts else None
            rate, n, _hint = store.similar_win_rate(feat, feat.ta_direction, before=before)
            if rate is not None and n >= 4 and rate < 0.38:
                skipped_memory += 1
                continue

        taken += 1
        cur, nxt = window[-1].close, candles[i + 1].close
        if nxt == cur:
            equals += 1
        elif (nxt > cur) == (feat.ta_direction == "call"):
            wins += 1
        else:
            losses += 1

    decided = wins + losses
    p = payout / 100.0 if payout > 1 else payout
    breakeven = (1.0 / (1.0 + p)) if p > 0 else 0.0
    ev = ((wins * p - losses) / taken) if taken and p > 0 else 0.0

    notes: list[str] = []
    if skipped_memory:
        notes.append(f"Memoria bloqueou {skipped_memory} sinais com historico ruim (so experiencias anteriores a cada vela).")
    if not p:
        notes.append("Payout desconhecido: EV nao calculado.")
    else:
        notes.append(f"Payout {payout:.0f}%: empate exige win rate >= {breakeven:.1%}. EV por unidade apostada {ev:+.3f}.")
    if decided < 100:
        notes.append(f"Amostra pequena ({decided} trades decididos): resultado nao e estatisticamente confiavel.")
    notes.append(
        "Limitacoes: sem IA; usa velas fechadas enquanto o motor ao vivo decide com a vela ainda aberta "
        "nos ultimos segundos; nao modela slippage nem o strike real da opcao digital."
    )

    return BacktestResult(
        asset=asset,
        candles=len(candles),
        signals=signals,
        taken=taken,
        wins=wins,
        losses=losses,
        equals=equals,
        win_rate=(wins / decided) if decided else 0.0,
        payout=payout,
        breakeven_win_rate=breakeven,
        expected_value_per_unit=round(ev, 4),
        skipped_memory=skipped_memory,
        notes=notes,
    )
