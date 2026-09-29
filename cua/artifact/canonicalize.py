"""URL/id canonicalization helpers."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

ID_SEGMENT = re.compile(r"^\d{4,}$")
QUERY_ID_KEYS = {"m", "id", "member", "member_no", "acct"}


def canonicalize_path(path: str) -> str:
    parts = path.split("/")
    out: list[str] = []
    for part in parts:
        if part and ID_SEGMENT.match(part):
            out.append(":id")
        else:
            out.append(part)
    return "/".join(out)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = canonicalize_path(parsed.path)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    pairs: list[str] = []
    for k, vals in qs.items():
        if k.lower() in QUERY_ID_KEYS:
            pairs.append(f"{k}=:id")
        else:
            for v in vals:
                pairs.append(f"{k}={v}")
    return urlunparse(("", "", path, "", "&".join(pairs), ""))
