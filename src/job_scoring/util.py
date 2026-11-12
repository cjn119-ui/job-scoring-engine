"""Hashing and timestamp helpers. Standard library only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data) -> str:
    if not isinstance(data, (bytes, bytearray)):
        data = data.encode("utf-8") if isinstance(data, str) else canonical_json(data).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def parse_ts(value) -> datetime:
    """ISO-8601 with a timezone (or a datetime / epoch number) -> aware UTC datetime. Raises ValueError otherwise."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        s = value.strip()
        if len(s) == 10:                       # a bare provider date (YYYY-MM-DD) means midnight UTC
            s += "T00:00:00+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        raise ValueError(f"unsupported timestamp type {type(value).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp must carry a timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def iso(value) -> str:
    return parse_ts(value).strftime("%Y-%m-%dT%H:%M:%SZ")
