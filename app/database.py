from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base


def _ensure_sqlite_directory(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url[len(prefix) :]
    if not path_text or path_text == ":memory:":
        return
    Path(path_text).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


class Database:
    def __init__(self, database_url: str) -> None:
        _ensure_sqlite_directory(database_url)

        connect_args: dict[str, object] = {}
        engine_kwargs: dict[str, object] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        if database_url.endswith(":memory:"):
            engine_kwargs["poolclass"] = StaticPool

        self.engine: Engine = create_engine(
            database_url,
            connect_args=connect_args,
            **engine_kwargs,
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
