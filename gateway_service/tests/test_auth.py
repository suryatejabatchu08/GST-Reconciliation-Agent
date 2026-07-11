"""
gateway_service/tests/test_auth.py
Unit tests for JWT authentication.
Uses real JWT encoding to test validation logic — no mocking needed.
"""

import time
import pytest
from unittest.mock import patch
from jose import jwt

from gateway_service.auth import _decode_token, TokenPayload
from fastapi import HTTPException


SECRET = "test-jwt-secret-key-for-unit-tests-only"
ALGORITHM = "HS256"


def _make_token(
    sub: str = "user-uuid-123",
    email: str = "test@example.com",
    role: str = "authenticated",
    aud: str = "authenticated",
    exp_offset: int = 3600,  # seconds from now
) -> str:
    """Create a real Supabase-style JWT for testing."""
    payload = {
        "sub": sub,
        "email": email,
        "role": role,
        "aud": aud,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset,
        "iss": "supabase",
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


class TestDecodeToken:

    def test_valid_token_decoded(self):
        token = _make_token()
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            result = _decode_token(token)
        assert result.sub == "user-uuid-123"
        assert result.email == "test@example.com"
        assert result.role == "authenticated"

    def test_expired_token_raises_401(self):
        token = _make_token(exp_offset=-10)   # Expired 10 seconds ago
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            with pytest.raises(HTTPException) as exc_info:
                _decode_token(token)
        assert exc_info.value.status_code == 401

    def test_wrong_secret_raises_401(self):
        token = _make_token()
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = "wrong-secret"
            with pytest.raises(HTTPException) as exc_info:
                _decode_token(token)
        assert exc_info.value.status_code == 401

    def test_tampered_token_raises_401(self):
        token = _make_token()
        tampered = token[:-5] + "XXXXX"   # Corrupt the signature
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            with pytest.raises(HTTPException) as exc_info:
                _decode_token(tampered)
        assert exc_info.value.status_code == 401

    def test_garbage_string_raises_401(self):
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            with pytest.raises(HTTPException) as exc_info:
                _decode_token("not-a-jwt-at-all")
        assert exc_info.value.status_code == 401


class TestRequireAuth:

    @pytest.mark.asyncio
    async def test_no_secret_returns_dev_user(self):
        """When JWT secret not set, auth is bypassed in dev mode."""
        from gateway_service.auth import require_auth
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = ""
            result = await require_auth(authorization=None)
        assert result.sub == "dev-user"
        assert result.role == "authenticated"

    @pytest.mark.asyncio
    async def test_missing_header_raises_401(self):
        from gateway_service.auth import require_auth
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            with pytest.raises(HTTPException) as exc_info:
                await require_auth(authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_bearer_token_succeeds(self):
        from gateway_service.auth import require_auth
        token = _make_token(sub="real-user-uuid")
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            result = await require_auth(authorization=f"Bearer {token}")
        assert result.sub == "real-user-uuid"

    @pytest.mark.asyncio
    async def test_wrong_scheme_raises_401(self):
        from gateway_service.auth import require_auth
        token = _make_token()
        with patch("gateway_service.auth.settings") as mock_settings:
            mock_settings.supabase_jwt_secret = SECRET
            with pytest.raises(HTTPException) as exc_info:
                await require_auth(authorization=f"Basic {token}")   # Wrong scheme
        assert exc_info.value.status_code == 401
