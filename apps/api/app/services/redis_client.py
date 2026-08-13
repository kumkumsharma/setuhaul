from __future__ import annotations

import json
from typing import Any

import fakeredis
import redis

from app.config import get_settings

_client: redis.Redis | fakeredis.FakeRedis | None = None


def get_redis() -> redis.Redis | fakeredis.FakeRedis:
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    url = settings.redis_url
    if url.startswith("fakeredis"):
        _client = fakeredis.FakeRedis(decode_responses=True)
    else:
        # Upstash (and other managed Redis) idle-closes TLS connections; health checks
        # + short timeouts keep the pooled client usable across requests.
        from redis.backoff import ExponentialBackoff
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError
        from redis.retry import Retry

        _client = redis.Redis.from_url(
            url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=10,
            socket_timeout=10,
            socket_keepalive=True,
            retry_on_timeout=True,
            retry=Retry(
                ExponentialBackoff(cap=2, base=0.1),
                retries=3,
                supported_errors=(RedisConnectionError, RedisTimeoutError),
            ),
        )
    return _client


def reset_redis_client() -> None:
    """Test helper to force a fresh client."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None


HOLD_KEY = "hold:slot:{slot_id}"
HOLD_META_KEY = "hold:meta:{hold_id}"
IDEMPOTENCY_KEY = "idem:{key}"
EXCEPTION_HOLD_KEY = "hold:exception:{exception_id}"


def hold_slot_key(slot_id: str) -> str:
    return HOLD_KEY.format(slot_id=slot_id)


def hold_meta_key(hold_id: str) -> str:
    return HOLD_META_KEY.format(hold_id=hold_id)


def idem_key(key: str) -> str:
    return IDEMPOTENCY_KEY.format(key=key)


def exception_hold_key(exception_id: str) -> str:
    return EXCEPTION_HOLD_KEY.format(exception_id=exception_id)


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, default=str)


def loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return json.loads(raw)
