import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


class SourceError(RuntimeError):
    """Raised when the upstream response cannot be fetched or validated."""


@dataclass(slots=True, frozen=True)
class SourceBundle:
    items: list[dict[str, Any]]
    traders: list[dict[str, Any]]
    fetched_at: datetime
    source_url: str


class TarkovDataSource:
    """Client for the current static tarkov.dev data feed."""

    def __init__(
        self,
        base_url: str,
        game_mode: str,
        language: str,
        timeout_seconds: float,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.game_mode = game_mode
        self.language = language
        self.timeout = httpx.Timeout(timeout_seconds)
        self.headers = {
            "Accept": "application/json",
            "User-Agent": user_agent,
        }
        self._client = client

    async def fetch(self) -> SourceBundle:
        if self._client is not None:
            items, traders = await asyncio.gather(
                self._fetch_dataset(self._client, "items"),
                self._fetch_dataset(self._client, "traders"),
            )
        else:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers=self.headers,
                follow_redirects=True,
            ) as client:
                items, traders = await asyncio.gather(
                    self._fetch_dataset(client, "items"),
                    self._fetch_dataset(client, "traders"),
                )

        return SourceBundle(
            items=self._as_records(items, "items"),
            traders=self._as_records(traders, "traders"),
            fetched_at=datetime.now(UTC),
            source_url=f"{self.base_url}/{self.game_mode}/items",
        )

    async def _fetch_dataset(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
    ) -> dict[str, Any]:
        root = f"{self.base_url}/{self.game_mode}/{endpoint}"
        urls = [root, f"{root}_en"]
        if self.language != "en":
            urls.append(f"{root}_{self.language}")

        payloads = await asyncio.gather(*(self._get_json(client, url) for url in urls))
        base_payload = payloads[0]
        fallback = self._translation_map(payloads[1], f"{root}_en")
        localized = fallback if self.language == "en" else self._translation_map(
            payloads[2],
            f"{root}_{self.language}",
        )

        data = base_payload.get("data")
        if not isinstance(data, dict):
            raise SourceError(f"Invalid payload from {root}: 'data' must be an object")

        return self._translate_tree(data, localized, fallback)

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, 0.5, 1.5), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await client.get(url, headers=self.headers, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SourceError(f"Invalid JSON root from {url}: expected an object")
                return payload
            except (httpx.HTTPError, ValueError, SourceError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status < 500 and status != 429:
                        break
                if attempt == 3:
                    break

        raise SourceError(f"Cannot fetch {url}: {last_error}") from last_error

    @staticmethod
    def _translation_map(payload: dict[str, Any], url: str) -> dict[str, str]:
        data = payload.get("data")
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data.items()
        ):
            raise SourceError(f"Invalid translation payload from {url}")
        return data

    @classmethod
    def _translate_tree(
        cls,
        value: Any,
        localized: dict[str, str],
        fallback: dict[str, str],
    ) -> Any:
        if isinstance(value, str):
            return localized.get(value, fallback.get(value, value))
        if isinstance(value, list):
            return [cls._translate_tree(item, localized, fallback) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._translate_tree(item, localized, fallback)
                for key, item in value.items()
            }
        return value

    @staticmethod
    def _as_records(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
        records = data.get(key)
        if records is None and data and all(
            isinstance(record, dict) for record in data.values()
        ):
            # Some feed documents (currently traders) expose the keyed collection
            # directly under `data` instead of wrapping it in a named property.
            records = data
        if isinstance(records, dict):
            values = list(records.values())
        elif isinstance(records, list):
            values = records
        else:
            raise SourceError(f"Invalid '{key}' collection in upstream payload")

        if not all(isinstance(record, dict) for record in values):
            raise SourceError(f"Invalid record inside upstream '{key}' collection")
        return values
