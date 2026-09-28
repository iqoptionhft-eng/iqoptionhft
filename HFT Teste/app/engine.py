from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Deque

from .backtest import run_ta_backtest
from .config import Settings, get_settings
from .features import expected_expiry, extract_features, seconds_to_next_minute, ta_confidence
from .iq_client import IQClient
from .memory import ExperienceMemory, get_memory
from .models import (
    AccountMode,
    BacktestResult,
    BotSettings,
    DecisionLog,
    Experience,
    HealthStatus,
    LogEvent,
    RuntimeState,
    Signal,
    TradeRecord,
)
from .ollama_brain import OllamaBrain
from .risk import RiskLedger, can_open

# folga apos a expiracao esperada antes de declarar o resultado como nao apurado
HARVEST_GRACE_SEC = 90.0
PAYOUT_CACHE_SEC = 60.0


class EngineBusy(RuntimeError):
    """Operacao recusada porque outra (start/conexao) esta em andamento."""


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    user, dom = email.split("@", 1)
    return f"{user[:1]}***@{dom[:1]}***"


class TradingEngine:
    def __init__(
        self,
        *,
        iq: Any = None,
        brain: Any = None,
        store: ExperienceMemory | None = None,
        ledger: RiskLedger | None = None,
        cfg: Settings | None = None,
        settings: BotSettings | None = None,
        start_health: bool = True,
    ) -> None:
        self.cfg = cfg or get_settings()
        self._lock = threading.RLock()  # estado compartilhado (A1/M6)
        self._start_lock = threading.Lock()  # start/stop/troca de conta
        self._order_lock = threading.Lock()  # envio de ordem serializado
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stop_gen = 0
        self.logs: Deque[LogEvent] = deque(maxlen=300)
        self.decisions: Deque[DecisionLog] = deque(maxlen=200)
        self.settings = settings or BotSettings(
            ollama_model=self.cfg.ollama_model,
            ollama_fast_model=self.cfg.ollama_fast_model,
        )
        self.allow_real = bool(self.cfg.allow_real)
        # M7: sempre inicia em PRACTICE, qualquer que seja DEFAULT_ACCOUNT.
        self.account: AccountMode = "PRACTICE"
        if self.cfg.default_account != "PRACTICE":
            self.log(
                f"DEFAULT_ACCOUNT={self.cfg.default_account} ignorado: o robo sempre inicia em PRACTICE.",
                "warn",
            )
        self.iq = iq or IQClient(self.cfg.iq_email, self.cfg.iq_password, allow_real=self.allow_real)
        self.memory = store or get_memory()
        self.brain = brain or OllamaBrain(store=self.memory)
        self.brain.set_models(self.settings.ollama_model, self.settings.ollama_fast_model)
        self.ledger = ledger or RiskLedger(Path(self.cfg.data_dir) / "ledger.json")
        self.trades: list[TradeRecord] = []
        self.open: dict[str, TradeRecord] = {}
        self.uncertain_orders = 0
        self.status = "idle"
        self.last_error = ""
        self.last_signal = ""
        self.last_trade_ts = 0.0
        self.last_tick_ts = 0.0
        self.health = HealthStatus()
        self.last_backtest: BacktestResult | None = None
        self._cached_balance = 0.0
        self._cached_currency = "USD"
        self._last_asset_try: dict[str, float] = {}
        self._payout_cache: dict[str, tuple[float, float]] = {}
        self._health_thread: threading.Thread | None = None
        if start_health:
            self._health_thread = threading.Thread(target=self._health_loop, daemon=True, name="hft-health")
            self._health_thread.start()

    # ------------------------------------------------------------- utilidades
    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.logs.appendleft(LogEvent(ts=datetime.now(), level=level, message=message))

    def decide_log(
        self,
        asset: str,
        action: str,
        detail: str,
        *,
        ta_direction: str = "",
        ta_score: float = 0.0,
        llm_outcome: str = "",
        used_model: str = "",
        fallback_used: bool = False,
        level: str = "info",
    ) -> None:
        item = DecisionLog(
            ts=datetime.now(),
            asset=asset,
            action=action,
            detail=detail,
            ta_direction=ta_direction,
            ta_score=ta_score,
            llm_outcome=llm_outcome,
            used_model=used_model,
            fallback_used=fallback_used,
        )
        with self._lock:
            self.decisions.appendleft(item)
        self.log(f"[{asset}] {action}: {detail}", level)

    def open_exposure(self, account: AccountMode | None = None) -> float:
        acct = account or self.account
        with self._lock:
            return round(sum(r.amount for r in self.open.values() if r.account == acct), 2)

    def _thread_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> RuntimeState:
        # M6: nenhuma chamada de rede aqui; saldo vem do cache atualizado pela thread de saude.
        with self._lock:
            counters = self.ledger.get(self.account)
            hb = (time.time() - self.last_tick_ts) if self.last_tick_ts and self._thread_alive() else None
            health = self.health.model_copy(update={"engine_heartbeat_age_sec": hb})
            return RuntimeState(
                status=self.status,  # type: ignore[arg-type]
                connected=self.iq.connected,
                account=self.account,
                allow_real=self.allow_real,
                email_masked=_mask_email(self.cfg.iq_email),
                balance=self._cached_balance,
                currency=self._cached_currency,
                ollama_ok=self.health.ollama_ok,
                ollama_model=self.settings.ollama_model,
                last_error=self.last_error,
                wins=counters.wins,
                losses=counters.losses,
                equals=counters.equals,
                errors=counters.errors,
                profit_today=counters.profit,
                trading_date=counters.date,
                consecutive_losses=counters.consecutive_losses,
                open_trades=len(self.open),
                open_exposure=self.open_exposure(),
                uncertain_orders=self.uncertain_orders,
                last_signal=self.last_signal,
                settings=self.settings,
                trades=[t.model_copy() for t in reversed(self.trades[-40:])],
                logs=list(self.logs)[:80],
                decisions=list(self.decisions)[:60],
                health=health,
                memory=self.memory.stats(),
                ollama_models=list(self.brain.last_models or self.health.models),
                last_backtest=self.last_backtest,
            )

    def update_settings(self, data: dict) -> BotSettings:
        if "account" in data:
            # Conhecido #1: conta so muda por /api/account (com confirmacao e trava).
            raise ValueError("A conta nao pode ser alterada por /api/settings. Use /api/account.")
        with self._lock:
            merged = self.settings.model_dump()
            merged.update({k: v for k, v in data.items() if v is not None})
            new = BotSettings(**merged)  # valida (inclusive amount <= max_daily_loss)
            self.settings = new
            self.brain.set_models(new.ollama_model, new.ollama_fast_model)
        self.log(
            f"Parametros: valor={new.amount} duracao={new.duration_min}m perda_max={new.max_daily_loss} "
            f"modelo={new.ollama_model} rapido={new.ollama_fast_model or 'nenhum'} fallback={new.fallback_mode}"
        )
        threading.Thread(target=self._refresh_health, daemon=True).start()
        return new

    # ------------------------------------------------------- conexao / conta
    def connect(self) -> tuple[bool, str]:
        with self._lock:
            if self.status not in ("running", "starting", "stopping"):
                self.status = "connecting"
            self.last_error = ""
        self.log("Conectando na IQ Option...")
        try:
            ok, reason = self.iq.connect()
        except Exception as exc:
            ok, reason = False, str(exc)
        if not ok:
            with self._lock:
                if self.status == "connecting":
                    self.status = "error"
                self.last_error = reason
                self.health.iq_ok = False
                self.health.iq_detail = reason
            self.log(reason, "error")
            return False, reason
        try:
            if self.iq.account != self.account:
                self.iq.change_account(self.account)
        except Exception as exc:
            with self._lock:
                self.status = "error"
                self.last_error = f"conta: {exc}"
            return False, str(exc)
        with self._lock:
            if self.status == "connecting":
                self.status = "idle"
            self.last_error = ""
        self._refresh_balance()
        self.log(f"Conectado. Conta {self.account}. Saldo {self._cached_balance:.2f}")
        self._refresh_health()
        if self.health.ollama_ok and self.settings.use_llm:
            threading.Thread(target=self.brain.warmup, args=(self.settings.ollama_model,), daemon=True).start()
        return True, "ok"

    def set_account(self, mode: AccountMode, confirm: str = "") -> float:
        """A3 + trava ALLOW_REAL: troca so com motor parado e sem ordens abertas."""
        if mode not in ("PRACTICE", "REAL"):
            raise ValueError(f"conta invalida: {mode}")
        if mode == "REAL":
            if not self.allow_real:
                raise PermissionError("Conta REAL bloqueada: ALLOW_REAL=0 no .env.")
            if confirm.strip().upper() != "REAL":
                raise PermissionError("Para conta REAL, confirme digitando REAL.")
        if not self._start_lock.acquire(blocking=False):
            raise EngineBusy("Start em andamento; tente de novo depois.")
        try:
            with self._lock:
                if self._thread_alive() or self.status in ("running", "starting", "stopping"):
                    raise EngineBusy("Pare o motor antes de trocar de conta.")
                if self.open:
                    raise EngineBusy(f"Existem {len(self.open)} ordens abertas; aguarde a apuracao.")
            bal = self.iq.change_account(mode)
            with self._lock:
                self.account = mode
            self._refresh_balance()
            self.log(f"Conta alterada para {mode}. Saldo {float(bal or 0):.2f}", "warn" if mode == "REAL" else "info")
            return float(bal or 0.0)
        finally:
            self._start_lock.release()

    # ------------------------------------------------------------ start/stop
    def start(self) -> None:
        # A1: start nunca roda em paralelo; o segundo clique recebe EngineBusy.
        if not self._start_lock.acquire(blocking=False):
            raise EngineBusy("Start ja em andamento.")
        try:
            with self._lock:
                if self._thread_alive():
                    if self._stop.is_set():
                        self._stop.clear()
                        self.status = "running"
                        self.log("Motor retomado")
                    return
                gen = self._stop_gen
                prev_status = self.status
                self.status = "starting"
            try:
                self._refresh_health()
                if not self.iq.connected:
                    ok, reason = self.connect()
                    if not ok:
                        raise RuntimeError(reason)
                    self._refresh_health()
                if not self.health.iq_ok:
                    raise RuntimeError(f"IQ Option nao saudavel: {self.health.iq_detail}")
                if self.settings.use_llm and not self.health.ollama_ok and self.settings.fallback_mode == "skip":
                    raise RuntimeError("Ollama offline e fallback=skip. Ligue o Ollama ou mude o fallback.")
                with self._lock:
                    if gen != self._stop_gen:
                        self.status = "paused"
                        raise RuntimeError("Parado durante a inicializacao.")
                    self._stop.clear()
                    self.status = "running"
                    self._thread = threading.Thread(target=self._loop, daemon=True, name="hft-engine")
                    self._thread.start()
            except Exception:
                with self._lock:
                    if self.status == "starting":
                        self.status = "error" if prev_status != "paused" else "paused"
                raise
            self.log(f"Motor iniciado (conta {self.account})")
        finally:
            self._start_lock.release()

    def stop(self, reason: str = "Motor pausado") -> None:
        with self._lock:
            self._stop_gen += 1
            self._stop.set()
            if self._thread_alive() and self.open:
                self.status = "stopping"
                reason += f" (aguardando apuracao de {len(self.open)} ordem(ns))"
            elif self.status != "error":
                self.status = "paused"
        self.log(reason, "warn")

    # --------------------------------------------------------------- backtest
    def run_backtest(self, asset: str | None = None) -> BacktestResult:
        s = self.settings
        target = (asset or (s.assets[0] if s.assets else "EURUSD-OTC")).strip()
        if not self.iq.connected:
            raise RuntimeError("Conecte na IQ Option para baixar velas historicas.")
        candles = self.iq.candles(target, 60, 400)
        payout = self.iq.payout(target)
        result = run_ta_backtest(
            target,
            candles,
            self.memory,
            min_confidence=s.min_confidence,
            payout=payout,
            use_memory_filter=s.memory_enabled,
        )
        with self._lock:
            self.last_backtest = result
        self.log(
            f"Backtest {target}: {result.taken} entradas, wr {result.win_rate:.0%} "
            f"(empate {result.breakeven_win_rate:.0%}), EV/unid {result.expected_value_per_unit:+.3f}"
        )
        return result

    # ------------------------------------------------------------------ saude
    def _health_loop(self) -> None:
        while True:
            try:
                self._refresh_health()
                if self.iq.connected:
                    self._refresh_balance()
            except Exception:
                pass
            time.sleep(8)

    def _refresh_balance(self) -> None:
        bal = self.iq.balance()
        cur = self.iq.currency()
        with self._lock:
            if bal is not None:
                self._cached_balance = bal
            if cur:
                self._cached_currency = cur

    def _refresh_health(self) -> None:
        iq_ok = False
        iq_detail = "desconectado"
        if self.iq.connected or self.iq.api:
            iq_ok = self.iq.check_connect()
            iq_detail = "websocket ok" if iq_ok else "websocket caiu"
            if not iq_ok and self.status in ("running", "stopping"):
                self.log("IQ Option desconectou. Tentando reconectar...", "warn")
                # Conhecido #3: nao bloqueia; se ja ha reconexao em andamento, espera a proxima rodada.
                ok, reason = self.iq.connect(blocking=False)
                iq_ok = ok
                iq_detail = "reconectado" if ok else reason
                if not ok:
                    with self._lock:
                        self.last_error = reason
        ollama_ok, ollama_detail, models = self.brain.health(self.settings.ollama_model)
        if self.settings.ollama_fast_model:
            fast_ok, fast_detail, _ = self.brain.health(self.settings.ollama_fast_model)
            if not fast_ok:
                ollama_detail += f" | rapido: {fast_detail}"
        ready = iq_ok and (ollama_ok or not self.settings.use_llm or self.settings.fallback_mode != "skip")
        with self._lock:
            self.health = HealthStatus(
                iq_ok=iq_ok,
                iq_detail=iq_detail,
                ollama_ok=ollama_ok,
                ollama_detail=ollama_detail,
                models=models,
                checked_at=datetime.now(),
                ready_to_trade=ready,
            )

    # ------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while True:
            stopping = self._stop.is_set()
            with self._lock:
                has_open = bool(self.open)
            if stopping and not has_open:
                break
            try:
                self._tick(trade=not stopping)
            except Exception as exc:
                with self._lock:
                    self.last_error = str(exc)
                self.log(f"Erro no ciclo: {exc}", "error")
                time.sleep(1.0)
            self.last_tick_ts = time.time()
            time.sleep(self.settings.poll_interval_sec)
        with self._lock:
            if self.status in ("running", "stopping"):
                self.status = "paused"
        self.log("Motor parado (thread encerrada)")

    def _entry_window(self, s: BotSettings) -> tuple[bool, float]:
        remain = seconds_to_next_minute(self.iq.server_time())
        return (s.min_seconds_left <= remain <= s.enter_last_seconds), remain

    def _risk_check(self, s: BotSettings) -> tuple[bool, str]:
        with self._lock:
            counters = self.ledger.get(self.account)
            return can_open(
                counters,
                open_exposure=self.open_exposure(),
                amount=s.amount,
                max_daily_loss=s.max_daily_loss,
                max_consecutive_losses=s.max_consecutive_losses,
            )

    def _tick(self, trade: bool = True) -> None:
        self._harvest()
        if not trade or self._stop.is_set():
            return
        s = self.settings  # A4: parametros fixos durante todo o ciclo
        if not self.health.iq_ok:
            return
        ok, why = self._risk_check(s)
        if not ok:
            if not self.open:
                self.stop(f"Protecao de banca: {why}. Pausando.")
            return
        with self._lock:
            if len(self.open) >= s.max_open_trades:
                return
        if time.time() - self.last_trade_ts < s.cooldown_sec:
            return

        in_window, remain = self._entry_window(s)
        if not in_window:
            if remain > s.enter_last_seconds + 5:
                self._prefetch_payouts(s)
            return

        assets = self.iq.open_assets(s.assets)
        best: Signal | None = None
        for asset in assets:
            # M1: rechecagem da janela antes de cada ativo (LLM pode demorar)
            if not self._entry_window(s)[0] or self._stop.is_set():
                break
            if time.time() - self._last_asset_try.get(asset, 0) < 3:
                continue
            try:
                sig = self._analyze(asset, s)
            except Exception as exc:
                self.decide_log(asset, "skip", f"erro na analise: {exc}", level="warn")
                sig = None
            self._last_asset_try[asset] = time.time()
            if not sig:
                continue
            if best is None or sig.confidence > best.confidence:
                best = sig

        if best and best.confidence >= s.min_confidence:
            self._execute(best, s)

    def _prefetch_payouts(self, s: BotSettings) -> None:
        now = time.time()
        for asset in s.assets:
            ts, _ = self._payout_cache.get(asset, (0.0, 0.0))
            if now - ts > PAYOUT_CACHE_SEC / 2:
                self._payout_cache[asset] = (now, self.iq.payout(asset))
                return  # um por ciclo para nao atrasar o loop

    def _payout(self, asset: str) -> float:
        ts, value = self._payout_cache.get(asset, (0.0, 0.0))
        if time.time() - ts <= PAYOUT_CACHE_SEC and value > 0:
            return value
        value = self.iq.payout(asset)
        self._payout_cache[asset] = (time.time(), value)
        return value

    def _analyze(self, asset: str, s: BotSettings | None = None) -> Signal | None:
        s = s or self.settings
        payout = self._payout(asset)
        # M2: payout desconhecido (0) tambem bloqueia
        if not payout or payout < s.min_payout:
            self.decide_log(
                asset, "skip", f"payout {payout:.0f}% desconhecido ou abaixo do minimo {s.min_payout:.0f}%"
            )
            return None
        candles = self.iq.candles(asset, 60, 80)
        if len(candles) < 30:
            return None
        feat = extract_features(asset, candles)
        if feat.ta_direction is None:
            self.decide_log(asset, "skip", f"TA neutra ({feat.ta_reason})", ta_score=feat.ta_score)
            return None

        direction = feat.ta_direction
        confidence = ta_confidence(feat.ta_score)
        source = "TA"
        reason = feat.ta_reason
        llm_action = ""
        llm_reason = ""
        llm_outcome = "unused"
        used_model = ""
        memory_hint = ""

        if s.memory_enabled:
            rate, n, hint = self.memory.similar_win_rate(feat, direction)
            memory_hint = hint
            if rate is not None and n >= 4 and rate < 0.38:
                self.decide_log(
                    asset,
                    "recusa",
                    f"TA {direction.upper()} recusada pela memoria: {hint}",
                    ta_direction=direction,
                    ta_score=feat.ta_score,
                    llm_outcome="unused",
                    level="warn",
                )
                self.last_signal = f"{asset} bloqueado pela memoria"
                return None

        if s.use_llm:
            if not self.health.ollama_ok:
                llm_reason = self.health.ollama_detail
                return self._apply_fallback(feat, direction, confidence, source, reason, llm_reason, memory_hint, payout, s=s)
            tail = [c.model_dump() for c in candles]
            llm_dir, llm_conf, llm_reason, used_model, timed_out = self.brain.decide(
                feat,
                tail,
                timeout=s.llm_timeout_sec,
                use_fast_first=s.use_fast_model_first,
            )
            llm_action = llm_dir or "skip"
            if timed_out:
                return self._apply_fallback(
                    feat, direction, confidence, source, reason, llm_reason, memory_hint, payout, used_model, s=s
                )
            if llm_dir is None:
                llm_outcome = "skip"
                self.decide_log(
                    asset,
                    "recusa",
                    f"TA indicou {direction.upper()} ({feat.ta_reason}), mas a IA recusou: {llm_reason}",
                    ta_direction=direction,
                    ta_score=feat.ta_score,
                    llm_outcome=llm_outcome,
                    used_model=used_model,
                    level="warn",
                )
                self.last_signal = f"{asset} IA recusou ({llm_reason})"
                return None
            if llm_dir != direction:
                llm_outcome = "reject"
                self.decide_log(
                    asset,
                    "recusa",
                    f"TA indicou {direction.upper()} (RSI {feat.rsi:.0f}, {feat.ta_reason}), "
                    f"mas {used_model} divergiu ({llm_dir.upper()}): {llm_reason}",
                    ta_direction=direction,
                    ta_score=feat.ta_score,
                    llm_outcome=llm_outcome,
                    used_model=used_model,
                    level="warn",
                )
                self.last_signal = f"{asset} conflito TA={direction} IA={llm_dir}"
                return None
            llm_outcome = "agree"
            # M3: a IA so pode reduzir a confianca, nunca inflar um sinal fraco da TA.
            confidence = min(confidence, float(llm_conf))
            source = "TA+IA"
            reason = f"{feat.ta_reason} | IA concordou ({used_model}, conf {llm_conf:.2f}): {llm_reason}"
            self.decide_log(
                asset,
                "aceite",
                f"TA {direction.upper()} e IA concordaram (conf final {confidence:.2f}). {reason}. {memory_hint}",
                ta_direction=direction,
                ta_score=feat.ta_score,
                llm_outcome=llm_outcome,
                used_model=used_model,
            )
        else:
            self.decide_log(
                asset,
                "aceite",
                f"IA desligada. TA {direction.upper()} ({feat.ta_reason}), conf {confidence:.2f}. {memory_hint}",
                ta_direction=direction,
                ta_score=feat.ta_score,
                llm_outcome="unused",
            )

        self.last_signal = f"{asset} {direction.upper()} {confidence:.0%} ({source})"
        return Signal(
            asset=asset,
            direction=direction,
            confidence=confidence,
            payout=payout,
            source=source,
            reason=reason,
            features=feat,
            ta_direction=feat.ta_direction,
            ta_score=feat.ta_score,
            llm_action=llm_action,
            llm_reason=llm_reason,
            llm_outcome=llm_outcome,  # type: ignore[arg-type]
            used_model=used_model,
            fallback_used=False,
            memory_hint=memory_hint,
        )

    def _apply_fallback(
        self,
        feat,
        direction,
        confidence,
        source,
        reason,
        llm_reason,
        memory_hint,
        payout,
        used_model: str = "",
        *,
        s: BotSettings | None = None,
    ) -> Signal | None:
        s = s or self.settings
        mode = s.fallback_mode
        detail = f"IA indisponivel ({llm_reason}). Fallback={mode}."
        if mode == "skip":
            self.decide_log(
                feat.asset,
                "recusa",
                f"{detail} Modo seguranca: nao opera sem a IA.",
                ta_direction=direction,
                ta_score=feat.ta_score,
                llm_outcome="timeout",
                used_model=used_model,
                fallback_used=True,
                level="warn",
            )
            self.last_signal = f"{feat.asset} fallback skip"
            return None
        strong = abs(feat.ta_score) >= 0.55
        if mode == "safe" and not strong:
            self.decide_log(
                feat.asset,
                "recusa",
                f"{detail} TA nao esta forte o bastante (score {feat.ta_score:.2f}) para operar sozinha.",
                ta_direction=direction,
                ta_score=feat.ta_score,
                llm_outcome="timeout",
                used_model=used_model,
                fallback_used=True,
                level="warn",
            )
            return None
        source = "TA-fallback"
        reason = f"{reason} | {detail}"
        self.decide_log(
            feat.asset,
            "aceite",
            f"{detail} Operando por TA {direction.upper()} (score {feat.ta_score:.2f}). {memory_hint}",
            ta_direction=direction,
            ta_score=feat.ta_score,
            llm_outcome="timeout",
            used_model=used_model,
            fallback_used=True,
        )
        self.last_signal = f"{feat.asset} {direction.upper()} fallback TA"
        return Signal(
            asset=feat.asset,
            direction=direction,
            confidence=min(confidence, 0.78) if mode == "safe" else confidence,
            payout=payout,
            source=source,
            reason=reason,
            features=feat,
            ta_direction=feat.ta_direction,
            ta_score=feat.ta_score,
            llm_action="timeout",
            llm_reason=llm_reason,
            llm_outcome="timeout",
            used_model=used_model,
            fallback_used=True,
            memory_hint=memory_hint,
        )

    # ----------------------------------------------------------------- ordens
    def _execute(self, signal: Signal, s: BotSettings | None = None) -> bool:
        s = s or self.settings
        with self._order_lock:
            if self._stop.is_set():
                self.log(f"Ordem {signal.asset} descartada: motor parando", "warn")
                return False
            in_window, remain = self._entry_window(s)
            if not in_window:
                self.decide_log(
                    signal.asset, "skip", f"janela de entrada perdida (faltam {remain:.1f}s no candle)", level="warn"
                )
                return False
            ok, why = self._risk_check(s)
            with self._lock:
                too_many = len(self.open) >= s.max_open_trades
                account = self.account
            if not ok or too_many:
                self.log(f"Ordem {signal.asset} bloqueada: {why if not ok else 'max de ordens abertas'}", "warn")
                return False
            if self.iq.account != account:
                self.log("Conta do cliente difere da conta do motor; ordem bloqueada", "error")
                return False
            now_srv = self.iq.server_time()
            expires = expected_expiry(now_srv, s.duration_min)
            amount = s.amount
            res = self.iq.buy(signal.asset, amount, signal.direction, s.duration_min)
            if res.status == "rejected":
                self.log(f"Ordem recusada {signal.asset}: {res.detail}", "error")
                with self._lock:
                    self.last_trade_ts = time.time()
                return False
            rec = TradeRecord(
                id=str(res.order_id) if res.order_id is not None else f"incerta-{uuid.uuid4().hex[:8]}",
                order_id=res.order_id,
                account=account,
                asset=signal.asset,
                direction=signal.direction,
                amount=amount,
                payout=signal.payout,
                duration_min=s.duration_min,
                opened_at=datetime.now(),
                expires_at=expires,
                source=signal.source,
                reason=signal.reason,
                ta_score=signal.ta_score,
                ta_direction=signal.ta_direction,
                rsi=signal.features.rsi,
                stoch_k=signal.features.stoch_k,
                trend=signal.features.trend,
                llm_action=signal.llm_action,
                llm_reason=signal.llm_reason,
                llm_outcome=signal.llm_outcome,
                used_model=signal.used_model,
                fallback_used=signal.fallback_used,
                memory_hint=signal.memory_hint,
            )
            with self._lock:
                self.trades.append(rec)
                self.last_trade_ts = time.time()
            if res.status == "uncertain":
                # C2: a ordem pode existir. Conta como perda e para o motor para conferencia manual.
                with self._lock:
                    self.uncertain_orders += 1
                self._close(rec, "ERROR", -amount, note=f"ordem incerta ({res.detail})")
                self.stop(f"Ordem incerta em {signal.asset}: confira na corretora antes de retomar")
                return False
            with self._lock:
                self.open[rec.id] = rec
            self.log(
                f"ORDEM {signal.direction.upper()} {signal.asset} ${amount:.2f} [{account}] id {res.order_id} "
                f"conf {signal.confidence:.0%} payout {signal.payout:.0f}% via {signal.source}, "
                f"expira {datetime.fromtimestamp(expires).strftime('%H:%M:%S')}"
            )
            return True

    def _harvest(self) -> None:
        with self._lock:
            items = list(self.open.items())
        if not items:
            return
        now_srv = self.iq.server_time()
        for oid, rec in items:
            result = self.iq.check_result(rec.order_id)
            if result is None:
                deadline = (rec.expires_at or 0) + HARVEST_GRACE_SEC
                if rec.expires_at and now_srv > deadline:
                    # Conhecido #2: nao vira EQUAL com lucro 0; conta como perda total (conservador).
                    self._close(rec, "ERROR", -rec.amount, note="resultado nao apurado no prazo; contado como perda")
                continue
            status, profit = result
            self._close(rec, status, profit)

    def _close(self, rec: TradeRecord, status: str, profit: float, note: str = "") -> None:
        with self._lock:
            rec.result = status  # type: ignore[assignment]
            rec.profit = float(profit)
            rec.closed_at = datetime.now()
            counters = self.ledger.record(rec.account, status, profit)
            self.open.pop(rec.id, None)
        level = "error" if status == "ERROR" else "info"
        self.log(
            f"{status} {rec.asset} {rec.direction.upper()} [{rec.account}] PnL {profit:+.2f} | "
            f"dia {counters.profit:+.2f}, perdas seguidas {counters.consecutive_losses}"
            + (f" | {note}" if note else ""),
            level,
        )
        if status != "ERROR":
            self._remember(rec)

    def _remember(self, rec: TradeRecord) -> None:
        if not self.settings.memory_enabled:
            return
        self.memory.record(
            Experience(
                id=rec.id,
                ts=rec.closed_at or datetime.now(),
                asset=rec.asset,
                direction=rec.direction,
                result=rec.result,
                profit=rec.profit,
                ta_score=rec.ta_score,
                ta_direction=rec.ta_direction,
                rsi=rec.rsi,
                stoch_k=rec.stoch_k,
                trend=rec.trend or ("alta" if rec.direction == "call" else "baixa"),
                llm_action=rec.llm_action,
                llm_reason=rec.llm_reason,
                llm_outcome=rec.llm_outcome,
                used_model=rec.used_model,
                source=rec.source,
                reason=rec.reason,
            )
        )


@lru_cache
def get_engine() -> TradingEngine:
    return TradingEngine()
