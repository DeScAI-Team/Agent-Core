"""Shared HTTP helpers for discover sources."""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_TIMEOUT = float(os.environ.get("DISCOVER_TIMEOUT", "10"))
MAX_HTTP_CALLS = int(os.environ.get("DISCOVER_MAX_HTTP_CALLS", "80"))


class CallBudget:
    def __init__(self, limit: int = MAX_HTTP_CALLS) -> None:
        self.limit = limit
        self.count = 0
        self.exhausted = False

    def consume(self) -> bool:
        if self.count >= self.limit:
            self.exhausted = True
            return False
        self.count += 1
        return True


def req(
    url: str,
    fail: list[dict[str, str]],
    step: str,
    budget: CallBudget,
    *,
    params: dict | None = None,
    json_out: bool = False,
    timeout: float | None = None,
) -> Any:
    if not budget.consume():
        fail.append({"step": step, "reason": "HTTP call budget exhausted"})
        return None
    tmo = timeout if timeout is not None else DEFAULT_TIMEOUT
    try:
        r = requests.get(url, params=params or {}, timeout=tmo)
        if not r.ok:
            fail.append({"step": step, "reason": f"HTTP {r.status_code}: {r.text[:500]}"})
            return None
        return r.json() if json_out else r.text
    except requests.JSONDecodeError as e:
        fail.append({"step": step, "reason": f"JSON decode error: {e}"})
        return None
    except requests.RequestException as e:
        fail.append({"step": step, "reason": str(e)})
        return None
