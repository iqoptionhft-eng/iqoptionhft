from datetime import datetime

from app.backtest import run_ta_backtest
from app.memory import ExperienceMemory
from app.models import Candle, Experience


def candles(n=120):
    out, p = [], 1.0
    for i in range(n):
        p += 0.001 if i % 3 else -0.0005
        out.append(Candle(ts=1_700_000_000 + 60 * i, open=p - 0.0004, high=p + 0.0003, low=p - 0.0006, close=p, volume=1))
    return out


def test_backtest_reporta_empate_e_ev():
    r = run_ta_backtest("X", candles(), None, payout=80, min_confidence=0.6)
    assert abs(r.breakeven_win_rate - 1 / 1.8) < 1e-9
    assert r.taken == r.wins + r.losses + r.equals
    assert any("empate" in n for n in r.notes)


def test_backtest_memoria_nao_usa_futuro(tmp_path):
    mem = ExperienceMemory(tmp_path / "m.jsonl")
    future = datetime.fromtimestamp(1_700_000_000 + 60 * 10_000)
    for i in range(10):
        mem.record(Experience(id=str(i), ts=future, asset="X", direction="call", result="LOSS",
                              ta_score=0.5, ta_direction="call", rsi=60, stoch_k=50, trend="alta"))
    r = run_ta_backtest("X", candles(), mem, payout=80, min_confidence=0.6)
    assert r.skipped_memory == 0
