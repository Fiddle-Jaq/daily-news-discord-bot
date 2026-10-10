"""Secret-free exception summaries (SPEC §9.7).

Exception messages may embed request URLs (e.g. the Discord webhook URL) or API keys,
so callers must log exceptions only through ``describe_error`` and never via
``logger.exception`` / ``exc_info=True``.
"""

from typing import Any

import httpx
from google.genai import errors as genai_errors


class ConfigError(Exception):
    """A required setting is missing. The message names the variable, never a value."""


def _gemini_details(exc: genai_errors.APIError) -> list[dict[str, Any]]:
    """The ``error.details`` entries of a Gemini error response (google.rpc types)."""
    body = exc.details
    error = body.get("error", body) if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def gemini_quota_ids(exc: genai_errors.APIError) -> list[str]:
    """``quotaId`` values of QuotaFailure violations, e.g. ``...PerDayPerProjectPerModel...``."""
    return [
        v["quotaId"]
        for d in _gemini_details(exc)
        if isinstance(d.get("violations"), list)
        for v in d["violations"]
        if isinstance(v, dict) and isinstance(v.get("quotaId"), str)
    ]


def gemini_retry_delay(exc: genai_errors.APIError) -> float | None:
    """Seconds from a RetryInfo ``retryDelay`` such as ``"17s"`` or ``"0.5s"``."""
    for d in _gemini_details(exc):
        delay = d.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                return None
    return None


def describe_error(exc: BaseException) -> str:
    """Return the exception class name plus safe key facts, never ``str(exc)``."""
    name = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{name}(status={exc.response.status_code})"
    if isinstance(exc, genai_errors.APIError):
        parts = [f"code={exc.code}", f"status={exc.status}"]
        if quota_ids := gemini_quota_ids(exc):
            parts.append(f"quota_ids={','.join(quota_ids)}")
        if (delay := gemini_retry_delay(exc)) is not None:
            parts.append(f"retry_delay={delay:g}s")
        return f"{name}({' '.join(parts)})"
    if isinstance(exc, ConfigError):
        return f"{name}({exc})"
    return name
