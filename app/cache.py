import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlencode

from redis.asyncio import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)

CacheStatus = Literal["ok", "unavailable"]


@dataclass(slots=True, frozen=True)
class CacheLookup:
    value: bytes | None
    available: bool


class CacheBackend(Protocol):
    namespace: str
    max_response_bytes: int

    @property
    def status(self) -> CacheStatus: ...

    async def ping(self) -> bool: ...

    async def get(self, key: str) -> CacheLookup: ...

    async def set(self, key: str, value: bytes) -> bool: ...

    async def clear(self) -> int: ...

    async def close(self) -> None: ...


def build_cache_key(
    namespace: str,
    method: str,
    path: str,
    raw_query_string: bytes,
) -> str:
    query_string = raw_query_string.decode("utf-8", errors="replace")
    query_pairs = sorted(parse_qsl(query_string, keep_blank_values=True))
    canonical_query = urlencode(query_pairs)
    signature = f"{method.upper()}:{path}?{canonical_query}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    return f"{namespace}:response:{digest}"


class RedisResponseCache:
    def __init__(
        self,
        *,
        url: str,
        namespace: str,
        ttl_seconds: int,
        max_response_bytes: int,
        socket_timeout_seconds: float,
        client: Any | None = None,
    ) -> None:
        self.namespace = namespace.rstrip(":")
        self.ttl_seconds = ttl_seconds
        self.max_response_bytes = max_response_bytes
        self._status: CacheStatus = "unavailable"
        self._warned_unavailable = False
        self._client = client or Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            health_check_interval=30,
        )

    @property
    def status(self) -> CacheStatus:
        return self._status

    async def ping(self) -> bool:
        try:
            await self._client.ping()
        except RedisError as exc:
            self._mark_unavailable(exc)
            return False
        self._mark_available()
        return True

    async def get(self, key: str) -> CacheLookup:
        try:
            value = await self._client.get(key)
        except RedisError as exc:
            self._mark_unavailable(exc)
            return CacheLookup(value=None, available=False)
        self._mark_available()
        return CacheLookup(value=value, available=True)

    async def set(self, key: str, value: bytes) -> bool:
        if len(value) > self.max_response_bytes:
            return False
        try:
            await self._client.set(key, value, ex=self.ttl_seconds)
        except RedisError as exc:
            self._mark_unavailable(exc)
            return False
        self._mark_available()
        return True

    async def clear(self) -> int:
        deleted = 0
        cursor: int | bytes = 0
        pattern = f"{self.namespace}:response:*"
        try:
            while True:
                cursor, keys = await self._client.scan(
                    cursor=cursor,
                    match=pattern,
                    count=500,
                )
                if keys:
                    deleted += int(await self._client.delete(*keys))
                if int(cursor) == 0:
                    break
        except RedisError as exc:
            self._mark_unavailable(exc)
            return deleted
        self._mark_available()
        return deleted

    async def close(self) -> None:
        await self._client.aclose()

    def _mark_available(self) -> None:
        if self._warned_unavailable:
            logger.info("Redis response cache is available again")
        self._status = "ok"
        self._warned_unavailable = False

    def _mark_unavailable(self, exc: RedisError) -> None:
        self._status = "unavailable"
        if not self._warned_unavailable:
            logger.warning("Redis unavailable, falling back to SQLite: %s", exc)
            self._warned_unavailable = True
