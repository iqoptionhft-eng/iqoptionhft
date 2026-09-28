import threading
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

from app.iq_client import IQClient, call_with_timeout, BrokerTimeout


def nested():
    return defaultdict(dict)


def client_with(api):
    c = IQClient("x@y.z", "p")
    c.api = api
    c.connected = True
    return c


# ---------------------------------------------------------------- C1
def test_check_result_nao_bloqueia_quando_ordem_desconhecida():
    api = SimpleNamespace(api=SimpleNamespace(order_async=nested()))
    c = client_with(api)
    t0 = time.time()
    assert c.check_result("123") is None
    assert c.check_result(123) is None
    assert c.check_result(None) is None
    assert time.time() - t0 < 0.1
    # .get nao pode criar a chave no nested_dict
    assert 123 not in api.api.order_async


def test_check_result_ordem_aberta_retorna_none_sem_chamar_check_win_v4():
    store = nested()
    store[55]["position-changed"] = {"msg": {"status": "open"}}

    def boom(*a, **k):
        raise AssertionError("nao pode chamar metodos bloqueantes da lib")

    api = SimpleNamespace(api=SimpleNamespace(order_async=store), check_win_v4=boom, check_win_digital_v2=boom)
    assert client_with(api).check_result("55") is None


@pytest.mark.parametrize(
    "msg,expected",
    [
        ({"status": "closed", "close_reason": "expired", "close_profit": 1.7, "invest": 1.0}, ("WIN", 0.7)),
        ({"status": "closed", "close_reason": "expired", "close_profit": 0, "invest": 1.0}, ("LOSS", -1.0)),
        ({"status": "closed", "close_reason": "expired", "close_profit": 1.0, "invest": 1.0}, ("EQUAL", 0.0)),
        ({"status": "closed", "close_reason": "default", "pnl_realized": -0.4}, ("LOSS", -0.4)),
    ],
)
def test_check_result_apura_ordem_fechada_com_id_string(msg, expected):
    store = nested()
    store[77]["position-changed"] = {"msg": msg}
    api = SimpleNamespace(api=SimpleNamespace(order_async=store))
    assert client_with(api).check_result("77") == expected


def test_call_with_timeout_desiste():
    def forever():
        while True:
            time.sleep(0.01)

    t0 = time.time()
    with pytest.raises(BrokerTimeout):
        call_with_timeout(forever, 0.2)
    assert time.time() - t0 < 1.0


# ---------------------------------------------------------------- C2
class BuyApi:
    def __init__(self, response=None, block=False, exc=None):
        self.response = response
        self.block = block
        self.exc = exc
        self.legacy_calls = 0

    def buy_digital_spot_v2(self, asset, amount, action, duration):
        if self.exc:
            raise self.exc
        if self.block:
            threading.Event().wait(5)
        return self.response

    def buy_digital_spot(self, *a):
        self.legacy_calls += 1
        return (True, 1)

    def buy(self, *a):
        self.legacy_calls += 1
        return (True, 1)


@pytest.mark.parametrize(
    "response,status",
    [
        ((True, 123456), "ok"),
        ((False, {"message": "saldo insuficiente"}), "rejected"),
        ((True, "123"), "rejected"),
        ((True, True), "rejected"),
        ((-1, None), "rejected"),
        ((False, None), "rejected"),
    ],
)
def test_buy_so_aceita_true_int_e_sem_fallback(response, status):
    api = BuyApi(response)
    r = client_with(api).buy("EURUSD-OTC", 2, "call", 1)
    assert r.status == status
    if status == "ok":
        assert r.order_id == 123456 and isinstance(r.order_id, int)
    else:
        assert r.order_id is None
    assert api.legacy_calls == 0


def test_buy_timeout_vira_incerta():
    api = BuyApi(block=True)
    c = client_with(api)
    c.BUY_TIMEOUT = 0.2
    r = c.buy("EURUSD-OTC", 2, "put", 1)
    assert r.status == "uncertain"
    assert api.legacy_calls == 0


def test_buy_keyerror_do_ativo_e_recusa():
    r = client_with(BuyApi(exc=KeyError("XYZ"))).buy("XYZ", 2, "call", 1)
    assert r.status == "rejected"


@pytest.mark.parametrize("duration", [2, 3, 4, 15])
def test_buy_recusa_duracao_diferente_de_1_e_5(duration):
    api = BuyApi((True, 1))
    assert client_with(api).buy("EURUSD-OTC", 2, "call", duration).status == "rejected"


def test_change_account_real_bloqueada_sem_allow_real():
    c = client_with(SimpleNamespace(change_balance=lambda m: None, get_balance=lambda: 1.0))
    with pytest.raises(RuntimeError, match="ALLOW_REAL"):
        c.change_account("REAL")
    assert c.account == "PRACTICE"


def test_connect_concorrente_e_serializado():
    c = IQClient("x@y.z", "p")
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return True, "ok"

    c._connect_locked = slow  # type: ignore[method-assign]
    t = threading.Thread(target=c.connect)
    t.start()
    time.sleep(0.05)
    ok, why = c.connect(blocking=False)
    t.join()
    assert not ok and "andamento" in why
    assert len(calls) == 1
