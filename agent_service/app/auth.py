"""Shared-secret authentication between n8n and this service."""

import hmac
import logging

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)


async def require_token(
    x_service_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    # Fail closed: if no secret is configured, nothing is authorized.
    if not settings.service_shared_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is not configured (SERVICE_SHARED_SECRET missing).",
        )
    supplied = x_service_token or ""
    if not hmac.compare_digest(supplied.encode(), settings.service_shared_secret.encode()):
        log.warning("Rejected request: missing or invalid X-Service-Token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Service-Token header.",
        )
