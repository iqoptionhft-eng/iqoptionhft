from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

from .iq_login import fetch_ssid
from .models import AccountMode, Candle


class IQClient:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.api: Any = None
        self.connected = False
        self.account: AccountMode = "PRACTICE"

    def connect(self) -> tuple[bool, str]:
        if not self.email or not self.password:
            return False, "Credenciais vazias. Confira o arquivo .env na pasta do projeto."

        try:
            from iqbroker.stable_api import IQ_Option
            import iqbroker.global_value as global_value
            from iqbroker.api import IQOptionAPI
        except Exception as exc:
            return False, f"Biblioteca IQ Option nao instalada: {exc}"

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

        self.api = IQ_Option(self.email, self.password, active_account_type="PRACTICE")
        self.api.SESSION_COOKIE = {"ssid": ssid}
        try:
            self.api.set_max_reconnect(5)
        except Exception:
            pass

        box: dict[str, Any] = {}

        def _run() -> None:
            try:
                box["result"] = self.api.connect()
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(45)
        if worker.is_alive():
            self.connected = False
            return False, "Timeout de 45s na conexao websocket da IQ Option."
        if "error" in box:
            self.connected = False
            return False, self._format_reason(box["error"])

        result = box.get("result")
        if isinstance(result, tuple):
            status, reason = result[0], result[1] if len(result) > 1 else None
        else:
            status, reason = bool(result), None
        if not status:
            self.connected = False
            return False, self._format_reason(reason)

        self.connected = True
        try:
            self.change_account(self.account)
        except Exception as exc:
            return False, f"Logou, mas nao conseguiu selecionar a conta pratica/real: {exc}"
        return True, "ok"

    def change_account(self, mode: AccountMode) -> float:
        if not self.api:
            raise RuntimeError("Nao conectado")
        try:
            self.api.change_balance(mode)
        except SystemExit as exc:
            raise RuntimeError(f"Modo de conta invalido: {mode}") from exc
        self.account = mode
        time.sleep(0.4)
        return self.balance()

    def balance(self) -> float:
        if not self.api:
            return 0.0
        try:
            return float(self.api.get_balance() or 0.0)
        except Exception:
            return 0.0

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

    def currency(self) -> str:
        try:
            fn = getattr(self.api, "get_currency", None)
            if callable(fn):
                return str(fn())
        except Exception:
            pass
        return "USD"

    def open_assets(self, preferred: list[str]) -> list[str]:
        opened: list[str] = []
        try:
            all_open = self.api.get_all_open_time()
        except Exception:
            return preferred
        for name in preferred:
            for kind in ("digital", "turbo", "binary"):
                try:
                    if all_open.get(kind, {}).get(name, {}).get("open"):
                        opened.append(name)
                        break
                except Exception:
                    continue
        if opened:
            return list(dict.fromkeys(opened))
        try:
            digital = all_open.get("digital", {})
            for name, meta in digital.items():
                if meta.get("open") and ("OTC" in name or name in preferred):
                    opened.append(name)
                if len(opened) >= 6:
                    break
        except Exception:
            pass
        return opened or preferred

    def payout(self, asset: str) -> float:
        best = 0.0
        try:
            best = max(best, float(self.api.get_digital_payout(asset) or 0))
        except Exception:
            pass
        try:
            profits = self.api.get_all_profit()
            for kind in ("turbo", "binary"):
                p = profits.get(asset, {}).get(kind)
                if p:
                    best = max(best, float(p) * 100 if p <= 1 else float(p))
        except Exception:
            pass
        return best

    def candles(self, asset: str, size: int = 60, count: int = 80) -> list[Candle]:
        raw = self.api.get_candles(asset, size, count, time.time())
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

    def buy(self, asset: str, amount: float, direction: str, duration: int) -> tuple[bool, str]:
        action = direction.upper()
        try:
            status, order_id = self.api.buy_digital_spot_v2(asset, amount, action, duration)
            if status and order_id:
                return True, str(order_id)
        except Exception:
            pass
        try:
            order_id = self.api.buy_digital_spot(asset, amount, action, duration)
            if order_id:
                return True, str(order_id)
        except Exception:
            pass
        try:
            check, order_id = self.api.buy(amount, asset, action, duration)
            if check:
                return True, str(order_id)
        except Exception as exc:
            return False, str(exc)
        return False, "ordem rejeitada"

    def check_result(self, order_id: str) -> tuple[str, float] | None:
        try:
            done, profit = self.api.check_win_digital_v2(order_id)
            if done:
                profit = float(profit or 0)
                if profit > 0:
                    return "WIN", profit
                if profit < 0:
                    return "LOSS", profit
                return "EQUAL", 0.0
        except Exception:
            pass
        try:
            done, profit = self.api.check_win_v4(order_id)
            if done:
                profit = float(profit or 0)
                if profit > 0:
                    return "WIN", profit
                if profit < 0:
                    return "LOSS", profit
                return "EQUAL", 0.0
        except Exception:
            pass
        return None

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
