from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from .settings import settings


def _resolve_sqlite_path(url: str) -> Path | None:
    try:
        parsed = make_url(url)
    except Exception:
        return None

    if parsed.get_backend_name() != "sqlite":
        return None

    database = parsed.database or ""
    path = Path(database)
    if not path.is_absolute():
        base_dir = Path(__file__).resolve().parents[1]
        path = (base_dir / path).resolve()
    return path


_sqlite_path = _resolve_sqlite_path(settings.database_url)
_connect_args: dict[str, object] = {"check_same_thread": False} if _sqlite_path else {}

if _sqlite_path:
    _sqlite_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    echo=settings.database_echo,
    connect_args=_connect_args,
)


def _ensure_sqlite_profile_columns() -> None:
    if not _sqlite_path:
        return

    required_columns = {
        "target_weight_kg": "ALTER TABLE profiles ADD COLUMN target_weight_kg FLOAT",
        "target_date": "ALTER TABLE profiles ADD COLUMN target_date DATE",
    }

    with engine.begin() as connection:
        rows = connection.execute(text("PRAGMA table_info(profiles)"))
        existing_columns = {
            str(row._mapping.get("name") or row[1])
            for row in rows
        }
        for column_name, ddl in required_columns.items():
            if column_name not in existing_columns:
                try:
                    connection.execute(text(ddl))
                except OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_sqlite_profile_columns()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
