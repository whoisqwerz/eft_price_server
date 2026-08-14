from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.cache import CacheBackend, build_cache_key


def _upsert_header(
    headers: list[tuple[bytes, bytes]],
    name: bytes,
    value: bytes,
) -> list[tuple[bytes, bytes]]:
    lowered = name.lower()
    result = [(key, val) for key, val in headers if key.lower() != lowered]
    result.append((lowered, value))
    return result


class RedisCacheMiddleware:
    """Serve successful JSON GET responses from Redis before routes touch SQLite."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        cache: CacheBackend,
        path_prefixes: Sequence[str],
    ) -> None:
        self.app = app
        self.cache = cache
        self.path_prefixes = tuple(path_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_cacheable_request(scope):
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET"))
        path = str(scope.get("path", ""))
        raw_query_string = scope.get("query_string", b"")
        key = build_cache_key(
            self.cache.namespace,
            method,
            path,
            raw_query_string,
        )
        lookup = await self.cache.get(key)

        if lookup.value is not None:
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(lookup.value)).encode("ascii")),
                (b"x-cache", b"HIT"),
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": headers,
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": lookup.value,
                    "more_body": False,
                }
            )
            return

        cache_result = b"MISS" if lookup.available else b"BYPASS"
        body_parts: list[bytes] = []
        body_size = 0
        cache_candidate = False

        async def send_wrapper(message: Message) -> None:
            nonlocal body_size, cache_candidate

            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = next(
                    (
                        value.lower()
                        for name, value in headers
                        if name.lower() == b"content-type"
                    ),
                    b"",
                )
                cache_candidate = (
                    message["status"] == 200
                    and b"application/json" in content_type
                )
                headers = _upsert_header(headers, b"x-cache", cache_result)
                message = {**message, "headers": headers}

            elif message["type"] == "http.response.body" and cache_candidate:
                chunk = message.get("body", b"")
                body_size += len(chunk)
                if body_size <= self.cache.max_response_bytes:
                    body_parts.append(chunk)
                else:
                    cache_candidate = False
                    body_parts.clear()

            await send(message)

            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and cache_candidate
            ):
                await self.cache.set(key, b"".join(body_parts))

        await self.app(scope, receive, send_wrapper)

    def _is_cacheable_request(self, scope: Scope) -> bool:
        if scope["type"] != "http" or scope.get("method") != "GET":
            return False
        path = str(scope.get("path", ""))
        return any(path.startswith(prefix) for prefix in self.path_prefixes)
