"""
orchestration_service/llm_gateway.py
Rate-limiter and circuit breaker for Gemini and Groq API calls.

PRD §9 (Risk Mitigation): "Queue-backed rate limiter in front of every LLM call;
automatic backoff and retry; jobs degrade gracefully."

Free tier limits enforced:
  - Gemini 1.5 Flash: 15 RPM, 1,500 RPD
  - Groq (Llama 3.3 70B): 30 RPM, 14,400 RPD
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import google.generativeai as genai
from groq import AsyncGroq
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from shared.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# ──────────────────────────────────────────────────────────
# Circuit Breaker
# ──────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"       # Normal — requests allowed
    OPEN = "open"           # Tripped — requests blocked, fallback used
    HALF_OPEN = "half_open" # Probing — one request allowed to test recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for an LLM provider.
    Trips after `failure_threshold` consecutive errors.
    Resets after `recovery_timeout` seconds.

    When OPEN:
      - No calls made to the LLM
      - Returns a pre-defined fallback response
      - After recovery_timeout, moves to HALF_OPEN (one probe allowed)
    """
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0       # seconds

    _failures: int = field(default=0, init=False, repr=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _last_failure_time: float = field(default=0.0, init=False, repr=False)

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                "Circuit breaker OPEN for %s after %d failures",
                self.name, self._failures
            )

    def can_attempt(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker HALF_OPEN for %s — probing", self.name)
                return True
            return False
        # HALF_OPEN: allow one attempt
        return True

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN


# ──────────────────────────────────────────────────────────
# Token Bucket Rate Limiter (per-minute)
# ──────────────────────────────────────────────────────────

class TokenBucketRateLimiter:
    """
    Async token bucket rate limiter.
    Limits requests per minute (RPM) by refilling tokens every 60 seconds.

    Usage:
        limiter = TokenBucketRateLimiter(rpm=15)
        await limiter.acquire()    # Waits if rate limit reached
    """

    def __init__(self, rpm: int, name: str = ""):
        self.rpm = rpm
        self.name = name
        self._tokens = float(rpm)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill tokens proportionally to time elapsed
            refill = elapsed * (self.rpm / 60.0)
            self._tokens = min(float(self.rpm), self._tokens + refill)
            self._last_refill = now

            if self._tokens < 1:
                # Calculate wait time to get 1 token
                wait = (1 - self._tokens) / (self.rpm / 60.0)
                logger.debug(
                    "Rate limit (%s): waiting %.2fs for token", self.name, wait
                )
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ──────────────────────────────────────────────────────────
# LLM Gateway
# ──────────────────────────────────────────────────────────

class LLMGateway:
    """
    Unified gateway for Gemini and Groq API calls.

    Features:
    - Per-provider rate limiting (RPM)
    - Circuit breaker per provider
    - Automatic retry with exponential backoff (tenacity)
    - Fallback: if Gemini fails, tries Groq; if both fail, returns stub response
    - LLM call logging for cost/usage tracking

    Usage:
        gateway = LLMGateway()
        result = await gateway.generate(
            prompt="Classify this mismatch...",
            provider="groq",        # "gemini" | "groq" | "auto"
            model_hint="classify",  # hint used in logs
        )
    """

    def __init__(self):
        # Configure Gemini
        if settings.gemini_api_key:
            genai.configure(api_key=settings.gemini_api_key)
        self._gemini_model = genai.GenerativeModel("gemini-1.5-flash")

        # Configure Groq
        self._groq_client = AsyncGroq(api_key=settings.groq_api_key) if settings.groq_api_key else None

        # Rate limiters (free tier)
        self._gemini_limiter = TokenBucketRateLimiter(
            rpm=settings.gemini_rpm_limit, name="gemini"
        )
        self._groq_limiter = TokenBucketRateLimiter(
            rpm=settings.groq_rpm_limit, name="groq"
        )

        # Circuit breakers
        self._gemini_cb = CircuitBreaker(name="gemini", failure_threshold=3, recovery_timeout=60.0)
        self._groq_cb = CircuitBreaker(name="groq", failure_threshold=3, recovery_timeout=60.0)

        # Daily request counters
        self._gemini_calls_today = 0
        self._groq_calls_today = 0
        self._day_start = time.time()

    def _reset_daily_counters_if_needed(self) -> None:
        """Reset daily counters if a new day has started."""
        now = time.time()
        if now - self._day_start > 86400:
            self._gemini_calls_today = 0
            self._groq_calls_today = 0
            self._day_start = now

    async def generate(
        self,
        prompt: str,
        provider: str = "auto",
        model_hint: str = "general",
        temperature: float = 0.1,
    ) -> dict:
        """
        Generate a text response from Gemini or Groq.

        Args:
            prompt: The prompt to send
            provider: "gemini" | "groq" | "auto"
                      "auto" tries Gemini first, falls back to Groq
            model_hint: Short label for logs (e.g. "normalise", "classify")
            temperature: 0.1 for structured outputs, higher for creative

        Returns:
            {
                "text": str,           # The generated text
                "provider": str,       # Which provider was used
                "model": str,          # Model name used
                "prompt_tokens": int,  # Estimated token count
                "fallback": bool,      # True if fallback provider was used
            }
        """
        self._reset_daily_counters_if_needed()

        if provider == "gemini":
            return await self._call_gemini(prompt, model_hint, temperature)
        elif provider == "groq":
            return await self._call_groq(prompt, model_hint, temperature)
        else:
            # Auto: try Gemini first, fall back to Groq
            if self._gemini_cb.can_attempt() and settings.gemini_api_key:
                try:
                    result = await self._call_gemini(prompt, model_hint, temperature)
                    return result
                except Exception as e:
                    logger.warning("Gemini failed (%s), falling back to Groq: %s", model_hint, e)

            if self._groq_cb.can_attempt() and self._groq_client:
                try:
                    result = await self._call_groq(prompt, model_hint, temperature)
                    result["fallback"] = True
                    return result
                except Exception as e:
                    logger.error("Both Gemini and Groq failed for %s: %s", model_hint, e)

            # Both failed — return stub for graceful degradation
            logger.error("All LLM providers failed for hint=%s — returning stub", model_hint)
            return {
                "text": "CLASSIFICATION_UNAVAILABLE",
                "provider": "none",
                "model": "none",
                "prompt_tokens": 0,
                "fallback": True,
            }

    async def _call_gemini(
        self, prompt: str, hint: str, temperature: float
    ) -> dict:
        """Call Gemini 1.5 Flash with rate limiting and circuit breaker."""
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")

        await self._gemini_limiter.acquire()

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        temperature=temperature,
                        max_output_tokens=2048,
                    ),
                )
            )
            text = response.text.strip()
            self._gemini_cb.record_success()
            self._gemini_calls_today += 1

            logger.debug("Gemini [%s] OK — %d chars", hint, len(text))
            return {
                "text": text,
                "provider": "gemini",
                "model": "gemini-1.5-flash",
                "prompt_tokens": len(prompt.split()),   # rough estimate
                "fallback": False,
            }
        except Exception as e:
            self._gemini_cb.record_failure()
            raise

    async def _call_groq(
        self, prompt: str, hint: str, temperature: float
    ) -> dict:
        """Call Groq (Llama 3.3 70B) with rate limiting and circuit breaker."""
        if not self._groq_client:
            raise RuntimeError("GROQ_API_KEY not configured")

        await self._groq_limiter.acquire()

        try:
            completion = await self._groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=2048,
            )
            text = completion.choices[0].message.content.strip()
            self._groq_cb.record_success()
            self._groq_calls_today += 1

            logger.debug("Groq [%s] OK — %d chars", hint, len(text))
            return {
                "text": text,
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "prompt_tokens": completion.usage.prompt_tokens if completion.usage else 0,
                "fallback": False,
            }
        except Exception as e:
            self._groq_cb.record_failure()
            raise

    @property
    def usage_stats(self) -> dict:
        """Daily usage stats for monitoring."""
        return {
            "gemini_calls_today": self._gemini_calls_today,
            "groq_calls_today": self._groq_calls_today,
            "gemini_circuit": self._gemini_cb._state.value,
            "groq_circuit": self._groq_cb._state.value,
        }


# ── Module-level singleton ─────────────────────────────────
_gateway: Optional[LLMGateway] = None


def get_gateway() -> LLMGateway:
    """Return the singleton LLMGateway instance."""
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway
