"""Forward-only Mongo migrations for the gecko_trade_agent + gecko_events DBs.

Each file is dated ``YYYY-MM-DD-<sprint>-<slug>.py`` and exposes an
``async def apply(client) -> dict``. Migrations are idempotent —
re-running a migration is a no-op. There is no rollback path; per
project convention (Pattern A § soft-delete by default), schema changes
ship forward.

Run order: filename lexicographic. Each migration logs ``created`` /
``existed`` counts so smoke output is greppable.
"""
