import asyncio

import httpx

from app.source import TarkovDataSource


def test_source_localizes_items_and_traders() -> None:
    payloads = {
        "/regular/items": {
            "data": {
                "items": {
                    "item-1": {
                        "id": "item-1",
                        "name": "item-1 Name",
                        "shortName": "item-1 ShortName",
                        "description": "item-1 Description",
                        "properties": {"slotName": "MOD_MAGAZINE"},
                    }
                }
            },
            "translations": ["$.data.items.*.name"],
        },
        "/regular/items_en": {
            "data": {
                "item-1 Name": "English item",
                "item-1 ShortName": "EN",
                "item-1 Description": "English description",
                "MOD_MAGAZINE": "Magazine",
            }
        },
        "/regular/items_ru": {
            "data": {
                "item-1 Name": "Русский предмет",
                "item-1 ShortName": "РУ",
                # Description is intentionally absent: English fallback is expected.
                "MOD_MAGAZINE": "Магазин",
            }
        },
        "/regular/traders": {
            "data": {
                "trader-1": {
                    "id": "trader-1",
                    "name": "trader-1 Nickname",
                    "normalizedName": "test-trader",
                }
            },
            "translations": ["$.data.*.name"],
        },
        "/regular/traders_en": {
            "data": {"trader-1 Nickname": "English trader"}
        },
        "/regular/traders_ru": {
            "data": {"trader-1 Nickname": "Русский торговец"}
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads[request.url.path])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            source = TarkovDataSource(
                base_url="https://example.test",
                game_mode="regular",
                language="ru",
                timeout_seconds=5,
                user_agent="test",
                client=client,
            )
            bundle = await source.fetch()

        assert bundle.items[0]["name"] == "Русский предмет"
        assert bundle.items[0]["shortName"] == "РУ"
        assert bundle.items[0]["description"] == "English description"
        assert bundle.items[0]["properties"]["slotName"] == "Магазин"
        assert bundle.traders[0]["name"] == "Русский торговец"

    asyncio.run(run())
