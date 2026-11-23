async def upgrade(conn):
    await conn.execute("""CREATE TABLE ui_refresh_jobs (
        job_kind TEXT NOT NULL, entity_id TEXT NOT NULL, next_run REAL,
        attempts INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
        needs_check INTEGER NOT NULL DEFAULT 0, last_check_at REAL, outcome TEXT,
        PRIMARY KEY(job_kind, entity_id)
    )""")
    await conn.execute("CREATE INDEX idx_ui_refresh_due ON ui_refresh_jobs(next_run)")
    await conn.execute(
        """CREATE UNIQUE INDEX idx_order_widget_target
        ON bot_message_views(chat_id, message_id) WHERE renderer='widget' AND invalid=0"""
    )

    for table, kind in (("ad_orders", "ad_widget"), ("slot_ad_orders", "slot_widget")):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN pay_amount TEXT")
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN pay_address TEXT")
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN pay_type TEXT")
        await conn.execute(f"""CREATE TRIGGER refresh_{kind}_on_payment
            AFTER UPDATE OF paid_at ON {table}
            WHEN NEW.paid_at IS NOT NULL AND NEW.paid_at IS NOT OLD.paid_at
            BEGIN
                UPDATE ui_refresh_jobs SET revision=revision+1, next_run=unixepoch(),
                    attempts=0, needs_check=0 WHERE job_kind='{kind}' AND entity_id=NEW.out_trade_no;
            END
        """)
