"""TypeSafe Jev judge: turns (state, questions) into API answers.

Endpoint, auth header shape, and model name follow the working pattern in
hookbench-test/tag.py (POST https://api.typesafe.ai/v1/systemone, `Authorization:
Bearer <key>`, model "jev-latest"). This module never runs at import time -- it only
makes a network call when `JevJudge()` is actually invoked, so importing it (or
constructing it and letting construction fail on a missing key) is offline-safe.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Tiny KEY=VALUE parser -- no python-dotenv dependency."""
    out: dict[str, str] = {}
    try:
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                out[key] = value
    except OSError:
        return out
    return out


def _api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent / ".env"
    key = _read_dotenv(env_path).get("TYPESAFE_API_KEY")
    if key:
        return key
    raise RuntimeError("TYPESAFE_API_KEY is not set (checked environment and .env)")


class JevJudge:
    """Callable (state, questions) -> answers, backed by POST /v1/systemone.

    Raises RuntimeError immediately (no network call) if no key is configured, same
    as ScrapeCreatorsClient's "no key" behavior.
    """

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        self.api_key = api_key or _api_key()
        self.model = model
        self._http = httpx.Client(timeout=timeout)

    def __call__(self, state: dict, questions: dict) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        resp = self._http.post(
            API_URL,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("answers", {})
