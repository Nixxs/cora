"""Standalone SQLAlchemy connection check; not part of Cora's startup yet."""

import sys

from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from migrate import database_settings


def build_engine() -> Engine:
    """Reuse database settings and the existing Psycopg driver without connecting."""
    settings = database_settings()
    # URL.create preserves special characters in credentials without hand escaping.
    url = URL.create(
        "postgresql+psycopg",
        username=settings["user"],
        password=settings["password"],
        host=settings["host"],
        port=int(settings["port"]),
        database=settings["dbname"],
    )
    return create_engine(
        url,
        connect_args={"connect_timeout": settings["connect_timeout"]},
        pool_pre_ping=True,
    )


def main() -> None:
    try:
        engine = build_engine()
        try:
            with engine.connect() as connection:
                database, user = connection.execute(
                    text("SELECT current_database(), current_user")
                ).one()
            print(f"SQLAlchemy connected: database={database}, user={user}")
        finally:
            engine.dispose()
    except (OSError, ValueError, SQLAlchemyError) as error:
        sys.exit(f"Database connection failed: {error}")


if __name__ == "__main__":
    main()