from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Literal, NamedTuple

from .iq_login import fetch_ssid
from .models import AccountMode, Candle


class BrokerTimeout(Exception):
    """Chamada a corretora excedeu o prazo (a lib iqbroker espera em laco sem timeout)."""


def call_with_timeout(fn: Callable[..., Any], timeout: float, *args: Any, **kwargs: Any) -> Any:
    """Executa fn numa thread daemon e desiste apos `timeout` segundos (C1/M6).

    A lib usa busy-wait sem prazo em varias funcoes; aqui o chamador nunca fica preso.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # inclui SystemExit disparado pela lib
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True, name=f"iq-call-{getattr(fn, '__name__', 'fn')}")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise BrokerTimeout(f"{getattr(fn, '__name__', 'chamada')} excedeu {timeout:.0f}s")
    if "error" in box:
        err = box["error"]
        if isinstance(err, SystemExit):
            raise RuntimeError(f"lib iqbroker chamou exit(): {err}") from err
        raise err
    return box.get("result")


class BuyResult(NamedTuple):
    status: Literal["ok", "rejected", "uncertain"]
    order_id: int | None
    detail: str


class IQClient:
    BUY_TIMEOUT = 12.0
    CALL_TIMEOUT = 8.0

    def __init__(self, email: str, password: str, allow_real: bool = False) -> None:
        self.email = email
        self.password = password
        self.allow_real = bool(allow_real)
        self.api: Any = None
        self.connected = False
        self.account: AccountMode = "PRACTICE"
        # Conhecido #3: uma unica conexao/reconexao por vez.
        self._conn_lock = threading.Lock()

    # ------------------------------------------------------------------ conexao
    def connect(self, blocking: bool = True) -> tuple[bool, str]:
        if not self._conn_lock.acquire(blocking=blocking):
            return False, "conexao ja em andamento"
        try:
            return self._connect_locked()
        finally:
            self._conn_lock.release()

    def _connect_locked(self) -> tuple[bool, str]:
        if not self.email or not self.password:
            return False, "Credenciais vazias. Confira o arquivo .env na pasta do projeto."

        try:
            from iqbroker.stable_api import IQ_Option
            import iqbroker.global_value as global_value
            from iqbroker.api import IQOptionAPI
        except Exception as exc:
            return False, f"Biblioteca iqbroker nao instalada: {exc}"

        ssid, login_error = fetch_ssid(self.email, self.password)
        if not ssid:
            return False, f"Login IQ Option falhou: {login_error}"

        global_value.SSID = ssid
        dummy = SimpleNamespace(
            cookies={"ssid": ssid},
            text=json.dumps({"data": {"ssid": ssid}}),
            status_code=200,
        )
        dummy.json = lambda: {"data": {"ssid": ssid}}  # type: ignore[method-assign]
        IQOptionAPI.get_ssid = lambda _self, _dummy=dummy: _dummy  # type: ignore[method-assign]
        _patch_ssid_timeout(IQOptionAPI, global_value)

        # Sempre abre em PRACTICE; so depois aplica a conta atual (validada por change_account).
        api = IQ_Option(self.email, self.password, active_account_type="PRACTICE")
        api.SESSION_COOKIE = {"ssid": ssid}
        try:
            api.set_max_reconnect(5)
        except Exception:
            pass

        try:
            result = call_with_timeout(api.connect, 45.0)
        except BrokerTimeout:
            self.connected = False
            return False, "Timeout de 45s na conexao websocket da IQ Option."
        except Exception as exc:
            self.connected = False
            return False, self._format_reason(exc)

        if isinstance(result, tuple):
            status, reason = result[0], result[1] if len(result) > 1 else None
        else:
            status, reason = bool(result), None
        if not status:
            self.connected = False
            return False, self._format_reason(reason)

        self.api = api
        self.connected = True
        try:
            self.change_account(self.account)
        except Exception as exc:
            self.connected = False
            return False, f"Logou, mas nao conseguiu selecionar a conta {self.account}: {exc}"
        return True, "ok"

    def change_account(self, mode: AccountMode) -> float | None:
        if mode not in ("PRACTICE", "REAL"):
            raise RuntimeError(f"Modo de conta invalido: {mode}")
        if mode == "REAL" and not self.allow_real:
            raise RuntimeError("Conta REAL bloqueada (ALLOW_REAL=0).")
        if not self.api:
            raise RuntimeError("Nao conectado")
        call_with_timeout(self.api.change_balance, 15.0, mode)
        self.account = mode
        time.sleep(0.4)
        return self.balance()

    def balance(self) -> float | None:
        """Saldo atual ou None se nao foi possivel obter (nunca 0.0 falso)."""
        if not self.api:
            return None
        try:
            value = call_with_timeout(self.api.get_balance, 5.0)
            return None if value is None else float(value)
        except Exception:
            return None

    def check_connect(self) -> bool:
        if not self.api:
            self.connected = False
            return False
        try:
            ok = bool(self.api.check_connect())
        except Exception:
            ok = False
        self.connected = ok
        return ok

    def currency(self) -> str | None:
        fn = getattr(self.api, "get_currency", None)
        if not callable(fn):
            return None
        try:
            value = call_with_timeout(fn, 5.0)
            return str(value) if value else None
        except Exception:
            return None

    def server_time(self) -> float:
        try:
            ts = float(self.api.api.timesync.server_timestamp)
            if ts > 1e12:  # milissegundos
                ts /= 1000.0
            if ts > 1e9:
                return ts
        except Exception:
            pass
        return time.time()

    # --------------------------------------------------------------- mercado
    def open_assets(self, preferred: list[str]) -> list[str]:
        """Ativos preferidos que estao abertos em DIGITAL (unico produto negociado)."""
        try:
            all_open = call_with_timeout(self.api.get_all_open_time, 15.0)
        except Exception:
            return []
        opened: list[str] = []
        for name in preferred:
            try:
                if all_open.get("digital", {}).get(name, {}).get("open"):
                    opened.append(name)
            except Exception:
                continue
        return list(dict.fromkeys(opened))

    def payout(self, asset: str) -> float:
        """M2: somente o payout DIGITAL do ativo. 0.0 = desconhecido (o motor bloqueia)."""
        try:
            value = call_with_timeout(self.api.get_digital_payout, 5.0, asset, 2)
            return float(value or 0.0)
        except Exception:
            return 0.0

    def candles(self, asset: str, size: int = 60, count: int = 80) -> list[Candle]:
        raw = call_with_timeout(self.api.get_candles, self.CALL_TIMEOUT, asset, size, count, time.time())
        out: list[Candle] = []
        for row in raw or []:
            out.append(
                Candle(
                    ts=int(row.get("from") or row.get("to") or 0),
                    open=float(row["open"]),
                    high=float(row.get("max", row.get("high"))),
                    low=float(row.get("min", row.get("low"))),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
        return out

    # ---------------------------------------------------------------- ordens
    def buy(self, asset: str, amount: float, direction: str, duration: int) -> BuyResult:
        """C2: um unico metodo (digital v2), sem fallback para outros produtos.

        ok        -> a corretora devolveu (True, <int>)
        rejected  -> resposta explicita de recusa (nada foi aberto)
        uncertain -> timeout/erro depois do envio: a ordem PODE existir
        """
        if duration not in (1, 5):
            return BuyResult("rejected", None, f"duracao {duration} nao suportada (use 1 ou 5)")
        action = direction.lower()
        if action not in ("call", "put"):
            return BuyResult("rejected", None, f"direcao invalida: {direction}")
        if not self.api:
            return BuyResult("rejected", None, "nao conectado")
        try:
            resp = call_with_timeout(self.api.buy_digital_spot_v2, self.BUY_TIMEOUT, asset, amount, action, duration)
        except BrokerTimeout as exc:
            return BuyResult("uncertain", None, f"sem resposta da corretora: {exc}")
        except KeyError as exc:
            # KeyError so ocorre na busca do ativo (antes do envio)
            return BuyResult("rejected", None, f"ativo desconhecido na lib: {exc}")
        except Exception as exc:
            return BuyResult("uncertain", None, f"erro apos envio possivel: {exc}")
        if not isinstance(resp, tuple) or len(resp) != 2:
            return BuyResult("uncertain", None, f"resposta inesperada: {resp!r}"[:300])
        status, order_id = resp
        if status is True and isinstance(order_id, int) and not isinstance(order_id, bool):
            return BuyResult("ok", order_id, "ok")
        detail = order_id.get("message") if isinstance(order_id, dict) else order_id
        return BuyResult("rejected", None, f"ordem recusada: {detail!r}"[:300])

    def check_result(self, order_id: int | str | None) -> tuple[str, float] | None:
        """C1: leitura NAO bloqueante do estado da ordem digital.

        Nao usa check_win_digital_v2/check_win_v4 (busy-wait infinito na lib).
        """
        if order_id is None:
            return None
        try:
            key = int(order_id)
        except (TypeError, ValueError):
            return None
        inner = getattr(self.api, "api", None)
        store = getattr(inner, "order_async", None)
        if store is None:
            return None
        try:
            entry = store.get(key)  # .get nao cria chave no nested_dict
        except Exception:
            return None
        if not entry:
            return None
        message = entry.get("position-changed") if isinstance(entry, dict) else None
        msg = message.get("msg") if isinstance(message, dict) else None
        if not isinstance(msg, dict) or msg.get("status") != "closed":
            return None
        try:
            reason = msg.get("close_reason")
            if reason == "expired":
                pnl = float(msg.get("close_profit") or 0) - float(msg.get("invest") or 0)
            elif msg.get("pnl_realized") is not None:
                pnl = float(msg.get("pnl_realized") or 0)
            else:
                pnl = float(msg.get("close_profit") or 0) - float(msg.get("invest") or 0)
        except (TypeError, ValueError):
            return None
        pnl = round(pnl, 2)
        if pnl > 0:
            return "WIN", pnl
        if pnl < 0:
            return "LOSS", pnl
        return "EQUAL", 0.0

    @staticmethod
    def _format_reason(reason: Any) -> str:
        if reason is None:
            return "Falha ao conectar na IQ Option"
        if isinstance(reason, Exception):
            text = str(reason)
        else:
            text = str(reason).strip()
        if text.lower() == "2fa":
            return "A conta esta com 2FA. Desative o SMS/2FA na IQ Option para o robo logar."
        if text.startswith("{") or text.startswith("["):
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    if payload.get("message"):
                        return str(payload["message"])
                    if payload.get("code"):
                        return str(payload["code"])
            except Exception:
                pass
        if "<html" in text.lower():
            return "Endpoint antigo de login da IQ Option recusou a sessao."
        return text[:400]


def _patch_ssid_timeout(api_cls: Any, global_value: Any) -> None:
    if getattr(api_cls.send_ssid, "_hft_patched", False):
        return
    original = api_cls.send_ssid

    def send_ssid(self):
        self.profile.msg = None
        self.ssid(global_value.SSID)
        started = time.time()
        while self.profile.msg is None and time.time() - started < 20:
            time.sleep(0.05)
        if self.profile.msg is None:
            return False
        return bool(self.profile.msg)

    send_ssid._hft_patched = True  # type: ignore[attr-defined]
    try:
        api_cls.send_ssid = send_ssid
    except Exception:
        api_cls.send_ssid = original
