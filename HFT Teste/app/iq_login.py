from __future__ import annotations

import json
from typing import Any

import requests

LOGIN_URLS = (
    "https://auth.iqoption.com/api/v1.0/login",
    "https://auth.iqoption.com/api/v2/login",
    "https://api.iqoption.com/v2/login",
)

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Origin": "https://iqoption.com",
    "Referer": "https://iqoption.com/",
}


def _extract_ssid(response: requests.Response) -> str | None:
    ssid = response.cookies.get("ssid")
    if ssid:
        return str(ssid)
    raw_set = response.headers.get("Set-Cookie") or response.headers.get("set-cookie") or ""
    if "ssid=" in raw_set:
        part = raw_set.split("ssid=", 1)[1]
        return part.split(";", 1)[0].strip() or None
    try:
        payload = response.json()
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("ssid"):
        return str(data["ssid"])
    if isinstance(payload, dict) and payload.get("ssid"):
        return str(payload["ssid"])
    return None


def _error_message(response: requests.Response) -> str:
    text = (response.text or "")[:800]
    try:
        payload: Any = response.json()
    except Exception:
        return f"HTTP {response.status_code}: {text or 'resposta vazia'}"
    if isinstance(payload, dict):
        if payload.get("message"):
            return str(payload["message"])
        errors = payload.get("errors") or payload.get("fails")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("title") or first.get("message") or first)
            return str(first)
        code = payload.get("code")
        if code:
            return f"{code}: {payload.get('message') or text}"
    return f"HTTP {response.status_code}: {text}"


def fetch_ssid(email: str, password: str) -> tuple[str | None, str]:
    if not email or not password:
        return None, "Email ou senha vazios no arquivo .env"
    last_error = "falha no login"
    session = requests.Session()
    bodies = (
        {"email": email, "password": password},
        {"identifier": email, "password": password},
    )
    for url in LOGIN_URLS:
        for body in bodies:
            try:
                response = session.post(url, json=body, headers=HEADERS, timeout=25)
            except Exception as exc:
                last_error = f"rede: {exc}"
                continue
            ssid = _extract_ssid(response)
            if ssid and ssid.lower() not in {"false", "null", "none"}:
                return ssid, "ok"
            last_error = _error_message(response)
            lowered = last_error.lower()
            if "2fa" in lowered or "verify" in lowered or "sms" in lowered:
                return None, "A conta esta com 2FA. Desative o SMS/2FA na IQ Option para o robo logar."
            if response.status_code in (401, 403):
                return None, last_error
    return None, last_error
