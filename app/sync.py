import asyncio
import logging

from app.repository import Repository, SyncStats
from app.source import TarkovDataSource


logger = logging.getLogger(__name__)


class SyncAlreadyRunning(RuntimeError):
    pass


class SyncService:
    def __init__(
        self,
        *,
        source: TarkovDataSource,
        repository: Repository,
        source_url: str,
        game_mode: str,
        language: str,
        interval_seconds: int,
        sync_on_startup: bool,
    ) -> None:
        self.source = source
        self.repository = repository
        self.source_url = source_url
        self.game_mode = game_mode
        self.language = language
        self.interval_seconds = interval_seconds
        self.sync_on_startup = sync_on_startup
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def sync_now(self, trigger: str = "manual") -> SyncStats:
        if self._lock.locked():
            raise SyncAlreadyRunning("Synchronization is already running")

        await self._lock.acquire()
        run_id = self.repository.start_sync_run(
            trigger=trigger,
            source_url=self.source_url,
            game_mode=self.game_mode,
            language=self.language,
        )
        try:
            bundle = await self.source.fetch()
            stats = self.repository.apply_bundle(
                run_id=run_id,
                bundle=bundle,
                game_mode=self.game_mode,
                language=self.language,
            )
            logger.info(
                "Tarkov sync %s completed: %s items, %s snapshots",
                run_id,
                stats.items_upserted,
                stats.snapshots_created,
            )
            return stats
        except asyncio.CancelledError:
            self.repository.fail_sync_run(run_id, "Synchronization was cancelled")
            raise
        except Exception as exc:
            self.repository.fail_sync_run(run_id, f"{type(exc).__name__}: {exc}")
            logger.exception("Tarkov sync %s failed", run_id)
            raise
        finally:
            self._lock.release()

    def start(self) -> None:
        if self._task is None:
            self._stop_event.clear()
            self._task = asyncio.create_task(
                self._background_loop(),
                name="tarkov-data-sync",
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _background_loop(self) -> None:
        if self.sync_on_startup:
            await self._safe_periodic_sync("startup")

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                await self._safe_periodic_sync("periodic")

    async def _safe_periodic_sync(self, trigger: str) -> None:
        try:
            await self.sync_now(trigger)
        except SyncAlreadyRunning:
            logger.info("Skipping %s sync because another sync is running", trigger)
        except Exception:
            # The run is already persisted as failed; the scheduler remains alive.
            pass
