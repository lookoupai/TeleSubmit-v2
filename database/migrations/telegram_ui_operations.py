"""P0 operations, immutable publication snapshots and presentation references."""


async def add_column(conn, table, name, definition):
    fields = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
    if name not in {row[1] for row in fields}:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


async def upgrade(conn):
    await add_column(conn, "submissions", "session_id", "TEXT")
    await add_column(conn, "submissions", "revision", "INTEGER NOT NULL DEFAULT 1")
    await conn.execute(
        "UPDATE submissions SET session_id=lower(hex(randomblob(8))) WHERE session_id IS NULL"
    )
    await conn.execute(
        """CREATE TRIGGER IF NOT EXISTS submissions_session AFTER INSERT ON submissions
        WHEN NEW.session_id IS NULL BEGIN
        UPDATE submissions SET session_id=lower(hex(randomblob(8))) WHERE user_id=NEW.user_id; END"""
    )
    await conn.execute(
        """CREATE TRIGGER IF NOT EXISTS submissions_revision
        AFTER UPDATE OF mode,image_id,document_id,tags,link,title,note,spoiler,text_content ON submissions
        BEGIN UPDATE submissions SET revision=OLD.revision+1 WHERE user_id=NEW.user_id; END"""
    )
    await add_column(conn, "ad_credit_ledger", "idempotency_key", "TEXT")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ad_ledger_operation ON ad_credit_ledger(idempotency_key)"
    )
    await conn.execute("""CREATE TABLE bot_operations(
        id INTEGER PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, owner_user_id INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('processing','sending','succeeded','failed','unknown','partial','review_pending','rejected')),
        payload_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', error_code TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    await conn.execute("""CREATE TABLE publish_parts(
        id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL REFERENCES bot_operations(id),
        part_key TEXT NOT NULL, state TEXT NOT NULL, message_ids_json TEXT NOT NULL DEFAULT '[]',
        error_code TEXT, updated_at REAL NOT NULL, UNIQUE(operation_id,part_key))""")
    await conn.execute(
        """CREATE TABLE bot_message_views(
        id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL, owner_user_id INTEGER,
        chat_id TEXT, message_id INTEGER, inline_message_id TEXT,
        renderer TEXT NOT NULL DEFAULT 'plain', template_version INTEGER NOT NULL DEFAULT 1,
        rendered_revision INTEGER NOT NULL DEFAULT 0, invalid INTEGER NOT NULL DEFAULT 0,
        CHECK((chat_id IS NOT NULL AND message_id IS NOT NULL AND inline_message_id IS NULL)
          OR (chat_id IS NULL AND message_id IS NULL AND inline_message_id IS NOT NULL)))"""
    )
    await conn.execute(
        "CREATE UNIQUE INDEX idx_message_view_normal ON bot_message_views(entity_kind,entity_id,chat_id,message_id)"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX idx_message_view_inline ON bot_message_views(entity_kind,entity_id,inline_message_id)"
    )
