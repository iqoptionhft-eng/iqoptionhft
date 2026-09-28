import httpx

from app.memory import ExperienceMemory
from app.models import Features
from app.ollama_brain import OllamaBrain


def feat():
    return Features(asset="EURUSD-OTC", close=1, ema_fast=1.1, ema_slow=1.0, rsi=62, macd=0.1, macd_signal=0,
                    stoch_k=60, atr=0, momentum=0, body_ratio=0.5, trend="alta", ta_score=0.5,
                    ta_direction="call", ta_reason="EMA9>EMA21")


def test_decide_envia_think_false_e_le_json(monkeypatch, tmp_path):
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, json={"response": '```json\n{"action":"call","confidence":0.75,"reason":"ok"}\n```'},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    brain = OllamaBrain(store=ExperienceMemory(tmp_path / "m.jsonl"))
    brain.set_models("gemma4:12b-it-qat")
    action, conf, reason, model, timed = brain.decide(feat(), [{"close": 1.0}] * 12, timeout=5)
    assert (action, conf, timed) == ("call", 0.75, False)
    assert sent[0]["think"] is False


def test_decide_repete_sem_think_se_modelo_nao_aceitar(monkeypatch, tmp_path):
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        req = httpx.Request("POST", url)
        if "think" in json:
            return httpx.Response(400, json={"error": "model does not support thinking"}, request=req)
        return httpx.Response(200, json={"response": '{"action":"skip","confidence":0.3,"reason":"x"}'}, request=req)

    monkeypatch.setattr(httpx, "post", fake_post)
    brain = OllamaBrain(store=ExperienceMemory(tmp_path / "m.jsonl"))
    action, conf, reason, model, timed = brain.decide(feat(), [{"close": 1.0}] * 12, timeout=5)
    assert action is None and len(sent) == 2 and "think" not in sent[1]


def test_resposta_vazia_vira_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: httpx.Response(
        200, json={"response": ""}, request=httpx.Request("POST", url)))
    brain = OllamaBrain(store=ExperienceMemory(tmp_path / "m.jsonl"))
    action, *_ = brain.decide(feat(), [{"close": 1.0}] * 12, timeout=5)
    assert action is None
