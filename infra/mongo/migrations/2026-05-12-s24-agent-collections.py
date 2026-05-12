"""S24 DE-4 — Mongo bootstrap for the trade-agent runtime + global events.

Creates / verifies four collections used by the trade-agent runtime
(WS-B) and the global observability ledger (WS-D):

  agent_state              — one document per running agent. Persistent.
  agent_journal            — append-only per-agent event log. TTL 90d.
  agent_hotpath_snapshot   — last-known Helius/Pyth observation cache. TTL 5m.
  events                   — global structured events (verdict.served,
                             cache.hit, oracle.cost_usd, x402.settle, …).
                             TTL 180d. Lives in a separate DB
                             (``gecko_events``) so the agent DB can scale
                             independently.

Idempotency
-----------
* ``create_collection`` is guarded by an existence check; if a race
  beats us, ``CollectionInvalid`` is swallowed.
* ``create_index`` is naturally idempotent on identical specs.
* ``collMod`` re-applies the validator every run (cheap); validator
  level is ``moderate`` so existing documents aren't rejected.

Field set (DE-4 hand-off to software-engineer for SE-1)
-------------------------------------------------------
**agent_state** (Pattern A enums via ``gecko_core.types``):
    agent_id (str, unique index)
    user_wallet (str | None)
    spec_name (str)
    spec_version (str)
    spec_fingerprint (str)
    status (AgentStatus literal — starting/running/paused/stopped/halted/resuming)
    mode (AgentMode literal — advisor/trader; v0.1 is advisor-only)
    oracle_cache_key (str | None)
    journal_count (int)
    created_at (datetime)
    last_tick_at (datetime | None)

**agent_journal**:
    agent_id (str, indexed)
    ts (datetime, indexed; TTL 90 days)
    event_type (AgentJournalEvent literal)
    payload (dict)

**agent_hotpath_snapshot**:
    agent_id (str)
    protocol (str)
    as_of (datetime, TTL 5 minutes)
    snapshot (dict)
    (compound index on agent_id + protocol for warm-start lookup)

**events** (gecko_events DB):
    ts (datetime, indexed; TTL 180 days)
    event (EventKind literal — verdict.served / cache.hit / …; indexed)
    wallet (str | None, indexed)
    payload (dict)

Index names are stable strings so smoke tools / doctor probes can assert
``index_information()`` includes them.

Exit codes
----------
0 — success.
1 — env missing (no MONGODB_URI / MONGO_URI).
2 — connection failure (server unreachable / auth failure).
3 — index or collection creation failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("mongo.migrate.s24_agent")

TRADE_AGENT_DB = "gecko_trade_agent"
EVENTS_DB = "gecko_events"

# --- Index specs ---------------------------------------------------------
# Each entry: (db_name, collection, keys, options).
INDEX_SPECS: list[tuple[str, str, list[tuple[str, int]], dict[str, Any]]] = [
    # agent_state
    (
        TRADE_AGENT_DB,
        "agent_state",
        [("agent_id", 1)],
        {"unique": True, "name": "agent_id_unique"},
    ),
    (
        TRADE_AGENT_DB,
        "agent_state",
        [("user_wallet", 1), ("status", 1)],
        {"name": "user_wallet_status"},
    ),
    (
        TRADE_AGENT_DB,
        "agent_state",
        [("status", 1), ("last_tick_at", -1)],
        {"name": "status_last_tick_desc"},
    ),
    # agent_journal — TTL 90 days on `ts`.
    (
        TRADE_AGENT_DB,
        "agent_journal",
        [("agent_id", 1), ("ts", -1)],
        {"name": "agent_id_ts_desc"},
    ),
    (
        TRADE_AGENT_DB,
        "agent_journal",
        [("event_type", 1), ("ts", -1)],
        {"name": "event_type_ts_desc"},
    ),
    (
        TRADE_AGENT_DB,
        "agent_journal",
        [("ts", 1)],
        {"name": "ts_ttl_90d", "expireAfterSeconds": 60 * 60 * 24 * 90},
    ),
    # agent_hotpath_snapshot — TTL 5 min on `as_of`.
    (
        TRADE_AGENT_DB,
        "agent_hotpath_snapshot",
        [("agent_id", 1), ("protocol", 1)],
        {"unique": True, "name": "agent_id_protocol_unique"},
    ),
    (
        TRADE_AGENT_DB,
        "agent_hotpath_snapshot",
        [("as_of", 1)],
        {"name": "as_of_ttl_5m", "expireAfterSeconds": 60 * 5},
    ),
    # events — TTL 180 days on `ts`.
    (
        EVENTS_DB,
        "events",
        [("ts", -1)],
        {"name": "ts_desc"},
    ),
    (
        EVENTS_DB,
        "events",
        [("event", 1), ("ts", -1)],
        {"name": "event_ts_desc"},
    ),
    (
        EVENTS_DB,
        "events",
        [("wallet", 1), ("ts", -1)],
        {"name": "wallet_ts_desc", "sparse": True},
    ),
    (
        EVENTS_DB,
        "events",
        [("ts", 1)],
        {"name": "ts_ttl_180d", "expireAfterSeconds": 60 * 60 * 24 * 180},
    ),
]

COLLECTIONS: tuple[tuple[str, str], ...] = (
    (TRADE_AGENT_DB, "agent_state"),
    (TRADE_AGENT_DB, "agent_journal"),
    (TRADE_AGENT_DB, "agent_hotpath_snapshot"),
    (EVENTS_DB, "events"),
)

# --- Schema validators ---------------------------------------------------
# ``validationLevel="moderate"`` — pre-existing docs aren't rejected on
# validator install. Required-fields list matches the Python Pattern A
# literals in ``gecko_core.types``; expand here when the literals expand.

VALIDATORS: dict[tuple[str, str], dict[str, Any]] = {
    (TRADE_AGENT_DB, "agent_state"): {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["agent_id", "spec_name", "spec_version", "mode", "status"],
            "properties": {
                "agent_id": {"bsonType": "string"},
                "user_wallet": {"bsonType": ["string", "null"]},
                "spec_name": {"bsonType": "string"},
                "spec_version": {"bsonType": "string"},
                "spec_fingerprint": {"bsonType": ["string", "null"]},
                "mode": {"enum": ["advisor", "trader"]},
                "status": {
                    "enum": [
                        "starting",
                        "running",
                        "paused",
                        "stopped",
                        "halted",
                        "resuming",
                    ]
                },
                "oracle_cache_key": {"bsonType": ["string", "null"]},
                "journal_count": {"bsonType": ["int", "long"]},
                "created_at": {"bsonType": "date"},
                "last_tick_at": {"bsonType": ["date", "null"]},
            },
        }
    },
    (TRADE_AGENT_DB, "agent_journal"): {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["agent_id", "ts", "event_type"],
            "properties": {
                "agent_id": {"bsonType": "string"},
                "ts": {"bsonType": "date"},
                "event_type": {"bsonType": "string"},
                "payload": {"bsonType": ["object", "null"]},
            },
        }
    },
    (TRADE_AGENT_DB, "agent_hotpath_snapshot"): {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["agent_id", "protocol", "as_of"],
            "properties": {
                "agent_id": {"bsonType": "string"},
                "protocol": {"bsonType": "string"},
                "as_of": {"bsonType": "date"},
                "snapshot": {"bsonType": ["object", "null"]},
            },
        }
    },
    (EVENTS_DB, "events"): {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["ts", "event"],
            "properties": {
                "ts": {"bsonType": "date"},
                "event": {"bsonType": "string"},
                "wallet": {"bsonType": ["string", "null"]},
                "payload": {"bsonType": ["object", "null"]},
            },
        }
    },
}


def _mongo_uri() -> str | None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri or uri == "__unset__":
        return None
    return uri


def _redact_uri(uri: str) -> str:
    if "@" not in uri:
        return uri
    scheme, _, rest = uri.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://<redacted>@{host}"


async def _ensure_collection(
    db: Any, name: str, dry_run: bool
) -> tuple[bool, bool]:
    existing = await db.list_collection_names()
    if name in existing:
        logger.info("collection.exists db=%s name=%s", db.name, name)
        return (False, True)
    if dry_run:
        logger.info("WOULD CREATE collection db=%s name=%s", db.name, name)
        return (True, False)
    try:
        await db.create_collection(name)
        logger.info("collection.created db=%s name=%s", db.name, name)
        return (True, False)
    except Exception as exc:
        from pymongo.errors import CollectionInvalid

        if isinstance(exc, CollectionInvalid):
            logger.info("collection.exists db=%s name=%s (race)", db.name, name)
            return (False, True)
        raise


async def _ensure_index(
    db: Any,
    collection: str,
    keys: list[tuple[str, int]],
    options: dict[str, Any],
    dry_run: bool,
) -> tuple[bool, bool]:
    coll = db[collection]
    name = options.get("name", "_".join(f"{k}_{v}" for k, v in keys))
    existed = False
    try:
        existing = await coll.index_information()
        if name in existing:
            existed = True
    except Exception:
        existing = {}

    if existed:
        logger.info(
            "index.exists db=%s collection=%s name=%s", db.name, collection, name
        )
        return (False, True)

    if dry_run:
        logger.info(
            "WOULD CREATE index db=%s collection=%s name=%s keys=%s",
            db.name,
            collection,
            name,
            keys,
        )
        return (True, False)

    await coll.create_index(keys, **options)
    logger.info(
        "index.created db=%s collection=%s name=%s", db.name, collection, name
    )
    return (True, False)


async def _apply_validator(
    db: Any, collection: str, dry_run: bool
) -> None:
    validator = VALIDATORS.get((db.name, collection))
    if validator is None:
        return
    if dry_run:
        logger.info(
            "WOULD APPLY validator db=%s collection=%s level=moderate",
            db.name,
            collection,
        )
        return
    try:
        await db.command(
            "collMod",
            collection,
            validator=validator,
            validationLevel="moderate",
        )
        logger.info(
            "validator.applied db=%s collection=%s level=moderate",
            db.name,
            collection,
        )
    except Exception as exc:
        logger.warning(
            "validator.skipped db=%s collection=%s reason=%s",
            db.name,
            collection,
            exc,
        )


async def apply(client: Any, *, dry_run: bool = False) -> dict[str, int]:
    """Entrypoint expected by the (future) migration runner. Idempotent."""
    coll_created = 0
    coll_existed = 0
    idx_created = 0
    idx_existed = 0

    for db_name, name in COLLECTIONS:
        created, existed = await _ensure_collection(client[db_name], name, dry_run)
        coll_created += int(created)
        coll_existed += int(existed)

    for db_name, collection, keys, options in INDEX_SPECS:
        created, existed = await _ensure_index(
            client[db_name], collection, keys, options, dry_run
        )
        idx_created += int(created)
        idx_existed += int(existed)

    for db_name, name in COLLECTIONS:
        await _apply_validator(client[db_name], name, dry_run)

    return {
        "collections_created": coll_created,
        "collections_existed": coll_existed,
        "indexes_created": idx_created,
        "indexes_existed": idx_existed,
    }


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "S24 DE-4 — bootstrap gecko_trade_agent + gecko_events Mongo "
            "collections + indexes. Idempotent."
        )
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    uri = _mongo_uri()
    if not uri:
        logger.error("env.missing var=MONGODB_URI or MONGO_URI")
        return 1
    logger.info(
        "migrate.start migration=2026-05-12-s24-agent-collections uri=%s dry_run=%s",
        _redact_uri(uri),
        args.dry_run,
    )

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        logger.error("import.failure motor not installed")
        return 2

    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10_000)
    try:
        await client.admin.command("ping")
    except Exception as exc:
        logger.error("connection.failure error=%s", exc)
        client.close()
        return 2

    try:
        stats = await apply(client, dry_run=args.dry_run)
    except Exception as exc:
        logger.error("migrate.failure error=%s", exc)
        client.close()
        return 3
    finally:
        client.close()

    logger.info(
        "migrate.done collections_created=%d collections_existed=%d "
        "indexes_created=%d indexes_existed=%d",
        stats["collections_created"],
        stats["collections_existed"],
        stats["indexes_created"],
        stats["indexes_existed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
