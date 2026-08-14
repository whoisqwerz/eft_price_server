from copy import deepcopy
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.cache import CacheLookup
from app.config import Settings
from app.main import create_app
from app.source import SourceBundle


ITEM = {
    "id": "item-1",
    "name": "Тестовый предмет",
    "shortName": "Тест",
    "normalizedName": "test-item",
    "description": "Описание",
    "types": ["barter"],
    "categories": ["category-1"],
    "handbookCategories": ["handbook-1"],
    "properties": {"quality": 10},
    "width": 2,
    "height": 1,
    "weight": 0.5,
    "minLevelForFlea": 15,
    "basePrice": 1000,
    "lastLowPrice": 2000,
    "avg24hPrice": 2200,
    "low24hPrice": 1800,
    "high24hPrice": 3000,
    "changeLast48h": 100,
    "changeLast48hPercent": 5.0,
    "lastOfferCount": 25,
    "lastScan": "2026-08-14T12:00:00Z",
    "updated": "2026-08-14T12:01:00Z",
    "iconLink": "https://assets.example/icon.webp",
    "gridImageLink": "https://assets.example/grid.webp",
    "inspectImageLink": "https://assets.example/image.webp",
    "wikiLink": "https://wiki.example/item",
    "link": "https://tarkov.dev/item/test-item",
    "buyFromTrader": [
        {
            "trader": "trader-1",
            "price": 2500,
            "priceRUB": 2500,
            "currency": "RUB",
            "minTraderLevel": 2,
            "buyLimit": 5,
        }
    ],
    "sellToTrader": [
        {
            "trader": "trader-1",
            "price": 900,
            "priceRUB": 900,
            "currency": "RUB",
        }
    ],
}

TRADER = {
    "id": "trader-1",
    "name": "Торговец",
    "normalizedName": "test-trader",
    "imageLink": "https://assets.example/trader.webp",
}


class FakeSource:
    def __init__(self) -> None:
        self.bundle = self.make_bundle(ITEM)

    @staticmethod
    def make_bundle(item: dict) -> SourceBundle:
        return SourceBundle(
            items=[deepcopy(item)],
            traders=[deepcopy(TRADER)],
            fetched_at=datetime.now(UTC),
            source_url="https://example.test/regular/items",
        )

    async def fetch(self) -> SourceBundle:
        return self.bundle


class MemoryCache:
    namespace = "test-cache"
    max_response_bytes = 25_000_000

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.clear_count = 0

    @property
    def status(self) -> str:
        return "ok"

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> CacheLookup:
        return CacheLookup(value=self.values.get(key), available=True)

    async def set(self, key: str, value: bytes) -> bool:
        self.values[key] = value
        return True

    async def clear(self) -> int:
        deleted = len(self.values)
        self.values.clear()
        self.clear_count += 1
        return deleted

    async def close(self) -> None:
        return None


def test_sync_search_detail_history_and_export(tmp_path) -> None:
    fake_source = FakeSource()
    memory_cache = MemoryCache()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        tarkov_source_base_url="https://example.test",
        tarkov_game_mode="regular",
        tarkov_language="ru",
        sync_on_startup=False,
        sync_interval_seconds=3600,
        admin_api_key="secret",
    )
    app = create_app(
        settings=settings,
        source=fake_source,  # type: ignore[arg-type]
        cache=memory_cache,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        initial_health = client.get("/health").json()
        assert initial_health["status"] == "starting"
        assert initial_health["cache"] == "ok"
        assert client.post("/api/v1/sync").status_code == 401

        sync = client.post(
            "/api/v1/sync",
            headers={"X-API-Key": "secret"},
        )
        assert sync.status_code == 200
        assert sync.json()["run"]["items_upserted"] == 1
        assert sync.json()["run"]["snapshots_created"] == 1
        assert memory_cache.clear_count == 1
        assert client.get("/ready").status_code == 200

        response = client.get(
            "/api/v1/items",
            params={"q": "ТЕСТОВЫЙ", "type": "barter"},
        )
        assert response.status_code == 200
        assert response.headers["x-cache"] == "MISS"
        assert response.json()["meta"]["total"] == 1
        summary = response.json()["items"][0]
        assert summary["prices"]["flea"]["last_low"] == 2000
        assert summary["prices"]["best_trader_sell"] == {
            "trader_id": "trader-1",
            "trader_name": "Торговец",
            "price_rub": 900,
        }
        cached_response = client.get(
            "/api/v1/items",
            params={"type": "barter", "q": "ТЕСТОВЫЙ"},
        )
        assert cached_response.headers["x-cache"] == "HIT"
        assert cached_response.json() == response.json()

        detail = client.get("/api/v1/items/item-1").json()
        assert detail["raw_data"] is None
        assert detail["buy_offers"][0]["min_trader_level"] == 2
        assert detail["sell_offers"][0]["trader_name"] == "Торговец"
        raw_detail = client.get(
            "/api/v1/items/item-1",
            params={"include_raw": True},
        ).json()
        assert raw_detail["raw_data"]["properties"] == {"quality": 10}

        compact = client.get("/api/v1/export/prices")
        assert compact.status_code == 200
        assert compact.headers["x-cache"] == "MISS"
        assert compact.json() == [
            {
                "id": "item-1",
                "types": ["barter"],
                "prices": {
                    "base": 1000,
                    "avg24": 2200,
                    "best_trader_price": 900,
                },
            }
        ]
        assert client.get("/api/v1/export/prices").headers["x-cache"] == "HIT"

        assert len(client.get("/api/v1/items/item-1/history").json()["points"]) == 1

        # An identical refresh must not add a duplicate price point.
        same_sync = client.post(
            "/api/v1/sync",
            headers={"X-API-Key": "secret"},
        ).json()
        assert same_sync["run"]["snapshots_created"] == 0
        assert memory_cache.clear_count == 2
        assert client.get(
            "/api/v1/items",
            params={"q": "ТЕСТОВЫЙ", "type": "barter"},
        ).headers["x-cache"] == "MISS"

        changed = deepcopy(ITEM)
        changed["lastLowPrice"] = 2100
        fake_source.bundle = fake_source.make_bundle(changed)
        changed_sync = client.post(
            "/api/v1/sync",
            headers={"X-API-Key": "secret"},
        ).json()
        assert changed_sync["run"]["snapshots_created"] == 1
        assert len(client.get("/api/v1/items/item-1/history").json()["points"]) == 2

        exported = client.get("/api/v1/export/items").json()
        assert exported["meta"]["count"] == 1
        assert exported["items"]["item-1"]["name"] == "Тестовый предмет"
        assert len(client.get("/api/v1/traders").json()) == 1


def test_missing_item_returns_404(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'empty.db'}",
        sync_on_startup=False,
        sync_interval_seconds=3600,
        redis_cache_enabled=False,
    )
    app = create_app(settings=settings, source=FakeSource())  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/api/v1/items/missing").status_code == 404
        assert client.get("/api/v1/items/missing/history").status_code == 404
