from __future__ import annotations

import math
from typing import Sequence

from .models import Candle, Direction, Features


def _closes(candles: Sequence[Candle]) -> list[float]:
    return [c.close for c in candles]


def ema(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))


def macd(values: Sequence[float]) -> tuple[float, float]:
    if len(values) < 26:
        return 0.0, 0.0
    line = ema(values, 12) - ema(values, 26)
    # Approximate signal with EMA of last MACD points via closing series residual
    signal = ema(values[-9:], 9) - ema(values[-26:], 9) * 0.35
    return line, signal


def stoch_k(candles: Sequence[Candle], period: int = 14) -> float:
    window = candles[-period:]
    hh = max(c.high for c in window)
    ll = min(c.low for c in window)
    if hh == ll:
        return 50.0
    return (window[-1].close - ll) / (hh - ll) * 100


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    window = candles[-period:]
    trs = []
    prev = window[0].close
    for c in window:
        tr = max(c.high - c.low, abs(c.high - prev), abs(c.low - prev))
        trs.append(tr)
        prev = c.close
    return sum(trs) / len(trs)


def extract_features(asset: str, candles: Sequence[Candle]) -> Features:
    c = _closes(candles)
    last = candles[-1]
    prev = candles[-2] if len(candles) > 1 else last
    ema_fast = ema(c, 9)
    ema_slow = ema(c, 21)
    r = rsi(c, 14)
    macd_line, macd_sig = macd(c)
    k = stoch_k(candles, 14)
    a = atr(candles, 14)
    mom = 0.0 if prev.close == 0 else (last.close - c[-6]) / abs(c[-6] + 1e-12) if len(c) >= 6 else 0.0
    rng = last.high - last.low
    body = abs(last.close - last.open)
    body_ratio = 0.0 if rng == 0 else body / rng

    score = 0.0
    reasons: list[str] = []

    if ema_fast > ema_slow:
        score += 0.22
        reasons.append("EMA9>EMA21")
    else:
        score -= 0.22
        reasons.append("EMA9<EMA21")

    if last.close > ema_fast:
        score += 0.12
    else:
        score -= 0.12

    if r < 32:
        score += 0.18
        reasons.append("RSI oversold")
    elif r > 68:
        score -= 0.18
        reasons.append("RSI overbought")
    elif r > 55:
        score += 0.08
    elif r < 45:
        score -= 0.08

    if macd_line > 0:
        score += 0.12
    else:
        score -= 0.12

    if k < 20:
        score += 0.1
    elif k > 80:
        score -= 0.1

    bull_engulf = last.close > last.open and prev.close < prev.open and last.close >= prev.open and last.open <= prev.close
    bear_engulf = last.close < last.open and prev.close > prev.open and last.close <= prev.open and last.open >= prev.close
    if bull_engulf:
        score += 0.16
        reasons.append("engolfo de alta")
    if bear_engulf:
        score -= 0.16
        reasons.append("engolfo de baixa")

    if last.close > last.open and body_ratio > 0.65:
        score += 0.08
    if last.close < last.open and body_ratio > 0.65:
        score -= 0.08

    score = max(-1.0, min(1.0, score))
    direction: Direction | None = None
    if score >= 0.18:
        direction = "call"
    elif score <= -0.18:
        direction = "put"

    trend = "alta" if ema_fast > ema_slow else "baixa"
    return Features(
        asset=asset,
        close=last.close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=r,
        macd=macd_line,
        macd_signal=macd_sig,
        stoch_k=k,
        atr=a,
        momentum=mom,
        body_ratio=body_ratio,
        trend=trend,
        ta_score=score,
        ta_direction=direction,
        ta_reason="; ".join(reasons) or "neutro",
    )


def seconds_to_next_minute(now_ts: float) -> float:
    return 60.0 - (now_ts % 60.0)


def is_finite_number(v: float) -> bool:
    return math.isfinite(v)
