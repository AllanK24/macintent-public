"""Public speech-result extraction shared by the ASR child and parent.

Only bounded public strings leave this seam. Arbitrary backend objects are
never converted with ``repr`` or otherwise exposed to the application.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

MODEL_SAMPLE_RATE = 16_000


def _public_text_or_none(result: Any) -> str | None:
    if isinstance(result, (list, tuple)):
        result = result[0] if result else ""
    text = result if isinstance(result, str) else getattr(result, "text", None)
    return text if isinstance(text, str) else None


def public_text_from_result(result: Any) -> str:
    """Extract only an actual public string, never ``repr`` arbitrary output."""
    return _public_text_or_none(result) or ""


def invoke_backend_diagnostic(backend: Any, samples: Any) -> tuple[str, str]:
    """Return bounded-seam public raw and normalized strings, without repr."""
    method: Callable[..., Any]
    if hasattr(backend, "transcribe"):
        method = backend.transcribe
    elif callable(backend):
        method = backend
    else:
        raise TypeError("speech backend is not callable")

    # Test doubles commonly accept only samples; the production contract also
    # permits a sample-rate argument.  Bind without executing twice, so a
    # backend TypeError is never mistaken for a signature mismatch.
    try:
        inspect.signature(method).bind(samples, MODEL_SAMPLE_RATE)
    except (TypeError, ValueError):
        result = method(samples)
    else:
        result = method(samples, MODEL_SAMPLE_RATE)
    raw = _public_text_or_none(result)
    if raw is None:
        raise TypeError("speech result has no public text")
    return raw, raw.strip()


__all__ = ["MODEL_SAMPLE_RATE", "invoke_backend_diagnostic", "public_text_from_result"]
