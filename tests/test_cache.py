import asyncio

from app.cache import RedisResponseCache, build_cache_key


class FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.expirations: dict[str, int] = {}
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, ex: int) -> bool:
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def scan(
        self,
        *,
        cursor: int | bytes,
        match: str,
        count: int,
    ) -> tuple[int, list[str]]:
        prefix = match.removesuffix("*")
        return 0, [key for key in self.values if key.startswith(prefix)]

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.values:
                deleted += 1
                del self.values[key]
        return deleted

    async def aclose(self) -> None:
        self.closed = True


def test_cache_key_normalizes_query_parameter_order() -> None:
    first = build_cache_key(
        "test",
        "GET",
        "/api/v1/items",
        b"q=bolt&type=barter&limit=50",
    )
    second = build_cache_key(
        "test",
        "GET",
        "/api/v1/items",
        b"limit=50&type=barter&q=bolt",
    )

    assert first == second


def test_redis_response_cache_uses_one_hour_ttl_and_clears_namespace() -> None:
    async def run() -> None:
        client = FakeRedisClient()
        cache = RedisResponseCache(
            url="redis://unused:6379/0",
            namespace="test:v1",
            ttl_seconds=3600,
            max_response_bytes=1024,
            socket_timeout_seconds=1,
            client=client,
        )

        assert await cache.ping() is True
        assert await cache.set("test:v1:response:first", b"payload") is True
        assert client.expirations["test:v1:response:first"] == 3600
        lookup = await cache.get("test:v1:response:first")
        assert lookup.available is True
        assert lookup.value == b"payload"
        assert await cache.clear() == 1
        assert (await cache.get("test:v1:response:first")).value is None
        await cache.close()
        assert client.closed is True

    asyncio.run(run())
