from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC and restore timezone information SQLite does not retain."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Any,
    ) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_mode: Mapped[str] = mapped_column(String(24), primary_key=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    types_search: Mapped[str] = mapped_column(Text, nullable=False, default="|")
    categories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    handbook_categories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[float | None] = mapped_column(Float)
    min_level_for_flea: Mapped[int | None] = mapped_column(Integer)

    base_price: Mapped[int | None] = mapped_column(Integer)
    last_low_price: Mapped[int | None] = mapped_column(Integer)
    avg_24h_price: Mapped[int | None] = mapped_column(Integer)
    low_24h_price: Mapped[int | None] = mapped_column(Integer)
    high_24h_price: Mapped[int | None] = mapped_column(Integer)
    change_48h: Mapped[float | None] = mapped_column(Float)
    change_48h_percent: Mapped[float | None] = mapped_column(Float)
    last_offer_count: Mapped[int | None] = mapped_column(Integer)
    best_trader_sell_price: Mapped[int | None] = mapped_column(Integer)
    best_trader_sell_id: Mapped[str | None] = mapped_column(String(64))
    price_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    icon_url: Mapped[str | None] = mapped_column(Text)
    grid_image_url: Mapped[str | None] = mapped_column(Text)
    inspect_image_url: Mapped[str | None] = mapped_column(Text)
    wiki_url: Mapped[str | None] = mapped_column(Text)
    tarkov_dev_url: Mapped[str | None] = mapped_column(Text)

    upstream_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_items_mode_active_name", "game_mode", "active", "normalized_name"),
        Index("ix_items_mode_last_low_price", "game_mode", "last_low_price"),
    )


class Trader(Base):
    __tablename__ = "traders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_mode: Mapped[str] = mapped_column(String(24), primary_key=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PriceOffer(Base):
    __tablename__ = "price_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    game_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    trader_id: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    price_rub: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    min_trader_level: Mapped[int | None] = mapped_column(Integer)
    buy_limit: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "game_mode",
            "kind",
            "trader_id",
            "currency",
            name="uq_price_offer",
        ),
        Index("ix_price_offers_item_mode", "item_id", "game_mode"),
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    game_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_low_price: Mapped[int | None] = mapped_column(Integer)
    avg_24h_price: Mapped[int | None] = mapped_column(Integer)
    low_24h_price: Mapped[int | None] = mapped_column(Integer)
    high_24h_price: Mapped[int | None] = mapped_column(Integer)
    best_trader_sell_price: Mapped[int | None] = mapped_column(Integer)
    offer_count: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_price_snapshots_item_mode_time", "item_id", "game_mode", "observed_at"),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    game_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    items_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
