from __future__ import annotations

import hashlib
import re
from typing import Iterable


_RE_TELEGRAM_BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_RE_PHONE = re.compile(r"(?<!\w)\+?\d{7,15}(?!\w)")
_RE_LONG_HEX = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_RE_LONG_B64 = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    out = _RE_TELEGRAM_BOT_TOKEN.sub("«redacted:telegram_bot_token»", out)

    def _mask_phone(m: re.Match) -> str:
        s = m.group(0)
        if len(s) <= 4:
            return "«redacted:phone»"
        return s[:2] + "*" * (len(s) - 4) + s[-2:]

    out = _RE_PHONE.sub(_mask_phone, out)

    def _mask_long(m: re.Match) -> str:
        s = m.group(0)
        return f"«redacted:{_stable_hash(s)}»"

    out = _RE_LONG_HEX.sub(_mask_long, out)
    out = _RE_LONG_B64.sub(_mask_long, out)
    return out


def redact_lines(lines: Iterable[str]) -> list[str]:
    return [redact_text(line) for line in lines]

