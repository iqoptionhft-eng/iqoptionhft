from __future__ import annotations

import json
from typing import Any

import httpx

from .config import get_settings
from .memory import ExperienceMemory, memory as default_memory
from .models import Direction, Features

SYSTEM = (
    "Voce e um trader de opcoes binarias conservador. "
    "Use o historico de acertos e erros para nao repetir padroes falsos. "
    "Responda SOMENTE um JSON valido no formato "
    '{"action":"call"|"put"|"skip","confidence":0-1,"reason":"texto curto"}. '
    "Pule (skip) se o sinal nao for claro ou se o historico mostrar muitos erros no mesmo RSI/tendencia. "
    "Nao invente dados."
)


class OllamaBrain:
    def __init__(self, store: ExperienceMemory | None = None) -> None:
        s = get_settings()
        self.host = s.ollama_host.rstrip("/")
        self.model = s.ollama_model
        self.fast_model = ""
        self.memory = store or default_memory
        self.last_models: list[str] = []

    def set_models(self, complex_model: str, fast_model: str = "") -> None:
        if complex_model:
            self.model = complex_model.strip()
        self.fast_model = (fast_model or "").strip()

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=2.5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            self.last_models = names
            return names
        except Exception:
            return list(self.last_models)

    def health(self, model: str | None = None) -> tuple[bool, str, list[str]]:
        target = model or self.model
        try:
            names = self.list_models()
            if not names:
                return False, "Ollama respondeu sem modelos", names
            if _has_model(target, names):
                return True, target, names
            return False, f"modelo {target} nao encontrado em {', '.join(names)}", names
        except Exception as exc:
            return False, str(exc), []

    def warmup(self, model: str | None = None) -> None:
        try:
            httpx.post(
                f"{self.host}/api/generate",
                json={
                    "model": model or self.model,
                    "prompt": "ok",
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"num_predict": 1, "temperature": 0},
                },
                timeout=60.0,
            )
        except Exception:
            pass

    def decide(
        self,
        features: Features,
        candles_tail: list[dict[str, float]],
        timeout: float,
        use_fast_first: bool = False,
    ) -> tuple[Direction | None, float, str, str, bool]:
        """
        Returns: direction, confidence, reason, model_used, timed_out
        """
        models = []
        if use_fast_first and self.fast_model:
            models.append(self.fast_model)
        models.append(self.model)
        models = list(dict.fromkeys([m for m in models if m]))

        last_err = "sem modelo"
        for idx, model in enumerate(models):
            action, conf, reason, timed_out = self._call_model(
                model, features, candles_tail, timeout
            )
            if timed_out:
                last_err = reason
                continue
            if action is None and idx < len(models) - 1 and conf < 0.7:
                continue
            return action, conf, reason, model, False
        timed = "timeout" in last_err.lower() or "timed out" in last_err.lower()
        return None, 0.0, last_err, models[-1] if models else "", timed

    def _call_model(
        self,
        model: str,
        features: Features,
        candles_tail: list[dict[str, float]],
        timeout: float,
    ) -> tuple[Direction | None, float, str, bool]:
        history = self.memory.context_for_prompt(features, features.ta_direction or "call")
        payload = {
            "asset": features.asset,
            "trend": features.trend,
            "rsi": round(features.rsi, 2),
            "ema9": round(features.ema_fast, 6),
            "ema21": round(features.ema_slow, 6),
            "macd": round(features.macd, 6),
            "stoch": round(features.stoch_k, 2),
            "ta_score": round(features.ta_score, 3),
            "ta_dir": features.ta_direction,
            "ta_reason": features.ta_reason,
            "last_closes": [round(x["close"], 6) for x in candles_tail[-12:]],
        }
        prompt = (
            "Decida a proxima opcao de 1 minuto. call = sobe, put = desce.\n"
            f"Sinal tecnico atual: {json.dumps(payload, separators=(',', ':'))}\n"
            f"{history}\n"
            "Se o historico mostrar que RSI/tendencia iguais geraram LOSS, responda skip."
        )
        try:
            r = httpx.post(
                f"{self.host}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "system": SYSTEM,
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {
                        "temperature": 0.1,
                        "top_p": 0.9,
                        "num_predict": 90,
                        "num_ctx": 3072,
                    },
                },
                timeout=timeout,
            )
            r.raise_for_status()
            text = r.json().get("response", "")
            data = _extract_json(text)
            action = str(data.get("action", "skip")).lower()
            conf = float(data.get("confidence", 0))
            reason = str(data.get("reason", "modelo"))
            if action in ("call", "put") and 0.0 <= conf <= 1.0:
                return action, conf, reason, False  # type: ignore[return-value]
            return None, conf, reason or "skip", False
        except httpx.TimeoutException:
            return None, 0.0, f"timeout do modelo {model}", True
        except Exception as exc:
            err = str(exc)
            timed = "timeout" in err.lower()
            return None, 0.0, f"erro {model}: {exc}", timed


def _has_model(target: str, names: list[str]) -> bool:
    if not target:
        return False
    if target in names:
        return True
    parts = target.split(":", 1)
    base, tag = parts[0], parts[1] if len(parts) > 1 else ""
    for n in names:
        if n == target or n.startswith(target + ":") or n.startswith(target + "-"):
            return True
        if tag and n.startswith(f"{base}:{tag}"):
            return True
        if not tag and n.split(":")[0] == base:
            return True
    return False


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
