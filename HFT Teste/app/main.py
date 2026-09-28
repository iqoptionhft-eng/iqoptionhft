from __future__ import annotations

import os
import secrets
from pathlib import Path

from .api import create_app
from .config import get_settings
from .engine import get_engine

_settings = get_settings()
PANEL_TOKEN = _settings.panel_token or secrets.token_urlsafe(24)
PANEL_URL = f"http://{_settings.host}:{_settings.port}/#token={PANEL_TOKEN}"


def _write_token_file() -> Path:
    path = Path(_settings.data_dir) / "panel_token.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PANEL_URL + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


_token_file = _write_token_file()
print(f"Painel: {PANEL_URL}\n(URL com token tambem salva em {_token_file})", flush=True)

app = create_app(get_engine(), PANEL_TOKEN)
