import logging
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.cache import CacheBackend, RedisResponseCache
from app.cache_middleware import RedisCacheMiddleware
from app.config import Settings, get_settings
from app.database import Database
from app.models import Item, PriceOffer, PriceSnapshot, SyncRun, Trader
from app.repository import Repository
from app.schemas import (
    BestTraderPrice,
    ExportMeta,
    ExportResponse,
    FleaPrice,
    HealthResponse,
    ItemDetail,
    ItemListResponse,
    ItemSummary,
    Pagination,
    PriceHistoryResponse,
    PriceOfferOut,
    PriceSnapshotOut,
    PriceSummary,
    SyncResponse,
    SyncRunOut,
    TraderOut,
)
from app.source import TarkovDataSource
from app.sync import SyncAlreadyRunning, SyncService


logger = logging.getLogger(__name__)


def _trader_names(session: Session, game_mode: str) -> dict[str, str]:
    return dict(
        session.execute(
            select(Trader.id, Trader.name).where(Trader.game_mode == game_mode)
        ).all()
    )


def _item_summary(item: Item, trader_names: dict[str, str]) -> ItemSummary:
    best_trader = None
    if item.best_trader_sell_id and item.best_trader_sell_price is not None:
        best_trader = BestTraderPrice(
            trader_id=item.best_trader_sell_id,
            trader_name=trader_names.get(item.best_trader_sell_id),
            price_rub=item.best_trader_sell_price,
        )

    return ItemSummary(
        id=item.id,
        name=item.name,
        short_name=item.short_name,
        normalized_name=item.normalized_name,
        types=item.types,
        icon_url=item.icon_url,
        grid_image_url=item.grid_image_url,
        prices=PriceSummary(
            base=item.base_price,
            flea=FleaPrice(
                last_low=item.last_low_price,
                average_24h=item.avg_24h_price,
                low_24h=item.low_24h_price,
                high_24h=item.high_24h_price,
                change_48h=item.change_48h,
                change_48h_percent=item.change_48h_percent,
                offer_count=item.last_offer_count,
                scanned_at=item.last_scan_at,
            ),
            best_trader_sell=best_trader,
        ),
        updated_at=item.upstream_updated_at,
    )


def _offer_out(
    offer: PriceOffer,
    trader_names: dict[str, str],
) -> PriceOfferOut:
    return PriceOfferOut(
        kind=offer.kind,  # type: ignore[arg-type]
        trader_id=offer.trader_id,
        trader_name=trader_names.get(offer.trader_id),
        price=offer.price,
        price_rub=offer.price_rub,
        currency=offer.currency,
        min_trader_level=offer.min_trader_level,
        buy_limit=offer.buy_limit,
    )


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    source: TarkovDataSource | None = None,
    cache: CacheBackend | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.database_url)
    repository = Repository(database)
    source = source or TarkovDataSource(
        base_url=settings.source_base_url,
        game_mode=settings.tarkov_game_mode,
        language=settings.tarkov_language,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )

    response_cache = cache
    if response_cache is None and settings.redis_cache_enabled:
        response_cache = RedisResponseCache(
            url=settings.redis_url,
            namespace=(
                f"{settings.redis_cache_prefix}:"
                f"{settings.tarkov_game_mode}:{settings.tarkov_language}"
            ),
            ttl_seconds=settings.redis_cache_ttl_seconds,
            max_response_bytes=settings.redis_cache_max_response_bytes,
            socket_timeout_seconds=settings.redis_socket_timeout_seconds,
        )

    async def invalidate_response_cache() -> None:
        if response_cache is None:
            return
        deleted = await response_cache.clear()
        logger.info("Invalidated %s cached API responses", deleted)

    sync_service = SyncService(
        source=source,
        repository=repository,
        source_url=(
            f"{settings.source_base_url}/{settings.tarkov_game_mode}/items"
        ),
        game_mode=settings.tarkov_game_mode,
        language=settings.tarkov_language,
        interval_seconds=settings.sync_interval_seconds,
        sync_on_startup=settings.sync_on_startup,
        on_success=invalidate_response_cache,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.create_schema()
        if response_cache is not None:
            await response_cache.ping()
        sync_service.start()
        try:
            yield
        finally:
            await sync_service.stop()
            if response_cache is not None:
                await response_cache.close()
            database.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Локальное нормализованное хранилище предметов и цен tarkov.dev. "
            "Swagger: /docs"
        ),
        lifespan=lifespan,
    )
    if response_cache is not None:
        app.add_middleware(
            RedisCacheMiddleware,
            cache=response_cache,
            path_prefixes=(
                "/api/v1/items",
                "/api/v1/traders",
                "/api/v1/export/items",
            ),
        )
    app.state.settings = settings
    app.state.database = database
    app.state.repository = repository
    app.state.sync_service = sync_service
    app.state.response_cache = response_cache

    def get_session() -> Iterator[Session]:
        with database.session() as session:
            yield session

    def require_admin_key(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        expected = settings.admin_api_key
        if expected and (
            not x_api_key or not secrets.compare_digest(x_api_key, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-API-Key",
            )

    @app.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    def health(
        session: Annotated[Session, Depends(get_session)],
    ) -> HealthResponse:
        item_count = repository.active_item_count(
            session,
            settings.tarkov_game_mode,
        )
        latest = repository.latest_sync(session)
        if latest is None or latest.status == "running":
            health_status = "starting"
        elif latest.status == "success" and item_count > 0:
            health_status = "ok"
        else:
            health_status = "degraded"

        return HealthResponse(
            status=health_status,  # type: ignore[arg-type]
            database="ok",
            cache=(response_cache.status if response_cache else "disabled"),
            active_items=item_count,
            sync_running=sync_service.running,
            last_sync=SyncRunOut.model_validate(latest) if latest else None,
        )

    @app.get("/ready", response_model=HealthResponse, tags=["meta"])
    def ready(
        session: Annotated[Session, Depends(get_session)],
    ) -> HealthResponse:
        response = health(session)
        if response.status != "ok":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Initial successful synchronization has not completed",
            )
        return response

    @app.get(
        "/api/v1/items",
        response_model=ItemListResponse,
        tags=["items"],
    )
    def list_items(
        session: Annotated[Session, Depends(get_session)],
        q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        item_type: Annotated[
            str | None,
            Query(alias="type", min_length=1, max_length=100),
        ] = None,
        min_price: Annotated[int | None, Query(ge=0)] = None,
        max_price: Annotated[int | None, Query(ge=0)] = None,
        sort_by: Literal["name", "price", "updated"] = "name",
        order: Literal["asc", "desc"] = "asc",
        limit: Annotated[int, Query(ge=1, le=settings.max_page_size)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ItemListResponse:
        conditions = [
            Item.game_mode == settings.tarkov_game_mode,
            Item.active.is_(True),
        ]
        if q:
            conditions.append(Item.search_text.contains(q.casefold(), autoescape=True))
        if item_type:
            conditions.append(
                Item.types_search.contains(f"|{item_type}|", autoescape=True)
            )

        effective_price = func.coalesce(
            Item.last_low_price,
            Item.avg_24h_price,
            Item.base_price,
        )
        if min_price is not None:
            conditions.append(effective_price >= min_price)
        if max_price is not None:
            conditions.append(effective_price <= max_price)

        total = int(
            session.scalar(select(func.count()).select_from(Item).where(*conditions))
            or 0
        )
        sort_column = {
            "name": Item.normalized_name,
            "price": effective_price,
            "updated": Item.upstream_updated_at,
        }[sort_by]
        ordering = sort_column.desc() if order == "desc" else sort_column.asc()
        statement: Select[tuple[Item]] = (
            select(Item)
            .where(*conditions)
            .order_by(ordering.nulls_last(), Item.id.asc())
            .limit(limit)
            .offset(offset)
        )
        items = list(session.scalars(statement))
        names = _trader_names(session, settings.tarkov_game_mode)
        return ItemListResponse(
            meta=Pagination(total=total, limit=limit, offset=offset),
            items=[_item_summary(item, names) for item in items],
        )

    @app.get(
        "/api/v1/items/{item_id}",
        response_model=ItemDetail,
        tags=["items"],
    )
    def get_item(
        item_id: str,
        session: Annotated[Session, Depends(get_session)],
        include_raw: bool = False,
    ) -> ItemDetail:
        item = session.scalar(
            select(Item).where(
                Item.id == item_id,
                Item.game_mode == settings.tarkov_game_mode,
                Item.active.is_(True),
            )
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")

        names = _trader_names(session, settings.tarkov_game_mode)
        offers = list(
            session.scalars(
                select(PriceOffer)
                .where(
                    PriceOffer.item_id == item_id,
                    PriceOffer.game_mode == settings.tarkov_game_mode,
                )
                .order_by(PriceOffer.kind, PriceOffer.price_rub)
            )
        )
        summary = _item_summary(item, names)
        return ItemDetail(
            **summary.model_dump(),
            description=item.description,
            width=item.width,
            height=item.height,
            weight=item.weight,
            min_level_for_flea=item.min_level_for_flea,
            categories=item.categories,
            handbook_categories=item.handbook_categories,
            properties=item.properties,
            inspect_image_url=item.inspect_image_url,
            wiki_url=item.wiki_url,
            tarkov_dev_url=item.tarkov_dev_url,
            buy_offers=[
                _offer_out(offer, names) for offer in offers if offer.kind == "buy"
            ],
            sell_offers=sorted(
                (
                    _offer_out(offer, names)
                    for offer in offers
                    if offer.kind == "sell"
                ),
                key=lambda offer: offer.price_rub,
                reverse=True,
            ),
            last_scan_at=item.last_scan_at,
            synced_at=item.synced_at,
            raw_data=item.raw_data if include_raw else None,
        )

    @app.get(
        "/api/v1/items/{item_id}/history",
        response_model=PriceHistoryResponse,
        tags=["items"],
    )
    def get_item_history(
        item_id: str,
        session: Annotated[Session, Depends(get_session)],
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    ) -> PriceHistoryResponse:
        exists = session.scalar(
            select(Item.id).where(
                Item.id == item_id,
                Item.game_mode == settings.tarkov_game_mode,
            )
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="Item not found")

        newest_first = list(
            session.scalars(
                select(PriceSnapshot)
                .where(
                    PriceSnapshot.item_id == item_id,
                    PriceSnapshot.game_mode == settings.tarkov_game_mode,
                )
                .order_by(PriceSnapshot.observed_at.desc())
                .limit(limit)
            )
        )
        return PriceHistoryResponse(
            item_id=item_id,
            points=[
                PriceSnapshotOut.model_validate(point)
                for point in reversed(newest_first)
            ],
        )

    @app.get(
        "/api/v1/traders",
        response_model=list[TraderOut],
        tags=["traders"],
    )
    def list_traders(
        session: Annotated[Session, Depends(get_session)],
    ) -> list[TraderOut]:
        traders = session.scalars(
            select(Trader)
            .where(Trader.game_mode == settings.tarkov_game_mode)
            .order_by(Trader.name)
        )
        return [TraderOut.model_validate(trader) for trader in traders]

    @app.get(
        "/api/v1/sync",
        response_model=SyncResponse,
        tags=["synchronization"],
    )
    def sync_status(
        session: Annotated[Session, Depends(get_session)],
    ) -> SyncResponse:
        latest = repository.latest_sync(session)
        if latest is None:
            raise HTTPException(status_code=404, detail="No synchronization runs yet")
        return SyncResponse(run=SyncRunOut.model_validate(latest))

    @app.post(
        "/api/v1/sync",
        response_model=SyncResponse,
        dependencies=[Depends(require_admin_key)],
        tags=["synchronization"],
    )
    async def run_sync(
        session: Annotated[Session, Depends(get_session)],
    ) -> SyncResponse:
        try:
            stats = await sync_service.sync_now("manual")
        except SyncAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream synchronization failed: {exc}",
            ) from exc

        run = session.get(SyncRun, stats.run_id)
        if run is None:
            raise HTTPException(status_code=500, detail="Sync result was not persisted")
        session.refresh(run)
        return SyncResponse(run=SyncRunOut.model_validate(run))

    @app.get(
        "/api/v1/export/items",
        response_model=ExportResponse,
        tags=["export"],
    )
    def export_items(
        session: Annotated[Session, Depends(get_session)],
        output_format: Annotated[
            Literal["map", "list"],
            Query(alias="format"),
        ] = "map",
    ) -> ExportResponse:
        items = list(
            session.scalars(
                select(Item)
                .where(
                    Item.game_mode == settings.tarkov_game_mode,
                    Item.active.is_(True),
                )
                .order_by(Item.id)
            )
        )
        names = _trader_names(session, settings.tarkov_game_mode)
        summaries = [_item_summary(item, names) for item in items]
        latest_success = repository.latest_successful_sync(session)
        export_data: dict[str, ItemSummary] | list[ItemSummary]
        if output_format == "map":
            export_data = {item.id: item for item in summaries}
        else:
            export_data = summaries

        return ExportResponse(
            meta=ExportMeta(
                generated_at=datetime.now(UTC),
                game_mode=settings.tarkov_game_mode,
                language=settings.tarkov_language,
                count=len(summaries),
                source_synced_at=(
                    latest_success.finished_at if latest_success else None
                ),
            ),
            items=export_data,
        )

    return app


app = create_app()
