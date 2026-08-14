from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FleaPrice(BaseModel):
    last_low: int | None = None
    average_24h: int | None = None
    low_24h: int | None = None
    high_24h: int | None = None
    change_48h: float | None = None
    change_48h_percent: float | None = None
    offer_count: int | None = None
    scanned_at: datetime | None = None


class BestTraderPrice(BaseModel):
    trader_id: str
    trader_name: str | None = None
    price_rub: int


class PriceSummary(BaseModel):
    base: int | None = None
    flea: FleaPrice
    best_trader_sell: BestTraderPrice | None = None


class ItemSummary(BaseModel):
    id: str
    name: str
    short_name: str
    normalized_name: str
    types: list[str]
    icon_url: str | None = None
    grid_image_url: str | None = None
    prices: PriceSummary
    updated_at: datetime | None = None


class PriceOfferOut(BaseModel):
    kind: Literal["buy", "sell"]
    trader_id: str
    trader_name: str | None = None
    price: int
    price_rub: int
    currency: str
    min_trader_level: int | None = None
    buy_limit: int | None = None


class ItemDetail(ItemSummary):
    description: str | None = None
    width: int | None = None
    height: int | None = None
    weight: float | None = None
    min_level_for_flea: int | None = None
    categories: list[str]
    handbook_categories: list[str]
    properties: dict[str, Any] | None = None
    inspect_image_url: str | None = None
    wiki_url: str | None = None
    tarkov_dev_url: str | None = None
    buy_offers: list[PriceOfferOut]
    sell_offers: list[PriceOfferOut]
    last_scan_at: datetime | None = None
    synced_at: datetime
    raw_data: dict[str, Any] | None = None


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int


class ItemListResponse(BaseModel):
    meta: Pagination
    items: list[ItemSummary]


class PriceSnapshotOut(BaseModel):
    observed_at: datetime
    last_low_price: int | None = None
    avg_24h_price: int | None = None
    low_24h_price: int | None = None
    high_24h_price: int | None = None
    best_trader_sell_price: int | None = None
    offer_count: int | None = None

    model_config = ConfigDict(from_attributes=True)


class PriceHistoryResponse(BaseModel):
    item_id: str
    points: list[PriceSnapshotOut]


class TraderOut(BaseModel):
    id: str
    name: str
    normalized_name: str
    image_url: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SyncRunOut(BaseModel):
    id: int
    trigger: str
    status: str
    source_url: str
    game_mode: str
    language: str
    started_at: datetime
    finished_at: datetime | None = None
    items_received: int
    items_upserted: int
    snapshots_created: int
    error: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SyncResponse(BaseModel):
    run: SyncRunOut


class HealthResponse(BaseModel):
    status: Literal["ok", "starting", "degraded"]
    database: Literal["ok"]
    cache: Literal["ok", "unavailable", "disabled"]
    active_items: int
    sync_running: bool
    last_sync: SyncRunOut | None = None


class ExportMeta(BaseModel):
    generated_at: datetime
    game_mode: str
    language: str
    count: int
    source_synced_at: datetime | None = None


class ExportResponse(BaseModel):
    meta: ExportMeta
    items: dict[str, ItemSummary] | list[ItemSummary] = Field(default_factory=dict)
