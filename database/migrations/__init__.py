"""Numbered, additive schema migrations. Run inside the startup transaction."""

import importlib
import time


async def migrate(conn):
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    for version, name in [
        (1, "database.migrations.telegram_ui_operations"),
        (2, "database.migrations.rich_message_sources"),
        (3, "database.migrations.order_widgets"),
    ]:
        row = await (
            await conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            )
        ).fetchone()
        if not row:
            await importlib.import_module(name).upgrade(conn)
            await conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?)", (version, time.time())
            )
