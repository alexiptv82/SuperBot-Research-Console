from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .advisers import AdviserContext
from .schema import ExpertSignal, SourceType


@dataclass(frozen=True)
class RemoteAdviserConfig:
    adviser_id: str
    version: str
    url: str
    enabled: bool = False
    api_key_env: str | None = None
    timeout_seconds: float = 15.0
    min_interval_seconds: float = 60.0
    max_calls_per_hour: int = 60

    def validate(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"https", "http"}:
            raise ValueError("remote adviser URL must use http/https")
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and host not in {
            "127.0.0.1", "localhost", "::1"
        }:
            raise ValueError("non-local remote adviser URL must use https")
        if not self.adviser_id.strip() or not self.version.strip():
            raise ValueError("adviser_id and version are required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        if self.max_calls_per_hour < 1:
            raise ValueError("max_calls_per_hour must be >= 1")


class RemoteJSONAdviser:
    """Provider-neutral remote model bridge with hard call-rate guards.

    The endpoint must accept a normalized JSON context and return:
      direction, confidence, expected_edge_bps, optional metadata.

    No provider API credential is stored in the repository. If required, the
    bearer token is read only from the configured environment variable.
    """

    def __init__(self, config: RemoteAdviserConfig):
        config.validate()
        self.config = config
        self.adviser_id = config.adviser_id
        self.version = config.version
        self._last_call_monotonic = 0.0
        self._calls: deque[float] = deque()

    def _allow_call(self) -> bool:
        if not self.config.enabled:
            return False
        now = time.monotonic()
        if (
            self._last_call_monotonic
            and now - self._last_call_monotonic
            < self.config.min_interval_seconds
        ):
            return False
        cutoff = now - 3600.0
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        return len(self._calls) < self.config.max_calls_per_hour

    @staticmethod
    def parse_response(
        *,
        adviser_id: str,
        version: str,
        symbol: str,
        payload: dict[str, Any],
    ) -> ExpertSignal:
        direction = float(payload["direction"])
        confidence = float(payload["confidence"])
        edge = float(payload.get("expected_edge_bps", 0.0))
        metadata = dict(payload.get("metadata") or {})
        metadata.update({
            "adviser_id": adviser_id,
            "adviser_version": version,
            "adviser_type": "remote-json-bridge",
        })
        return ExpertSignal(
            expert_id=f"{adviser_id}@{version}",
            source_type=SourceType.MODEL,
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            expected_edge_bps=edge,
            metadata=metadata,
        ).normalized()

    async def advise(self, context: AdviserContext) -> ExpertSignal | None:
        if not self._allow_call():
            return None

        headers = {"Accept": "application/json"}
        if self.config.api_key_env:
            secret = os.environ.get(self.config.api_key_env)
            if not secret:
                raise RuntimeError(
                    f"missing remote adviser credential env: {self.config.api_key_env}"
                )
            headers["Authorization"] = f"Bearer {secret}"

        request_payload = {
            "contract_version": "superbot-adviser-v1",
            "symbol": context.symbol,
            "regime": context.regime,
            "market": context.market,
            "news_policy": context.news_policy,
            "features": context.features,
            "response_contract": {
                "direction": "float in [-1,1]",
                "confidence": "float in [0,1]",
                "expected_edge_bps": "float",
                "metadata": "object optional",
            },
        }

        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(
                self.config.url,
                json=request_payload,
            ) as response:
                response.raise_for_status()
                payload = await response.json()

        now = time.monotonic()
        self._last_call_monotonic = now
        self._calls.append(now)
        return self.parse_response(
            adviser_id=self.adviser_id,
            version=self.version,
            symbol=context.symbol,
            payload=payload,
        )

    def status(self) -> dict[str, Any]:
        return {
            "adviser_id": self.adviser_id,
            "version": self.version,
            "enabled": self.config.enabled,
            "url": self.config.url,
            "api_key_env": self.config.api_key_env,
            "timeout_seconds": self.config.timeout_seconds,
            "min_interval_seconds": self.config.min_interval_seconds,
            "max_calls_per_hour": self.config.max_calls_per_hour,
            "calls_in_last_hour": len(self._calls),
        }
