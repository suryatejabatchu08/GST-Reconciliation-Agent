"""
gateway_service/auth.py
JWT authentication for the API Gateway.

Validates Supabase-issued JWTs on protected routes.
Supabase signs tokens using HS256 with the project's JWT Secret.

Token flow:
  1. Frontend calls Supabase Auth (login/signup)
  2. Supabase returns access_token (JWT)
  3. Frontend sends: Authorization: Bearer <token>
  4. Gateway validates the token here — no Supabase API call needed
     (pure offline validation using the shared JWT secret)

Development mode:
  If SUPABASE_JWT_SECRET is not set, auth is BYPASSED with a warning.
  This allows the services to be tested locally without a Supabase account.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from pydantic import BaseModel

from shared.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Token payload model ────────────────────────────────────

class TokenPayload(BaseModel):
    sub: str                         # Supabase user UUID
    email: Optional[str] = None
    role: str = "authenticated"      # Supabase role claim
    aud: Optional[str] = None


# ── JWT validation ─────────────────────────────────────────

def _decode_token(token: str) -> TokenPayload:
    """
    Decode and validate a Supabase JWT.
    Raises HTTPException(401) if invalid or expired.
    """
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",   # Supabase sets aud="authenticated"
        )
        return TokenPayload(**payload)
    except JWTError as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI dependency ─────────────────────────────────────

async def require_auth(
    authorization: Optional[str] = Header(default=None)
) -> TokenPayload:
    """
    FastAPI dependency: validates Bearer token from Authorization header.

    Usage:
        @app.get("/protected")
        async def route(user: TokenPayload = Depends(require_auth)):
            return {"user_id": user.sub}
    """
    # Dev mode: bypass auth if JWT secret not configured
    if not settings.supabase_jwt_secret:
        logger.warning(
            "SUPABASE_JWT_SECRET not set — auth bypassed (dev mode). "
            "Set it in .env for production."
        )
        return TokenPayload(sub="dev-user", email="dev@local", role="authenticated")

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return _decode_token(token)


async def optional_auth(
    authorization: Optional[str] = Header(default=None)
) -> Optional[TokenPayload]:
    """
    Optional auth dependency — returns None instead of raising if no token.
    Used for endpoints that work both authenticated and unauthenticated.
    """
    if not authorization:
        return None
    try:
        return await require_auth(authorization)
    except HTTPException:
        return None


def extract_token_from_query(token: Optional[str] = None) -> Optional[str]:
    """
    Extract token from query parameter (?token=xxx).
    Used for WebSocket connections — browsers can't set custom headers
    on WebSocket upgrade requests.
    """
    return token
