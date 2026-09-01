"""Shared text redaction for local evidence and public telemetry."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|(?<![A-Za-z0-9])/(?:Users|home)/|(?<![A-Za-z0-9])\\\\[A-Za-z0-9._-]+[\\/]|(?:token|secret|password)\s*[:=](?!\s*\[redacted\]))"
)
_LOCAL_PATH_TEXT = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s<>\"']+|/(?:Users|home)/[^\s<>\"']+|\\\\[A-Za-z0-9._-]+[\\/][^\s<>\"']+)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|secret|password|api[ _-]?key|access[ _-]?token|private[ _-]?key|seed(?:[ _-]+phrase)?)\s*[:=]\s*[\"']?[^\s,;\]}>)\"']+"
)
_KNOWN_CREDENTIAL = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|xox[baprs]-[A-Za-z0-9-]+)\b"
)
_BITCOIN_PRIVATE_KEY = re.compile(
    r"(?<![A-Za-z0-9])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}(?![A-Za-z0-9])"
)


def redact_text(value: Any, *, default: str = "", max_len: int = 4000) -> str:
    """Remove sensitive spans while retaining surrounding public context."""

    text = default if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()[: max(0, int(max_len))]
    text = _LOCAL_PATH_TEXT.sub("[local-path]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1: [redacted]", text)
    text = _KNOWN_CREDENTIAL.sub("[redacted-token]", text)
    text = _BITCOIN_PRIVATE_KEY.sub("[redacted-key]", text)
    return "[redacted]" if _SENSITIVE_TEXT.search(text) else text
