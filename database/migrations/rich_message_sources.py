async def upgrade(conn):
    await conn.execute(
        "ALTER TABLE published_posts ADD COLUMN source_content_json TEXT"
    )
    await conn.execute("ALTER TABLE published_posts ADD COLUMN renderer TEXT")
    await conn.execute(
        "ALTER TABLE published_posts ADD COLUMN template_version INTEGER"
    )
    await conn.execute("ALTER TABLE publish_parts ADD COLUMN renderer TEXT")
