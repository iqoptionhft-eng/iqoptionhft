import threading
import time

import pytest

from app.engine import EngineBusy
from app.iq_client import BuyResult
from app.models import BotSettings, Features, Signal
from app.risk import RiskLedger, can_open
from app.models import DayCounters
from app.features import expected_expiry

from conftest import FakeIQ

# 12:00:55 -> faltam 5s no candle (dentro da janela padrao de 8s)
IN_WINDOW = 1_790_000_000 - (1_790_000_000 % 60) + 55


def signal(asset="EURUSD-OTC"):
    feat = Features(asset=asset, close=1, ema_fast=1, ema_slow=1, rsi=50, macd=0, macd_signal=0, stoch_k=50,
                    atr=0, momentum=0, body_ratio=0, trend="alta", ta_score=0.6, ta_direction="call", ta_reason="x")
    return Signal(asset=asset, direction="call", confidence=0.9, payout=85, source="TA", reason="x", features=feat)


# ---------------------------------------------------------------- A1
def test_start_duplo_cria_uma_thread(make_engine):
    iq = FakeIQ()
    iq.connect_delay = 0.4
    eng = make_engine(iq=iq)
    errors = []

    def go():
        try:
            eng.start()
        except EngineBusy as exc:
            errors.append(exc)

    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    try:
        alive = [t for t in threading.enumerate() if t.name == "hft-engine" and t.is_alive()]
        assert len(alive) == 1
        assert iq.connect_calls == 1
        assert len(errors) == 3
    finally:
        eng.stop()
        eng._thread.join(2)


def test_stop_durante_start_nao_inicia(make_engine):
    iq = FakeIQ()
    iq.connect_delay = 0.3
    eng = make_engine(iq=iq)
    t = threading.Thread(target=lambda: pytest.raises(RuntimeError, eng.start))
    t.start()
    time.sleep(0.1)
    eng.stop()
    t.join()
    assert eng._thread is None
    assert eng.status == "paused"


def test_execute_nao_envia_com_motor_parando(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW)
    eng = make_engine(iq=iq)
    eng._stop.set()
    assert eng._execute(signal()) is False
    assert iq.buy_calls == []


# ---------------------------------------------------------------- A4
def test_can_open_considera_exposicao_e_proxima_ordem():
    c = DayCounters(profit=-36.0)
    assert can_open(c, open_exposure=0, amount=2, max_daily_loss=40, max_consecutive_losses=3)[0]
    ok, why = can_open(c, open_exposure=2, amount=4, max_daily_loss=40, max_consecutive_losses=3)
    assert not ok and "exposicao" in why
    assert not can_open(DayCounters(profit=-40), open_exposure=0, amount=1, max_daily_loss=40, max_consecutive_losses=3)[0]
    assert not can_open(DayCounters(consecutive_losses=3), open_exposure=0, amount=1, max_daily_loss=40, max_consecutive_losses=3)[0]


def test_execute_bloqueia_quando_exposicao_estouraria_limite(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW)
    eng = make_engine(iq=iq, settings=BotSettings(use_llm=False, amount=4, max_daily_loss=40, max_open_trades=3))
    eng.ledger.record("PRACTICE", "LOSS", -34.0)
    assert eng._execute(signal()) is True  # -34 - 0 - 4 = -38 >= -40
    assert eng._execute(signal()) is False  # -34 - 4 - 4 = -42 < -40
    assert len(iq.buy_calls) == 1


def test_amount_maior_que_perda_maxima_e_invalido():
    with pytest.raises(ValueError):
        BotSettings(amount=50, max_daily_loss=40)


# ---------------------------------------------------------------- A3 / ALLOW_REAL / conhecido #1
def test_troca_de_conta_recusada_com_motor_rodando(make_engine):
    eng = make_engine()
    eng.start()
    try:
        with pytest.raises(EngineBusy):
            eng.set_account("PRACTICE")
    finally:
        eng.stop()
        eng._thread.join(2)


def test_troca_de_conta_recusada_com_ordem_aberta(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW)
    eng = make_engine(iq=iq)
    iq.connected = True
    assert eng._execute(signal())
    with pytest.raises(EngineBusy, match="abertas"):
        eng.set_account("PRACTICE")


def test_real_bloqueada_sem_allow_real(make_engine):
    eng = make_engine()
    with pytest.raises(PermissionError, match="ALLOW_REAL"):
        eng.set_account("REAL", "REAL")
    assert eng.account == "PRACTICE"


def test_real_exige_confirmacao_mesmo_com_allow_real(make_engine, cfg):
    eng = make_engine(iq=FakeIQ(allow_real=True), cfg_override=cfg.model_copy(update={"allow_real": True}))
    with pytest.raises(PermissionError, match="confirme"):
        eng.set_account("REAL", "")
    eng.set_account("REAL", "real")
    assert eng.account == "REAL"


def test_settings_nao_troca_conta(make_engine):
    eng = make_engine()
    with pytest.raises(ValueError):
        eng.update_settings({"account": "REAL"})
    assert eng.account == "PRACTICE"


def test_default_account_real_e_ignorado(make_engine, cfg):
    eng = make_engine(cfg_override=cfg.model_copy(update={"default_account": "REAL"}))
    assert eng.account == "PRACTICE"
    assert eng.iq.account == "PRACTICE"


# ---------------------------------------------------------------- conhecido #2
def test_contadores_por_conta_e_por_data(tmp_path):
    day = {"v": "2026-09-28"}
    led = RiskLedger(tmp_path / "l.json", today=lambda: day["v"])
    led.record("PRACTICE", "WIN", 100.0)
    led.record("REAL", "LOSS", -5.0)
    assert led.get("PRACTICE").profit == 100.0
    assert led.get("REAL").profit == -5.0
    assert led.get("REAL").consecutive_losses == 1
    day["v"] = "2026-09-29"
    assert led.get("PRACTICE").profit == 0.0
    assert led.get("REAL").consecutive_losses == 0
    # persistencia: reiniciar o processo nao zera o dia
    day["v"] = "2026-09-28"
    led2 = RiskLedger(tmp_path / "l.json", today=lambda: day["v"])
    assert led2.get("PRACTICE").profit == 100.0
    assert led2.get("REAL").profit == -5.0


def test_lucro_na_pratica_nao_aumenta_limite_da_real(make_engine, cfg):
    iq = FakeIQ(server_ts=IN_WINDOW, allow_real=True)
    eng = make_engine(iq=iq, cfg_override=cfg.model_copy(update={"allow_real": True}),
                      settings=BotSettings(use_llm=False, amount=2, max_daily_loss=10))
    eng.ledger.record("PRACTICE", "WIN", 100.0)
    eng.ledger.record("REAL", "LOSS", -9.0)
    iq.connected = True
    eng.set_account("REAL", "REAL")
    assert eng._execute(signal()) is False  # -9 - 2 < -10, independente do +100 da pratica


def test_harvest_apura_win_e_loss(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW)
    eng = make_engine(iq=iq, settings=BotSettings(use_llm=False, max_open_trades=2))
    iq.buy_response = BuyResult("ok", 1, "ok")
    eng._execute(signal())
    iq.buy_response = BuyResult("ok", 2, "ok")
    eng._execute(signal())
    iq.results = {1: ("WIN", 1.7), 2: ("LOSS", -2.0)}
    eng._harvest()
    c = eng.ledger.get("PRACTICE")
    assert (c.wins, c.losses, c.profit, c.consecutive_losses) == (1, 1, -0.3, 1)
    assert eng.open == {}


def test_harvest_sem_resultado_no_prazo_conta_como_perda(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW)
    eng = make_engine(iq=iq)
    eng._execute(signal())
    rec = next(iter(eng.open.values()))
    iq.set_time(rec.expires_at + 30)
    eng._harvest()
    assert eng.open  # ainda dentro da folga
    iq.set_time(rec.expires_at + 91)
    eng._harvest()
    c = eng.ledger.get("PRACTICE")
    assert not eng.open
    assert c.errors == 1 and c.profit == -2.0 and c.consecutive_losses == 1


def test_ordem_incerta_para_o_motor_e_conta_perda(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW)
    iq.buy_response = BuyResult("uncertain", None, "timeout")
    eng = make_engine(iq=iq)
    assert eng._execute(signal()) is False
    assert eng._stop.is_set()
    assert eng.ledger.get("PRACTICE").profit == -2.0
    assert eng.uncertain_orders == 1


def test_expiracao_esperada():
    base = 1_790_000_000 - (1_790_000_000 % 60)
    assert expected_expiry(base + 55, 1) == base + 120  # faltam 5s -> vela seguinte inteira
    assert expected_expiry(base + 10, 1) == base + 60
    # regra da lib (buy_digital_spot_v2): agora+1m30s, primeiro minuto multiplo de 5
    exp5 = expected_expiry(base + 55, 5)
    assert (exp5 // 60) % 5 == 0
    assert exp5 == base + 120  # base e minuto %5 == 3 neste timestamp


# ---------------------------------------------------------------- M1 / M2 / M3
def test_janela_perdida_nao_envia(make_engine):
    iq = FakeIQ(server_ts=IN_WINDOW + 5)  # 0s restantes -> virou o minuto
    eng = make_engine(iq=iq)
    assert eng._execute(signal()) is False
    assert iq.buy_calls == []


def test_payout_zero_bloqueia(make_engine):
    iq = FakeIQ(payout=0.0)
    eng = make_engine(iq=iq)
    assert eng._analyze("EURUSD-OTC") is None
    assert iq.candle_calls == 0


def test_ia_nao_infla_confianca(make_engine):
    eng = make_engine(settings=BotSettings(use_llm=True))
    eng.health.ollama_ok = True
    eng.brain.decision = ("call", 0.2, "fraco", "fake", False)
    sig = eng._analyze("EURUSD-OTC")
    assert sig is not None and sig.confidence == pytest.approx(0.2)


# ---------------------------------------------------------------- M4
@pytest.mark.parametrize("d", [2, 3, 4])
def test_duration_so_1_ou_5(d):
    with pytest.raises(ValueError):
        BotSettings(duration_min=d)
    assert BotSettings(duration_min=5).duration_min == 5


# ---------------------------------------------------------------- M6
def test_snapshot_nao_faz_io(make_engine):
    iq = FakeIQ()

    def boom():
        raise AssertionError("snapshot nao pode chamar a corretora")

    iq.balance = boom
    iq.currency = boom
    iq.connected = True
    eng = make_engine(iq=iq)
    st = eng.snapshot()
    assert st.account == "PRACTICE"
    assert "@" in st.email_masked and "x@y.z" != st.email_masked
