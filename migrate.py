"""Apply numbered SQL migrations separately from Cora's application startup."""

from hashlib import sha256
import os
from pathlib import Path
import re
import sys

from dotenv import dotenv_values
import psycopg


ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "migrations"


def database_settings() -> dict:
    """Read database-only settings; environment variables take precedence."""
    values = {**dotenv_values(ROOT / ".env.database"), **os.environ}
    required = ("CORA_DB_NAME", "CORA_DB_USER", "CORA_DB_PASSWORD")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError("Missing database settings: " + ", ".join(missing))
    return {
        "host": values.get("CORA_DB_HOST") or "127.0.0.1",
        "port": values.get("CORA_DB_PORT") or "5432",
        "dbname": values["CORA_DB_NAME"],
        "user": values["CORA_DB_USER"],
        "password": values["CORA_DB_PASSWORD"],
        "connect_timeout": 5,
    }


def read_migrations(directory: Path) -> list[tuple[str, str, str]]:
    """Read ordered SQL files and checksum their newline-normalised contents."""
    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise ValueError(f"No SQL migrations found in {directory}")
    numbers = set()
    migrations = []
    for path in paths:
        if not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", path.name):
            raise ValueError(f"Invalid migration filename: {path.name}")
        number = path.name[:4]
        if number in numbers:
            raise ValueError(f"Duplicate migration number: {number}")
        numbers.add(number)
        script = path.read_text(encoding="utf-8-sig")
        migrations.append((path.name, script, sha256(script.encode("utf-8")).hexdigest()))
    return migrations


def apply_migrations(connection: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Commit pending SQL and its history together, or roll back the whole run."""
    migrations = read_migrations(directory)
    applied = []
    with connection.transaction():
        # Serialize migration runners; the lock is released at commit/rollback.
        connection.execute("SELECT pg_advisory_xact_lock(1129271873, 1)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                name TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        history = connection.execute(
            "SELECT name, checksum FROM public.schema_migrations ORDER BY name"
        ).fetchall()
        if [row[0] for row in history] != [entry[0] for entry in migrations[:len(history)]]:
            raise ValueError("Migration history differs from files; do not remove, rename, or reorder applied migrations")
        for (_, saved_checksum), (name, _, checksum) in zip(history, migrations):
            if saved_checksum != checksum:
                raise ValueError(f"Applied migration was changed: {name}; add a new migration instead")
        for name, script, checksum in migrations[len(history):]:
            connection.execute(script, prepare=False)
            connection.execute(
                "INSERT INTO public.schema_migrations (name, checksum) VALUES (%s, %s)",
                (name, checksum),
            )
            applied.append(name)
    return applied


def main() -> None:
    try:
        with psycopg.connect(**database_settings(), autocommit=True) as connection:
            applied = apply_migrations(connection)
    except (OSError, ValueError, psycopg.Error) as error:
        sys.exit(f"Migration failed: {error}")
    for name in applied:
        print(f"Applied {name}")
    if not applied:
        print("Database is up to date; no migrations applied.")


if __name__ == "__main__":
    main()