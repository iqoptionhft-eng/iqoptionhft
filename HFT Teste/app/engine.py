from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Deque

from .backtest import run_ta_backtest
from .config import get_settings
from .features import extract_features, seconds_to_next_minute
from .iq_client import IQClient
from .memory import memory
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


class TradingEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._stop = threading.Event()
        s = get_settings()
        self.settings = BotSettings(
            account=s.default_account,  # type: ignore[arg-type]
            ollama_model=s.ollama_model,
        )
        self.iq = IQClient(s.iq_email, s.iq_password)
        self.brain = OllamaBrain()
        self.brain.set_models(self.settings.ollama_model, self.settings.ollama_fast_model)
        self.logs: Deque[LogEvent] = deque(maxlen=300)
        self.decisions: Deque[DecisionLog] = deque(maxlen=200)
        self.trades: list[TradeRecord] = []
        self.open: dict[str, TradeRecord] = {}
        self.status = "idle"
        self.last_error = ""
        self.wins = 0
        self.losses = 0
        self.equals = 0
        self.profit_today = 0.0
        self.consecutive_losses = 0
        self.last_signal = ""
        self.last_trade_ts = 0.0
        self.health = HealthStatus()
        self.last_backtest: BacktestResult | None = None
        self._cached_balance = 0.0
        self._cached_currency = "USD"
        self._last_asset_try: dict[str, float] = {}
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def log(self, message: str, level: str = "info") -> None:
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
        self.decisions.appendleft(item)
        self.log(f"[{asset}] {action}: {detail}", level)

    def snapshot(self) -> RuntimeState:
        with self._lock:
            s = get_settings()
            if self.iq.connected:
                try:
                    self._cached_balance = self.iq.balance()
                    self._cached_currency = self.iq.currency()
                except Exception:
                    pass
            return RuntimeState(
                status=self.status,  # type: ignore[arg-type]
                connected=self.iq.connected,
                account=self.iq.account,
                email=s.iq_email,
                balance=self._cached_balance,
                currency=self._cached_currency,
                ollama_ok=self.health.ollama_ok,
                ollama_model=self.settings.ollama_model,
                last_error=self.last_error,
                wins=self.wins,
                losses=self.losses,
                equals=self.equals,
                profit_today=self.profit_today,
                consecutive_losses=self.consecutive_losses,
                open_trades=len(self.open),
                last_signal=self.last_signal,
                settings=self.settings,
                trades=list(reversed(self.trades[-40:])),
                logs=list(self.logs)[:80],
                decisions=list(self.decisions)[:60],
                health=self.health,
                memory=memory.stats(),
                ollama_models=self.brain.last_models or self.health.models,
                last_backtest=self.last_backtest,
            )

    def update_settings(self, data: dict) -> BotSettings:
        with self._lock:
            merged = self.settings.model_dump()
            merged.update({k: v for k, v in data.items() if v is not None})
            self.settings = BotSettings(**merged)
            self.brain.set_models(self.settings.ollama_model, self.settings.ollama_fast_model)
            self.log(
                f"Modelos: complexo={self.settings.ollama_model} "
                f"rapido={self.settings.ollama_fast_model or 'nenhum'} "
                f"fallback={self.settings.fallback_mode}"
            )
            threading.Thread(target=self._refresh_health, daemon=True).start()
            return self.settings

    def connect(self) -> tuple[bool, str]:
        self.status = "connecting"
        self.last_error = ""
        self.log("Conectando na IQ Option...")
        try:
            ok, reason = self.iq.connect()
        except Exception as exc:
            ok, reason = False, str(exc)
        if not ok:
            self.status = "error"
            self.last_error = reason
            self.health.iq_ok = False
            self.health.iq_detail = reason
            self.log(reason, "error")
            return False, reason
        self.iq.change_account(self.settings.account)
        self.status = "idle"
        self.last_error = ""
        self.log(f"Conectado. Conta {self.iq.account}. Saldo {self.iq.balance():.2f}")
        self._refresh_health()
        if self.health.ollama_ok:
            threading.Thread(target=self.brain.warmup, args=(self.settings.ollama_model,), daemon=True).start()
        return True, "ok"

    def set_account(self, mode: AccountMode) -> float:
        bal = self.iq.change_account(mode)
        self.settings.account = mode
        self.log(f"Conta alterada para {mode}. Saldo {bal:.2f}", "warn" if mode == "REAL" else "info")
        return bal

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._refresh_health()
        if not self.iq.connected:
            ok, reason = self.connect()
            if not ok:
                raise RuntimeError(reason)
        if not self.health.iq_ok:
            raise RuntimeError(f"IQ Option nao saudavel: {self.health.iq_detail}")
        if self.settings.use_llm and not self.health.ollama_ok and self.settings.fallback_mode == "skip":
            raise RuntimeError("Ollama offline e fallback=skip. Ligue o Ollama ou mude o fallback.")
        self._stop.clear()
        self.status = "running"
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("Motor iniciado")

    def stop(self) -> None:
        self._stop.set()
        self.status = "paused"
        self.log("Motor pausado")

    def run_backtest(self, asset: str | None = None) -> BacktestResult:
        target = (asset or (self.settings.assets[0] if self.settings.assets else "EURUSD-OTC")).strip()
        if not self.iq.connected:
            raise RuntimeError("Conecte na IQ Option para baixar velas historicas.")
        candles = self.iq.candles(target, 60, 400)
        result = run_ta_backtest(target, candles, memory, use_memory_filter=self.settings.memory_enabled)
        self.last_backtest = result
        self.log(
            f"Backtest {target}: {result.taken} entradas, wr {result.win_rate:.0%} "
            f"({result.wins}W/{result.losses}L), memoria bloqueou {result.skipped_memory}"
        )
        return result

    def _health_loop(self) -> None:
        while True:
            try:
                self._refresh_health()
            except Exception:
                pass
            time.sleep(8)

    def _refresh_health(self) -> None:
        iq_ok = False
        iq_detail = "desconectado"
        if self.iq.connected or self.iq.api:
            iq_ok = self.iq.check_connect()
            iq_detail = "websocket ok" if iq_ok else "websocket caiu"
            if not iq_ok and self.status == "running":
                self.log("IQ Option desconectou. Tentando reconectar...", "warn")
                ok, reason = self.iq.connect()
                iq_ok = ok
                iq_detail = "reconectado" if ok else reason
                if not ok:
                    self.last_error = reason
        ollama_ok, ollama_detail, models = self.brain.health(self.settings.ollama_model)
        if self.settings.ollama_fast_model:
            fast_ok, fast_detail, _ = self.brain.health(self.settings.ollama_fast_model)
            if not fast_ok:
                ollama_detail += f" | rapido: {fast_detail}"
        ready = iq_ok and (ollama_ok or self.settings.fallback_mode != "skip")
        self.health = HealthStatus(
            iq_ok=iq_ok,
            iq_detail=iq_detail,
            ollama_ok=ollama_ok,
            ollama_detail=ollama_detail,
            models=models,
            checked_at=datetime.now(),
            ready_to_trade=ready,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.last_error = str(exc)
                self.log(f"Erro no ciclo: {exc}", "error")
                time.sleep(1.0)
            time.sleep(self.settings.poll_interval_sec)

    def _tick(self) -> None:
        self._harvest()
        if self.status != "running":
            return
        if not self.health.iq_ok:
            return
        if self.profit_today <= -abs(self.settings.max_daily_loss):
            self.log("Limite diario de perda atingido. Pausando.", "warn")
            self.stop()
            return
        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            self.log("Sequencia de perdas. Pausando para proteger banca.", "warn")
            self.stop()
            return
        if len(self.open) >= self.settings.max_open_trades:
            return
        if time.time() - self.last_trade_ts < self.settings.cooldown_sec:
            return

        remain = seconds_to_next_minute(time.time())
        if remain > self.settings.enter_last_seconds:
            return

        assets = self.iq.open_assets(self.settings.assets)
        best: Signal | None = None
        for asset in assets:
            if time.time() - self._last_asset_try.get(asset, 0) < 3:
                continue
            sig = self._analyze(asset)
            self._last_asset_try[asset] = time.time()
            if not sig:
                continue
            if best is None or sig.confidence > best.confidence:
                best = sig

        if best and best.confidence >= self.settings.min_confidence:
            self._execute(best)

    def _analyze(self, asset: str) -> Signal | None:
        payout = self.iq.payout(asset)
        if payout and payout < self.settings.min_payout:
            self.decide_log(asset, "skip", f"payout {payout:.0f}% abaixo do minimo {self.settings.min_payout:.0f}%")
            return None
        candles = self.iq.candles(asset, 60, 80)
        if len(candles) < 30:
            return None
        feat = extract_features(asset, candles)
        if feat.ta_direction is None:
            self.decide_log(asset, "skip", f"TA neutra ({feat.ta_reason})", ta_score=feat.ta_score)
            return None

        direction = feat.ta_direction
        confidence = min(0.93, 0.5 + abs(feat.ta_score) * 0.55)
        source = "TA"
        reason = feat.ta_reason
        llm_action = ""
        llm_reason = ""
        llm_outcome = "unused"
        used_model = ""
        fallback_used = False
        memory_hint = ""

        if self.settings.memory_enabled:
            rate, n, hint = memory.similar_win_rate(feat, direction)
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

        if self.settings.use_llm:
            if not self.health.ollama_ok:
                llm_outcome = "error"
                llm_reason = self.health.ollama_detail
                sig = self._apply_fallback(feat, direction, confidence, source, reason, llm_reason, memory_hint, payout)
                return sig
            tail = [c.model_dump() for c in candles]
            llm_dir, llm_conf, llm_reason, used_model, timed_out = self.brain.decide(
                feat,
                tail,
                timeout=self.settings.llm_timeout_sec,
                use_fast_first=self.settings.use_fast_model_first,
            )
            llm_action = llm_dir or "skip"
            if timed_out:
                llm_outcome = "timeout"
                sig = self._apply_fallback(
                    feat, direction, confidence, source, reason, llm_reason, memory_hint, payout, used_model
                )
                return sig
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
                    f"mas {used_model} recusou/divergiu ({llm_dir.upper()}): {llm_reason}",
                    ta_direction=direction,
                    ta_score=feat.ta_score,
                    llm_outcome=llm_outcome,
                    used_model=used_model,
                    level="warn",
                )
                self.last_signal = f"{asset} conflito TA={direction} IA={llm_dir}"
                return None
            llm_outcome = "agree"
            confidence = min(0.97, (confidence + llm_conf) / 1.7 + 0.25)
            source = "TA+IA"
            reason = f"{feat.ta_reason} | IA concordou ({used_model}): {llm_reason}"
            self.decide_log(
                asset,
                "aceite",
                f"TA {direction.upper()} e IA concordaram. {reason}. {memory_hint}",
                ta_direction=direction,
                ta_score=feat.ta_score,
                llm_outcome=llm_outcome,
                used_model=used_model,
            )
        else:
            self.decide_log(
                asset,
                "aceite",
                f"IA desligada. TA {direction.upper()} ({feat.ta_reason}). {memory_hint}",
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
            fallback_used=fallback_used,
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
    ) -> Signal | None:
        mode = self.settings.fallback_mode
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
            confidence=min(confidence, 0.78 if mode == "safe" else confidence),
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

    def _execute(self, signal: Signal) -> None:
        amount = self.settings.amount
        ok, order_id = self.iq.buy(
            signal.asset, amount, signal.direction, self.settings.duration_min
        )
        if not ok:
            self.log(f"Falha ao comprar {signal.asset}: {order_id}", "error")
            return
        rec = TradeRecord(
            id=str(order_id) if order_id != "None" else uuid.uuid4().hex[:10],
            asset=signal.asset,
            direction=signal.direction,
            amount=amount,
            payout=signal.payout,
            opened_at=datetime.now(),
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
        self.open[rec.id] = rec
        self.trades.append(rec)
        self.last_trade_ts = time.time()
        self.log(
            f"ORDEM {signal.direction.upper()} {signal.asset} ${amount:.2f} "
            f"conf {signal.confidence:.0%} payout {signal.payout:.0f}% via {signal.source}"
        )

    def _harvest(self) -> None:
        done_ids = []
        for oid, rec in list(self.open.items()):
            result = self.iq.check_result(oid)
            if result is None:
                if (datetime.now() - rec.opened_at).total_seconds() > self.settings.duration_min * 60 + 25:
                    rec.result = "EQUAL"
                    rec.closed_at = datetime.now()
                    self._remember(rec)
                    done_ids.append(oid)
                continue
            status, profit = result
            rec.result = status  # type: ignore[assignment]
            rec.profit = profit
            rec.closed_at = datetime.now()
            self.profit_today += profit
            if status == "WIN":
                self.wins += 1
                self.consecutive_losses = 0
            elif status == "LOSS":
                self.losses += 1
                self.consecutive_losses += 1
            else:
                self.equals += 1
            self.log(f"{status} {rec.asset} {rec.direction.upper()} PnL {profit:+.2f}")
            self._remember(rec)
            done_ids.append(oid)
        for oid in done_ids:
            self.open.pop(oid, None)

    def _remember(self, rec: TradeRecord) -> None:
        if not self.settings.memory_enabled:
            return
        memory.record(
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


engine = TradingEngine()
