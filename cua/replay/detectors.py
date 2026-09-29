"""Global detectors: session expired, 500, interstitial, permission denied."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from cua.surface.base import Observation


class DetectKind(str, Enum):
    INTERSTITIAL = "interstitial"
    SESSION_EXPIRED = "session_expired"
    APP_ERROR = "app_error"
    PERMISSION_DENIED = "permission_denied"
    NONE = "none"


class Detection(BaseModel):
    kind: DetectKind
    matched: str | None = None


def detect_observation(obs: Observation) -> Detection:
    text = (obs.visible_text_excerpt or "").lower()
    url = (obs.url or "").lower()
    title = (obs.title or "").lower()

    if "500 internal server error" in text or "core fault injected" in text:
        return Detection(kind=DetectKind.APP_ERROR, matched="500")
    if "/login" in url or "teller workstation login" in title or "operator sign-on" in title:
        if "session expired" in text or "logon" in text:
            return Detection(kind=DetectKind.SESSION_EXPIRED, matched="login")
    if "session expired" in text:
        return Detection(kind=DetectKind.SESSION_EXPIRED, matched="session expired")
    if "system notice" in text and "acknowledge" in text:
        return Detection(kind=DetectKind.INTERSTITIAL, matched="System notice")
    if "not authorized" in text or "permission" in text and "denied" in text:
        return Detection(kind=DetectKind.PERMISSION_DENIED, matched="not authorized")
    return Detection(kind=DetectKind.NONE)
