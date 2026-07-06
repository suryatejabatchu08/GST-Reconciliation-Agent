"""
orchestration_service/tests/test_llm_gateway.py
Unit tests for the LLM gateway — rate limiter, circuit breaker, mock responses.
All tests mock the actual LLM API calls to avoid network dependency.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestration_service.llm_gateway import (
    CircuitBreaker, CircuitState, TokenBucketRateLimiter, LLMGateway
)


class TestCircuitBreaker:

    def test_starts_closed(self):
        cb = CircuitBreaker(name="test")
        assert cb._state == CircuitState.CLOSED
        assert cb.can_attempt() is True

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb._state == CircuitState.CLOSED
        cb.record_failure()   # 3rd failure
        assert cb._state == CircuitState.OPEN
        assert cb.is_open is True

    def test_closed_after_success(self):
        cb = CircuitBreaker(name="test", failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        cb.record_success()
        assert cb._state == CircuitState.CLOSED

    def test_blocks_when_open(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=9999)
        cb.record_failure()
        assert cb.can_attempt() is False

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure()
        time.sleep(0.01)   # tiny sleep > 0 seconds
        assert cb.can_attempt() is True
        assert cb._state == CircuitState.HALF_OPEN


class TestTokenBucketRateLimiter:

    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        """Should not delay when tokens are available."""
        limiter = TokenBucketRateLimiter(rpm=60, name="test")
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1   # Should be near-instant

    @pytest.mark.asyncio
    async def test_rate_limits_after_bucket_empty(self):
        """Should delay when token bucket is empty."""
        limiter = TokenBucketRateLimiter(rpm=60, name="test")
        # Drain all tokens
        limiter._tokens = 0
        limiter._last_refill = time.monotonic()

        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        # Should wait ~1 second for 1 token at 1 token/second (60 RPM)
        assert elapsed >= 0.9


class TestLLMGateway:

    @pytest.mark.asyncio
    async def test_returns_stub_when_no_keys_configured(self):
        """When both API keys are empty, should return stub response."""
        with patch("orchestration_service.llm_gateway.get_settings") as mock_settings:
            mock_settings.return_value.gemini_api_key = ""
            mock_settings.return_value.groq_api_key = ""
            mock_settings.return_value.gemini_rpm_limit = 15
            mock_settings.return_value.groq_rpm_limit = 30

            gateway = LLMGateway()
            result = await gateway.generate("test prompt", provider="auto")
            assert result["text"] == "CLASSIFICATION_UNAVAILABLE"
            assert result["provider"] == "none"

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self):
        """After 3 Gemini failures, circuit should open."""
        with patch("orchestration_service.llm_gateway.get_settings") as mock_settings:
            mock_settings.return_value.gemini_api_key = "fake-key"
            mock_settings.return_value.groq_api_key = ""
            mock_settings.return_value.gemini_rpm_limit = 60
            mock_settings.return_value.groq_rpm_limit = 60

            gateway = LLMGateway()
            # Simulate 3 failures
            gateway._gemini_cb.record_failure()
            gateway._gemini_cb.record_failure()
            gateway._gemini_cb.record_failure()

            assert gateway._gemini_cb.is_open

    def test_usage_stats_structure(self):
        """Usage stats should have expected keys."""
        with patch("orchestration_service.llm_gateway.get_settings") as mock_settings:
            mock_settings.return_value.gemini_api_key = ""
            mock_settings.return_value.groq_api_key = ""
            mock_settings.return_value.gemini_rpm_limit = 15
            mock_settings.return_value.groq_rpm_limit = 30

            gateway = LLMGateway()
            stats = gateway.usage_stats
            assert "gemini_calls_today" in stats
            assert "groq_calls_today" in stats
            assert "gemini_circuit" in stats
            assert "groq_circuit" in stats
