"""Fail-closed single-operator pilot boundary; no client identity claims."""

import hashlib
import hmac
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request

from lextrace.config import AppSettings


@dataclass(frozen=True)
class OperatorPrincipal:
    operator_id: str


def authenticate_operator(request: Request, settings: AppSettings) -> OperatorPrincipal:
    """Validate configuration and compare fixed-size token digests in constant time."""
    identity = settings.operator_id
    secret = (
        settings.operator_api_secret.get_secret_value()
        if settings.operator_api_secret is not None
        else ""
    )
    # Require a 256-bit (or longer) URL-safe generated credential. Format cannot
    # establish entropy; administrators must generate it with secrets.token_urlsafe.
    if (
        identity is None
        or re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", identity) is None
        or re.fullmatch(r"[A-Za-z0-9_-]{43,256}", secret) is None
    ):
        raise HTTPException(503, "OPERATOR_AUTH_UNAVAILABLE")
    values = request.headers.getlist("authorization")
    parts = values[0].split(" ") if len(values) == 1 else []
    token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    matched = hmac.compare_digest(
        hashlib.sha256(token.encode()).digest(),
        hashlib.sha256(secret.encode()).digest(),
    )
    if not token or not matched:
        raise HTTPException(
            401, "OPERATOR_AUTH_REQUIRED", headers={"WWW-Authenticate": "Bearer"}
        )
    return OperatorPrincipal(operator_id=identity)


def require_operator(request: Request) -> OperatorPrincipal:
    """Trusted reviewer identity for future handlers; never read it from a body."""
    principal = getattr(request.state, "operator", None)
    if not isinstance(principal, OperatorPrincipal):
        raise HTTPException(401, "OPERATOR_AUTH_REQUIRED")
    return principal
