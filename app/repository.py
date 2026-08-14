import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database import Database
from app.models import (
    Item,
    ItemTranslation,
    PriceOffer,
    PriceSnapshot,
    SyncRun,
    Trader,
)
from app.source import SourceBundle


@dataclass(slots=True, frozen=True)
class SyncStats:
    run_id: int
    items_received: int
    items_upserted: int
    snapshots_created: int


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def start_sync_run(
        self,
        *,
        trigger: str,
        source_url: str,
        game_mode: str,
        language: str,
    ) -> int:
        with self.database.session() as session:
            run = SyncRun(
                trigger=trigger,
                status="running",
                source_url=source_url,
                game_mode=game_mode,
                language=language,
                started_at=datetime.now(UTC),
                items_received=0,
                items_upserted=0,
                snapshots_created=0,
            )
            session.add(run)
            session.commit()
            return run.id

    def apply_bundle(
        self,
        *,
        run_id: int,
        bundle: SourceBundle,
        game_mode: str,
        language: str,
    ) -> SyncStats:
        now = bundle.fetched_at
        item_rows: list[dict[str, Any]] = []
        offer_rows: list[dict[str, Any]] = []
        trader_rows = self._trader_rows(bundle.traders, game_mode, language, now)
        translation_rows = self._item_translation_rows(
            bundle,
            game_mode,
            language,
            now,
        )

        with self.database.session() as session:
            existing = dict(
                session.execute(
                    select(Item.id, Item.price_fingerprint).where(
                        Item.game_mode == game_mode
                    )
                ).all()
            )

            for raw_item in bundle.items:
                row, offers = self._item_row(raw_item, game_mode, language, now)
                item_rows.append(row)
                offer_rows.extend(offers)

            session.execute(
                update(Item)
                .where(Item.game_mode == game_mode)
                .values(active=False)
            )

            if item_rows:
                self._upsert_items(session, item_rows)
            if translation_rows:
                self._upsert_item_translations(session, translation_rows)
            if trader_rows:
                self._upsert_traders(session, trader_rows)

            session.execute(
                delete(PriceOffer).where(PriceOffer.game_mode == game_mode)
            )
            if offer_rows:
                session.execute(insert(PriceOffer), self._deduplicate_offers(offer_rows))

            snapshot_rows = [
                {
                    "item_id": row["id"],
                    "game_mode": game_mode,
                    "observed_at": now,
                    "last_low_price": row["last_low_price"],
                    "avg_24h_price": row["avg_24h_price"],
                    "low_24h_price": row["low_24h_price"],
                    "high_24h_price": row["high_24h_price"],
                    "best_trader_sell_price": row["best_trader_sell_price"],
                    "offer_count": row["last_offer_count"],
                }
                for row in item_rows
                if existing.get(row["id"]) != row["price_fingerprint"]
            ]
            if snapshot_rows:
                session.execute(insert(PriceSnapshot), snapshot_rows)

            session.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(
                    status="success",
                    source_url=bundle.source_url,
                    finished_at=datetime.now(UTC),
                    items_received=len(bundle.items),
                    items_upserted=len(item_rows),
                    snapshots_created=len(snapshot_rows),
                    error=None,
                )
            )
            session.commit()

        return SyncStats(
            run_id=run_id,
            items_received=len(bundle.items),
            items_upserted=len(item_rows),
            snapshots_created=len(snapshot_rows),
        )

    def fail_sync_run(self, run_id: int, error: str) -> None:
        with self.database.session() as session:
            session.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(
                    status="failed",
                    finished_at=datetime.now(UTC),
                    error=error[:4000],
                )
            )
            session.commit()

    def latest_sync(self, session: Session) -> SyncRun | None:
        return session.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))

    def latest_successful_sync(self, session: Session) -> SyncRun | None:
        return session.scalar(
            select(SyncRun)
            .where(SyncRun.status == "success")
            .order_by(SyncRun.id.desc())
            .limit(1)
        )

    @staticmethod
    def active_item_count(session: Session, game_mode: str) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(Item)
                .where(Item.game_mode == game_mode, Item.active.is_(True))
            )
            or 0
        )

    @staticmethod
    def _upsert_items(session: Session, rows: list[dict[str, Any]]) -> None:
        statement = sqlite_insert(Item)
        update_values = {
            column.name: getattr(statement.excluded, column.name)
            for column in Item.__table__.columns
            if column.name not in {"id", "game_mode"}
        }
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Item.id, Item.game_mode],
                set_=update_values,
            ),
            rows,
        )

    @staticmethod
    def _upsert_traders(session: Session, rows: list[dict[str, Any]]) -> None:
        statement = sqlite_insert(Trader)
        update_values = {
            column.name: getattr(statement.excluded, column.name)
            for column in Trader.__table__.columns
            if column.name not in {"id", "game_mode"}
        }
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Trader.id, Trader.game_mode],
                set_=update_values,
            ),
            rows,
        )

    @staticmethod
    def _upsert_item_translations(
        session: Session,
        rows: list[dict[str, Any]],
    ) -> None:
        statement = sqlite_insert(ItemTranslation)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ItemTranslation.item_id,
                    ItemTranslation.game_mode,
                    ItemTranslation.language,
                ],
                set_={
                    "name": statement.excluded.name,
                    "short_name": statement.excluded.short_name,
                    "synced_at": statement.excluded.synced_at,
                },
            ),
            rows,
        )

    @staticmethod
    def _item_translation_rows(
        bundle: SourceBundle,
        game_mode: str,
        default_language: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        translations = bundle.item_translations
        if not translations:
            translations = {
                str(item.get("id")): {
                    default_language: {
                        "name": str(item.get("name") or item.get("id") or ""),
                        "short_name": str(
                            item.get("shortName")
                            or item.get("name")
                            or item.get("id")
                            or ""
                        ),
                    }
                }
                for item in bundle.items
                if item.get("id")
            }

        rows: list[dict[str, Any]] = []
        for item_id, localized_values in translations.items():
            for language, values in localized_values.items():
                name = str(values.get("name") or item_id)
                rows.append(
                    {
                        "item_id": item_id,
                        "game_mode": game_mode,
                        "language": language,
                        "name": name,
                        "short_name": str(values.get("short_name") or name),
                        "synced_at": now,
                    }
                )
        return rows

    @staticmethod
    def _trader_rows(
        traders: list[dict[str, Any]],
        game_mode: str,
        language: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for trader in traders:
            trader_id = str(trader.get("id") or "").strip()
            if not trader_id:
                continue
            rows.append(
                {
                    "id": trader_id,
                    "game_mode": game_mode,
                    "language": language,
                    "name": str(trader.get("name") or trader_id),
                    "normalized_name": str(trader.get("normalizedName") or trader_id),
                    "image_url": trader.get("imageLink"),
                    "synced_at": now,
                }
            )
        return rows

    @classmethod
    def _item_row(
        cls,
        item: dict[str, Any],
        game_mode: str,
        language: str,
        now: datetime,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise ValueError("Upstream item has no id")

        types = [str(value) for value in (item.get("types") or [])]
        categories = [str(value) for value in (item.get("categories") or [])]
        handbook_categories = [
            str(value) for value in (item.get("handbookCategories") or [])
        ]
        name = str(item.get("name") or item_id)
        short_name = str(item.get("shortName") or name)
        normalized_name = str(item.get("normalizedName") or item_id)

        offers: list[dict[str, Any]] = []
        buy_offers = item.get("buyFromTrader") or []
        sell_offers = item.get("sellToTrader") or []
        offers.extend(cls._offer_rows(item_id, game_mode, "buy", buy_offers))
        offers.extend(cls._offer_rows(item_id, game_mode, "sell", sell_offers))

        valid_sell_offers = [
            offer for offer in offers if offer["kind"] == "sell" and offer["price_rub"] >= 0
        ]
        best_sell = max(valid_sell_offers, key=lambda offer: offer["price_rub"], default=None)

        fingerprint_payload = {
            "lastLowPrice": item.get("lastLowPrice"),
            "avg24hPrice": item.get("avg24hPrice"),
            "low24hPrice": item.get("low24hPrice"),
            "high24hPrice": item.get("high24hPrice"),
            "lastOfferCount": item.get("lastOfferCount"),
            "offers": sorted(
                (
                    offer["kind"],
                    offer["trader_id"],
                    offer["price"],
                    offer["price_rub"],
                    offer["currency"],
                )
                for offer in offers
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        row = {
            "id": item_id,
            "game_mode": game_mode,
            "language": language,
            "name": name,
            "short_name": short_name,
            "normalized_name": normalized_name,
            "description": item.get("description"),
            "search_text": f"{name} {short_name} {normalized_name} {item_id}".casefold(),
            "types": types,
            "types_search": "|" + "|".join(types) + "|",
            "categories": categories,
            "handbook_categories": handbook_categories,
            "properties": item.get("properties"),
            "width": optional_int(item.get("width")),
            "height": optional_int(item.get("height")),
            "weight": optional_float(item.get("weight")),
            "min_level_for_flea": optional_int(item.get("minLevelForFlea")),
            "base_price": optional_int(item.get("basePrice")),
            "last_low_price": optional_int(item.get("lastLowPrice")),
            "avg_24h_price": optional_int(item.get("avg24hPrice")),
            "low_24h_price": optional_int(item.get("low24hPrice")),
            "high_24h_price": optional_int(item.get("high24hPrice")),
            "change_48h": optional_float(item.get("changeLast48h")),
            "change_48h_percent": optional_float(item.get("changeLast48hPercent")),
            "last_offer_count": optional_int(item.get("lastOfferCount")),
            "best_trader_sell_price": best_sell["price_rub"] if best_sell else None,
            "best_trader_sell_id": best_sell["trader_id"] if best_sell else None,
            "price_fingerprint": fingerprint,
            "icon_url": item.get("iconLink"),
            "grid_image_url": item.get("gridImageLink"),
            "inspect_image_url": item.get("inspectImageLink"),
            "wiki_url": item.get("wikiLink"),
            "tarkov_dev_url": item.get("link"),
            "upstream_updated_at": parse_datetime(item.get("updated")),
            "last_scan_at": parse_datetime(item.get("lastScan")),
            "synced_at": now,
            "active": True,
            "raw_data": item,
        }
        return row, offers

    @staticmethod
    def _offer_rows(
        item_id: str,
        game_mode: str,
        kind: str,
        values: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        rows: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            trader_id = str(value.get("trader") or "").strip()
            price = optional_int(value.get("price"))
            price_rub = optional_int(value.get("priceRUB"))
            currency = str(value.get("currency") or "RUB")
            if not trader_id or price is None or price_rub is None:
                continue
            rows.append(
                {
                    "item_id": item_id,
                    "game_mode": game_mode,
                    "kind": kind,
                    "trader_id": trader_id,
                    "price": price,
                    "price_rub": price_rub,
                    "currency": currency,
                    "min_trader_level": optional_int(value.get("minTraderLevel")),
                    "buy_limit": optional_int(value.get("buyLimit")),
                }
            )
        return rows

    @staticmethod
    def _deduplicate_offers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["item_id"],
                row["game_mode"],
                row["kind"],
                row["trader_id"],
                row["currency"],
            )
            deduplicated[key] = row
        return list(deduplicated.values())
