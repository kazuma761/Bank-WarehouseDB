"""SchemaCache — Redis-backed persistence for AnnotatedSchema objects."""

from __future__ import annotations

import redis.asyncio as aioredis

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import AnnotatedSchema

logger = get_logger(__name__)

_KEY_PREFIX = "mercer:schema:"
_SCAN_PATTERN = f"{_KEY_PREFIX}*"


def _key(db_url_hash: str) -> str:
    return f"{_KEY_PREFIX}{db_url_hash}"


class SchemaCache:
    """Async Redis cache for AnnotatedSchema objects, keyed by db_url_hash."""

    def __init__(self, redis_url: str, ttl_seconds: int = 3600) -> None:
        self._client: aioredis.Redis = aioredis.from_url(
            redis_url, decode_responses=True
        )
        self._ttl = ttl_seconds

    @classmethod
    def _from_client(
        cls, client: aioredis.Redis, ttl_seconds: int = 3600
    ) -> SchemaCache:
        """Construct a SchemaCache from an already-connected client (used in tests)."""
        instance = cls.__new__(cls)
        instance._client = client
        instance._ttl = ttl_seconds
        return instance

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get(self, db_url_hash: str) -> AnnotatedSchema | None:
        """Return the cached schema or None if not found / expired."""
        raw = await self._client.get(_key(db_url_hash))
        if raw is None:
            logger.debug("schema_cache_miss", db_url_hash=db_url_hash)
            return None
        logger.debug("schema_cache_hit", db_url_hash=db_url_hash)
        return AnnotatedSchema.model_validate_json(raw)

    async def set(self, db_url_hash: str, schema: AnnotatedSchema) -> None:
        """Serialize and store the schema with TTL."""
        payload = schema.model_dump_json()
        await self._client.set(_key(db_url_hash), payload, ex=self._ttl)
        logger.debug("schema_cache_set", db_url_hash=db_url_hash, ttl=self._ttl)

    async def invalidate(self, db_url_hash: str) -> None:
        """Delete a single schema entry."""
        await self._client.delete(_key(db_url_hash))
        logger.debug("schema_cache_invalidated", db_url_hash=db_url_hash)

    async def invalidate_all(self) -> int:
        """Delete all mercer:schema:* keys and return the count deleted."""
        keys: list[str] = []
        async for key in self._client.scan_iter(_SCAN_PATTERN):
            keys.append(key)
        if not keys:
            return 0
        count: int = await self._client.delete(*keys)
        logger.info("schema_cache_invalidated_all", count=count)
        return count

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._client.aclose()
