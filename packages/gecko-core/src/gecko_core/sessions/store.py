"""Session persistence layer.

`SessionStore` is the single seam between gecko-core business logic and the
Supabase `sessions` / `sources` tables. Async surface; the underlying
supabase-py client is sync, so calls are dispatched via asyncio.to_thread to
avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel
from supabase import Client

from gecko_core.db import create_supabase_client
from gecko_core.models import SessionStatus, SourceInfo, Tier
from gecko_core.sources.types import ProviderKind

logger = logging.getLogger(__name__)

SessionPhase = Literal["pre_product", "during_build", "ongoing"]
"""S13-PHASE-01 — lifecycle phase a session belongs to.

`pre_product` is the legacy default and what every existing row carries:
the user is researching whether to build a thing. `during_build` covers
sessions kicked off mid-build (S14 `gecko_pulse` is the first consumer).
`ongoing` is the post-launch operational tier S15 will plug into.

Mirrors the CHECK constraint in
`infra/supabase/migrations/20260501100000_session_phase.sql`. Drift between
this Literal and the SQL CHECK is caught at insert time — Postgres will
reject anything not in the trio."""

PaymentMode = Literal["stub", "live", "frames", "cdp"]
"""Session-row payment-mode literal.

Kept in sync with `gecko_core.payments.modes.PAYMENT_MODES`. We can't
re-export from there directly because `gecko_core.payments.__init__`
imports `gate`, which imports `SessionStore` — a circular import the
moment session-store-load triggers payments-package-init.

The drift is caught at runtime: `tests/test_payment_mode_consistency.py`
asserts `typing.get_args(PaymentMode)` matches `PAYMENT_MODES` exactly,
and a startup-time assertion below catches it before any test runs."""

CostKind = Literal[
    "llm",
    "embed",
    "tavily",
    "deepgram",
    "twitsh",
    "v1_sources",
    # S13-COMMO-01..03 — Track E commoditization SKUs. Each is its own
    # column on `sessions` so `bb economics` can render line items.
    "advisor",
    "ask",
    "classify",
]

ProEventType = Literal["turn_start", "turn_end", "final", "error"]


class ProEventRow(BaseModel):
    """One row from the `pro_events` SSE buffer table.

    `id` is the BIGSERIAL primary key the SSE handler tails on
    (`WHERE id > $last_id`). `seq` is producer-assigned per-session and
    enforces ordering invariants via the UNIQUE (session_id, seq) constraint.
    """

    id: int
    session_id: UUID
    seq: int
    event_type: ProEventType
    agent: str | None
    content: str
    tokens_in: int
    tokens_out: int
    ts: float
    created_at: datetime

    model_config = {"frozen": True}


class SessionEconomics(BaseModel):
    """Read model for the per-session unit economics view.

    `margin_usd = price_usd - sum(cost_*)`. On devnet `price_usd` may be 0
    or NULL — that's fine, the cost columns still get the real numbers.
    """

    session_id: UUID
    price_usd: float | None
    cost_llm_usd: float
    cost_embed_usd: float
    cost_tavily_usd: float
    cost_deepgram_usd: float
    cost_twitsh_usd: float = 0.0
    cost_v1_sources_usd: float = 0.0
    # S13-COMMO-01..03 — Track E line items. Default 0.0 so models loaded
    # from databases that haven't yet run the migration still validate.
    cost_advisor_usd: float = 0.0
    cost_ask_usd: float = 0.0
    cost_classify_usd: float = 0.0
    advisor_calls_count: int = 0
    ask_calls_count: int = 0
    cost_total_usd: float
    margin_usd: float | None
    x402_tx_signature: str | None

    model_config = {"frozen": True}


class ProjectWallet(BaseModel):
    """Read model for a project's Privy wallet + budget snapshot.

    Fields mirror the columns added in migration 013_project_wallets.sql.
    `privy_wallet_id` / `privy_wallet_address` are nullable until S2-05
    lazy-provisioning runs. `budget_cap_usd` / `spent_usd` are NUMERIC in
    Postgres; we surface them as Decimal so callers don't lose precision
    when comparing against the cap.
    """

    privy_wallet_id: str | None
    privy_wallet_address: str | None
    budget_cap_usd: Decimal
    spent_usd: Decimal

    model_config = {"frozen": True}


SettlementNetwork = Literal["solana-devnet", "solana-mainnet"]

Verdict = Literal["ship", "kill", "pivot"]

# S9-PRECEDENT-01 — outcome labels for the Gecko Flywheel. Distinct from
# `Verdict` (which records what the panel SAID about the idea at evaluation
# time): `PrecedentOutcome` records what HAPPENED to the idea afterwards.
# Sprint 9 ships schema only — every row backfills to 'unknown'; the auto-
# labeling job that populates 'shipped' / 'killed' lands in Sprint 10.
PrecedentOutcome = Literal["shipped", "killed", "unknown"]


class GeckoPrecedent(BaseModel):
    """One row from the internal Gecko Flywheel (`gecko_precedent`).

    Written once per Pro session; retrieved on future sessions by cosine
    similarity on `idea_summary`'s embedding. `idea_summary` is an
    LLM-generated 1-sentence category abstraction — never verbatim user
    text (CI guardrail S2X-05 enforces <=30% character overlap).
    `similarity` is populated only on retrieval queries.
    """

    id: UUID
    session_id: UUID | None
    user_id: UUID | None
    idea_summary: str
    verdict: Verdict
    # S9-PRECEDENT-01 — denormalized outcome label. Defaults to 'unknown'
    # so legacy precedents and offline test fixtures stay valid without a
    # backfill migration of their own.
    outcome: PrecedentOutcome = "unknown"
    key_comparables: list[Any]
    similarity: float | None = None

    model_config = {"frozen": True}


class ChunkMatch(BaseModel):
    """One row from the `match_chunks_windowed` RPC (S13-PHASE-02).

    Distinct from `match_chunks` results because windowed search surfaces
    `captured_at` so callers can reason about chunk freshness (S14 pulse
    rendering wants this to flag "stale evidence" in deltas).
    """

    id: UUID
    source_id: UUID
    source_url: str
    chunk_index: int
    text: str
    captured_at: datetime
    similarity: float
    # S17-WEDGE-DATA-01 — provider attribution surfaced through the RPC.
    # Defaults to 'web' so rows from databases that haven't yet run the
    # provider_kind migration still validate (matches the SQL DEFAULT).
    provider_kind: ProviderKind = "web"

    model_config = {"frozen": True}


class SessionRecord(BaseModel):
    """Internal projection of a `sessions` row.

    Distinct from the public `ResearchResult` — this carries persistence
    metadata (status, payment, timestamps) that callers outside gecko-core
    don't need to see.
    """

    id: UUID
    idea: str
    tier: Tier
    status: SessionStatus
    payment_intent_id: str | None = None
    payment_mode: PaymentMode = "stub"
    x402_tx_signature: str | None = None
    project_id: UUID | None = None
    paid_from_wallet_address: str | None = None
    # S13-PHASE-01 — lifecycle phase + parent linkage. Defaults match the
    # SQL column defaults so models loaded from databases that haven't yet
    # run the phase migration still validate.
    phase: SessionPhase = "pre_product"
    parent_session_id: UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None
    deleted_at: datetime | None = None

    model_config = {"frozen": True}


class SessionStore:
    """Async wrapper over the Supabase service-role client for session CRUD."""

    SESSIONS_TABLE = "sessions"
    SOURCES_TABLE = "sources"
    CHUNKS_TABLE = "chunks"
    CHUNK_EMBEDDING_CACHE_TABLE = "chunk_embedding_cache"
    CHUNKS_WRITE_AUDIT_TABLE = "chunks_write_audit"

    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> SessionStore:
        """Build a store using SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from env.

        Server-side only. Never call this from gecko-mcpay-app.
        """
        return cls(create_supabase_client())

    async def create(
        self,
        idea: str,
        tier: Tier,
        payment_mode: PaymentMode = "stub",
        *,
        phase: SessionPhase = "pre_product",
        parent_session_id: UUID | None = None,
    ) -> UUID:
        """Insert a new session row and return its UUID. Status starts at 'pending'.

        `phase` and `parent_session_id` are S13-PHASE-01. They default to the
        legacy values so every existing call site stays correct without a
        change. `parent_session_id` is intended for `gecko_pulse` (S14):
        the pulse session points back at the `pre_product` research that
        spawned it, and the FK guarantees the parent exists.
        """
        payload: dict[str, Any] = {
            "idea": idea,
            "tier": tier,
            "payment_mode": payment_mode,
            "status": "pending",
            "phase": phase,
        }
        if parent_session_id is not None:
            payload["parent_session_id"] = str(parent_session_id)

        def _insert() -> dict[str, Any]:
            res = self._client.table(self.SESSIONS_TABLE).insert(payload).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("session insert returned no rows")
            return cast(dict[str, Any], data[0])

        row = await asyncio.to_thread(_insert)
        return UUID(str(row["id"]))

    async def set_payment_intent(self, session_id: UUID, intent_id: str) -> None:
        """Persist the x402 idempotency key for this session.

        Called by the payment gate before `client.charge()` so that retries
        reuse the same key. Unique constraint on `payment_intent_id` prevents
        accidental double-charges across processes.
        """
        patch: dict[str, Any] = {"payment_intent_id": intent_id}

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def set_tx_signature(self, session_id: UUID, tx_signature: str) -> None:
        """Persist the on-chain x402 transaction signature for this session.

        Called by the gecko-api payment middleware integration after a
        successful settle so the session row can be correlated with the
        Solana explorer link shown on stage during the demo.
        """
        patch: dict[str, Any] = {"x402_tx_signature": tx_signature}

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def set_settlement_network(
        self,
        session_id: UUID,
        network: SettlementNetwork,
    ) -> None:
        """Persist which Solana cluster this session's x402 tx settled on.

        Used during the cutover canary (10% mainnet rollout) so forensic
        queries can attribute tx signatures to devnet vs mainnet without
        round-tripping to an explorer. Migration 012 adds the column with
        a CHECK constraint matching `SettlementNetwork`.

        `session_id` is the unit (we don't yet have a separate
        `x402_settlements` table — the signature lives on the session row).
        Renamed from the spec's `settlement_id` to reflect that.
        """
        patch: dict[str, Any] = {"network": network}

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def set_price(self, session_id: UUID, price_usd: float) -> None:
        """Persist the price the user was charged (USD).

        Recorded after the x402 settle succeeds, so the row's `margin_usd`
        generated column reflects revenue minus accumulated costs.
        """
        patch: dict[str, Any] = {"price_usd": price_usd}

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def add_cost(self, session_id: UUID, kind: CostKind, amount_usd: float) -> None:
        """Atomically increment a cost line for a session.

        Uses the `gecko_add_session_cost` Postgres function (migration
        20260427000000) so concurrent adapters can't race on read-modify-write.
        Zero-or-negligible amounts are short-circuited locally to avoid an
        unnecessary RPC round-trip.
        """
        if amount_usd <= 0:
            return

        def _rpc() -> None:
            self._client.rpc(
                "gecko_add_session_cost",
                {
                    "p_session_id": str(session_id),
                    "p_kind": kind,
                    "p_amount_usd": amount_usd,
                },
            ).execute()

        await asyncio.to_thread(_rpc)

    async def get_economics(self, session_id: UUID) -> SessionEconomics | None:
        """Return the unit-economics view for a session, or None if missing."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select(
                    "id,price_usd,cost_llm_usd,cost_embed_usd,cost_tavily_usd,"
                    "cost_deepgram_usd,cost_twitsh_usd,cost_v1_sources_usd,"
                    "cost_advisor_usd,cost_ask_usd,cost_classify_usd,"
                    "advisor_calls_count,ask_calls_count,"
                    "cost_total_usd,margin_usd,x402_tx_signature"
                )
                .eq("id", str(session_id))
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        if not rows:
            return None
        row = rows[0]
        return SessionEconomics(
            session_id=UUID(str(row["id"])),
            price_usd=_as_float_or_none(row.get("price_usd")),
            cost_llm_usd=float(row.get("cost_llm_usd") or 0),
            cost_embed_usd=float(row.get("cost_embed_usd") or 0),
            cost_tavily_usd=float(row.get("cost_tavily_usd") or 0),
            cost_deepgram_usd=float(row.get("cost_deepgram_usd") or 0),
            cost_twitsh_usd=float(row.get("cost_twitsh_usd") or 0),
            cost_v1_sources_usd=float(row.get("cost_v1_sources_usd") or 0),
            cost_advisor_usd=float(row.get("cost_advisor_usd") or 0),
            cost_ask_usd=float(row.get("cost_ask_usd") or 0),
            cost_classify_usd=float(row.get("cost_classify_usd") or 0),
            advisor_calls_count=int(row.get("advisor_calls_count") or 0),
            ask_calls_count=int(row.get("ask_calls_count") or 0),
            cost_total_usd=float(row.get("cost_total_usd") or 0),
            margin_usd=_as_float_or_none(row.get("margin_usd")),
            x402_tx_signature=row.get("x402_tx_signature"),
        )

    async def set_result(self, session_id: UUID, result: dict[str, Any]) -> None:
        """Persist the final ResearchResult JSON on the session row.

        Called from the background research task once the workflow finishes;
        the GET /sessions/{id}/result endpoint reads it back via get_result.
        """

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update({"result_json": result})
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def get_result(self, session_id: UUID) -> dict[str, Any] | None:
        """Return the persisted ResearchResult JSON, or None if not yet ready."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("result_json")
                .eq("id", str(session_id))
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        if not rows:
            return None
        result = rows[0].get("result_json")
        return cast(dict[str, Any] | None, result if isinstance(result, dict) else None)

    async def set_error(self, session_id: UUID, message: str) -> None:
        """Mark a session failed and persist the error message."""

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update({"status": "failed", "error_message": message[:2000]})
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def get_error(self, session_id: UUID) -> str | None:
        """Read back a failure message persisted by set_error."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("error_message,status")
                .eq("id", str(session_id))
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        if not rows:
            return None
        return rows[0].get("error_message") if rows[0].get("status") == "failed" else None

    async def update_status(self, session_id: UUID, status: SessionStatus) -> None:
        """Transition a session's status. Sets completed_at on terminal 'complete'."""
        patch: dict[str, Any] = {"status": status}
        if status == "complete":
            patch["completed_at"] = datetime.now().astimezone().isoformat()

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def mark_interrupted(
        self,
        session_id: UUID,
        *,
        reason: str = "container_shutdown",
    ) -> None:
        """Stamp a still-running session as interrupted with a typed reason.

        Only flips rows whose status is non-terminal — a session that
        already raced to `complete` / `failed` / `interrupted` between the
        shutdown handler reading the in-flight set and this UPDATE landing
        must NOT be reverted. Implemented as a `WHERE status IN (...)`
        guard at the database layer so it's race-free across workers.

        Sets `completed_at` so dashboards and economics queries that group
        by terminal timestamp don't ignore interrupted runs.
        """
        non_terminal: tuple[SessionStatus, ...] = ("pending", "indexing", "generating")
        patch: dict[str, Any] = {
            "status": "interrupted",
            "interrupted_reason": reason,
            "completed_at": datetime.now().astimezone().isoformat(),
        }

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .in_("status", list(non_terminal))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def get(self, session_id: UUID) -> SessionRecord | None:
        """Fetch a single session row, or None if not found / soft-deleted."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("*")
                .eq("id", str(session_id))
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        if not rows:
            return None
        return SessionRecord.model_validate(rows[0])

    async def list_sources(self, session_id: UUID) -> list[SourceInfo]:
        """Return indexed sources for a session, ordered by indexed_at ascending."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SOURCES_TABLE)
                .select("url,type,chunk_count,indexed_at")
                .eq("session_id", str(session_id))
                .order("indexed_at", desc=False)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        return [SourceInfo.model_validate(r) for r in rows]

    async def insert_source(
        self,
        session_id: UUID,
        url: str,
        url_hash: str,
        type_: Literal["youtube", "web", "provider"],
        *,
        provider_kind: ProviderKind = "web",
    ) -> UUID | None:
        """Insert a source row idempotently.

        Returns the new row's UUID, or None if the (session_id, url_hash)
        already exists. Uses upsert with ignore_duplicates so re-ingest of the
        same URL is a no-op rather than an error.

        S17-WEDGE-DATA-01 — `provider_kind` defaults to ``"web"`` so legacy
        Tavily callers stay correct without a change. ``type_="provider"``
        is the new shape for synthetic Bazaar/Arxiv/twit.sh source rows
        (see design memo §1.4); the existing CHECK is extended to allow
        it in the same migration.
        """
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "url": url,
            "url_hash": url_hash,
            "type": type_,
            "provider_kind": provider_kind,
        }

        def _upsert() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SOURCES_TABLE)
                .upsert(
                    payload,
                    on_conflict="session_id,url_hash",
                    ignore_duplicates=True,
                )
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_upsert)
        if not rows:
            return None
        return UUID(str(rows[0]["id"]))

    # S16-INGEST-02 — embedding dim is a single source of truth. If we
    # ever migrate to a different model + dim, this constant moves AND
    # the matching pgvector column type moves in lockstep via a migration.
    EMBED_DIM = 1024

    # S17-INGEST-SHED-01 — outermost batch size, then halved on retryable
    # errors (`toast_limit` / `supabase_5xx`). Lowered from 500 to 16 after
    # field observation: at 1536-dim float embeddings each row is ~30 KB
    # serialized JSON; a 25-43 row first attempt produced ~1-1.6 MB
    # payloads which consistently tripped supabase-py's transport on the
    # remote host (`RemoteProtocolError` → classifier bucket
    # `supabase_5xx` → halve + retry). Halving worked but cost double
    # latency per source. 16 rows × ~30 KB ≈ 500 KB which lands cleanly on
    # the first attempt; halvings stay as the safety net for unusually
    # large rows. Override with `GECKO_CHUNKS_UPSERT_BATCH_SIZE`.
    _INSERT_INITIAL_BATCH_SIZE_DEFAULT = 16
    _INSERT_MAX_HALVINGS = 2

    @classmethod
    def _initial_batch_size(cls) -> int:
        raw = os.getenv("GECKO_CHUNKS_UPSERT_BATCH_SIZE")
        if not raw:
            return cls._INSERT_INITIAL_BATCH_SIZE_DEFAULT
        try:
            n = int(raw)
        except ValueError:
            return cls._INSERT_INITIAL_BATCH_SIZE_DEFAULT
        return max(1, n)

    # Backwards-compat surface — older tests referenced the constant
    # directly. Resolved on access so env overrides take effect.
    @property
    def _INSERT_INITIAL_BATCH_SIZE(self) -> int:
        return self._initial_batch_size()

    async def insert_chunks(
        self,
        session_id: UUID,
        source_id: UUID,
        chunks: list[tuple[int, str, list[float]]],
        *,
        provider_kind: ProviderKind = "web",
        source_url: str | None = None,
    ) -> int:
        """Bulk-insert chunks for a source. Returns the count inserted.

        `chunks` is a list of (chunk_index, text, embedding) tuples.

        S16-INGEST-02 — transactional + idempotent.
          - Pre-flight validation rejects empty text / wrong-dim vectors
            via `ChunkValidationError` BEFORE any DB round-trip.
          - Each batch goes through Postgres upsert with
            `ON CONFLICT (source_id, chunk_index) DO NOTHING`, so a
            re-run of the same source produces zero duplicates.
          - On `toast_limit` / `supabase_5xx`, the failing batch is halved
            and retried twice. After two halvings the exception is
            re-raised so the audit row records the real error_kind.

        Chunked at the HTTP layer to keep individual requests under the
        default httpx read timeout. Some pages (e.g. wiki dumps) emit
        thousands of chunks; a single 13 MB upsert with 1536-dim vectors
        regularly trips supabase-py's read timeout.

        S18-MONGO-WRITE-01 — when ``GECKO_CHUNK_STORE=mongo``, dispatch to
        :func:`gecko_core.db.mongo_chunks.insert_chunks_mongo` and bypass
        the entire Supabase upsert path.
        """
        if not chunks:
            return 0

        from gecko_core.db import get_chunk_store

        if get_chunk_store() == "mongo":
            from gecko_core.db.mongo_chunks import insert_chunks_mongo

            return await insert_chunks_mongo(
                session_id,
                source_id,
                chunks,
                provider_kind=provider_kind,
                source_url=source_url,
            )

        # Lazy import — avoid a top-level cycle (audit imports nothing
        # from sessions, but exceptions lives next to it).
        from gecko_core.ingestion.audit import classify_exception
        from gecko_core.ingestion.exceptions import ChunkValidationError

        # ----- Pre-flight validation (S16-INGEST-02) ----------------------
        # Refuse a doomed insert before the wire. `_filter_embeddable` in
        # the pipeline should already have dropped empties, but if it
        # ever regresses we surface here as `embedding_null` (kind=
        # 'empty_text' on the exception) instead of letting Postgres trip
        # the new `chunks_text_nonempty_check`.
        for idx, text, embedding in chunks:
            if not text or not text.strip():
                raise ChunkValidationError(
                    f"chunk_index={idx} has empty/whitespace text",
                    kind="empty_text",
                )
            if len(embedding) != self.EMBED_DIM:
                raise ChunkValidationError(
                    f"chunk_index={idx} embedding dim {len(embedding)} "
                    f"!= expected {self.EMBED_DIM}",
                    kind="dim_mismatch",
                )

        # S17-WEDGE-DATA-01 — provider_kind defaults to 'web' for Tavily
        # callers; WIRE-02 passes the real value for bazaar/arxiv/twitsh.
        rows: list[dict[str, Any]] = [
            {
                "session_id": str(session_id),
                "source_id": str(source_id),
                "chunk_index": idx,
                "text": text,
                "embedding": embedding,
                "provider_kind": provider_kind,
            }
            for idx, text, embedding in chunks
        ]

        def _upsert_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # ON CONFLICT (source_id, chunk_index) DO NOTHING — the
            # existing UNIQUE constraint covers it. Idempotent re-runs
            # of the same source produce zero duplicate rows; supabase-py
            # returns the rows it actually inserted (skipped conflicts
            # are absent from `data`), so the count is the *new* writes.
            res = (
                self._client.table(self.CHUNKS_TABLE)
                .upsert(
                    batch,
                    on_conflict="source_id,chunk_index",
                    ignore_duplicates=True,
                )
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        # S17-INGEST-SHED-01 — cheap payload-size estimate for observation
        # logging. `text` dominates row weight; embeddings are 1536 floats
        # rendered as ~10-12 chars each in JSON ≈ 18 KB. Sum once per
        # batch on the first call and reuse via the closure.
        def _estimate_bytes(batch: list[dict[str, Any]]) -> int:
            # Avoid `json.dumps` on the full payload — that allocates a
            # second copy of an already large object. Approximate: sum of
            # text + 18 KB per embedding.
            n = len(batch)
            text_bytes = sum(len(r.get("text") or "") for r in batch)
            return text_bytes + n * 18_432

        async def _insert_with_shedding(
            batch: list[dict[str, Any]], halvings_left: int, *, top_level: bool = False
        ) -> int:
            """Run one batch transactionally; halve + retry on retryable kinds."""
            t0 = time.monotonic()
            try:
                # Result list discarded: ON CONFLICT DO NOTHING omits
                # conflicts from `data`, but our durability count comes
                # from the input batch length on the success branch
                # below — that's the right number for an idempotent
                # upsert (re-runs report all rows durable, not 0).
                await asyncio.to_thread(_upsert_batch, batch)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                kind = classify_exception(exc)
                if kind in {"toast_limit", "supabase_5xx"} and halvings_left > 0 and len(batch) > 1:
                    half = max(1, len(batch) // 2)
                    # S17-INGEST-SHED-01 — surface the real exception
                    # class + a truncated message so a noisy run gives us
                    # signal instead of "RemoteProtocolError? something
                    # else?". Keeps the warning shape backwards-compatible.
                    exc_msg = str(exc)
                    if len(exc_msg) > 240:
                        exc_msg = exc_msg[:237] + "..."
                    logger.warning(
                        "store.insert_chunks.shed_and_retry",
                        extra={
                            "session_id": str(session_id),
                            "source_id": str(source_id),
                            "batch_size": len(batch),
                            "next_batch_size": half,
                            "halvings_left": halvings_left,
                            "error_kind": kind,
                            "exc": exc.__class__.__name__,
                            "exc_msg": exc_msg,
                            "first_attempt_ms": elapsed_ms,
                            "payload_bytes_estimate": _estimate_bytes(batch),
                        },
                    )
                    left = batch[:half]
                    right = batch[half:]
                    a = await _insert_with_shedding(left, halvings_left - 1)
                    b = await _insert_with_shedding(right, halvings_left - 1)
                    return a + b
                # Non-retryable, or out of halvings: re-raise so the
                # pipeline's audit row gets the real error_kind.
                raise
            # On supabase-py upsert with ignore_duplicates, conflicting
            # rows are skipped — `len(inserted)` is the count of *new*
            # rows. We treat all batch rows as "successfully durable"
            # because either we just wrote them OR they were already
            # present from a prior run (which is exactly what idempotency
            # promises). FM-1 silent partial-drop without an exception
            # is no longer reachable here: a partial supabase response
            # without an error would be a contract break, not a normal
            # mode of operation.
            return len(batch)

        # Walk through the rows in initial-size batches; each batch is
        # transactional + idempotent + retry-on-shed. No accumulator of
        # "partial wins" — either every batch resolves cleanly or the
        # exception bubbles up.
        initial_batch_size = self._initial_batch_size()
        # S17-INGEST-SHED-01 — observation log at upsert entry. `n_rows`
        # is the total inbound, `payload_bytes_estimate` is the size of
        # the FIRST batch (since that's the one whose first-attempt
        # latency we care about). If shed_and_retry stops firing after
        # this change, this row plus the success path is the only signal
        # we keep.
        first_batch_preview = rows[:initial_batch_size]
        logger.info(
            "store.insert_chunks.start",
            extra={
                "session_id": str(session_id),
                "source_id": str(source_id),
                "n_rows": len(rows),
                "initial_batch_size": initial_batch_size,
                "first_batch_payload_bytes_estimate": _estimate_bytes(first_batch_preview),
            },
        )
        total_inserted = 0
        for i in range(0, len(rows), initial_batch_size):
            batch = rows[i : i + initial_batch_size]
            total_inserted += await _insert_with_shedding(
                batch, self._INSERT_MAX_HALVINGS, top_level=True
            )

        logger.info(
            "store.insert_chunks.done",
            extra={
                "session_id": str(session_id),
                "source_id": str(source_id),
                "batch_size": len(rows),
                "succeeded": total_inserted,
                "error_kind": "none",
            },
        )
        return total_inserted

    # ------------------------------------------------------------------
    # S16-INGEST-01 — chunks_write_audit. One row per source-batch exit.
    # Best-effort: if the audit insert fails we log and swallow — audit
    # observability must NEVER mask the real ingestion outcome.
    # ------------------------------------------------------------------

    async def insert_chunks_write_audit(
        self,
        *,
        session_id: UUID,
        source_id: UUID | None,
        batch_size: int,
        succeeded: int,
        failed: int,
        error_kind: str,
        embed_model: str | None,
    ) -> None:
        """Persist one audit row. Service-role only; never expose anon.

        S18-MONGO-WRITE-01 — dispatches to Mongo when the chunk store flag
        is ``mongo``. Audit remains best-effort in both stores.
        """
        from gecko_core.db import get_chunk_store

        if get_chunk_store() == "mongo":
            from gecko_core.db.mongo_chunks import insert_chunks_write_audit_mongo

            await insert_chunks_write_audit_mongo(
                session_id=session_id,
                source_id=source_id,
                batch_size=batch_size,
                succeeded=succeeded,
                failed=failed,
                error_kind=error_kind,
                embed_model=embed_model,
            )
            return

        row: dict[str, Any] = {
            "session_id": str(session_id),
            "source_id": str(source_id) if source_id is not None else None,
            "batch_size": batch_size,
            "succeeded": succeeded,
            "failed": failed,
            "error_kind": error_kind,
            "embed_model": embed_model,
        }

        def _insert() -> None:
            self._client.table(self.CHUNKS_WRITE_AUDIT_TABLE).insert(row).execute()

        # Audit is best-effort. The classifier exists precisely so we
        # don't lose the outcome — but losing the *audit row* must not
        # crash the request. Caller already logged the real outcome.
        import contextlib

        with contextlib.suppress(Exception):
            await asyncio.to_thread(_insert)

    async def chunks_write_audit_rollup_recent(
        self,
        *,
        days: int = 7,
    ) -> list[dict[str, Any]]:
        """Return [{error_kind, count}] for the last `days`. Used by
        `bb doctor --recent`. PostgREST has no GROUP BY, so we pull the
        raw `error_kind` column over the window and aggregate in Python —
        the table is small (one row per source-batch) and the window is
        bounded.
        """
        from datetime import datetime, timedelta

        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.CHUNKS_WRITE_AUDIT_TABLE)
                .select("error_kind")
                .gte("captured_at", since)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        counts: dict[str, int] = {}
        for r in rows:
            kind = str(r.get("error_kind") or "unknown")
            counts[kind] = counts.get(kind, 0) + 1
        # Stable order: success first, then failure buckets alphabetical.
        ordered = sorted(counts.items(), key=lambda kv: (kv[0] != "none", kv[0]))
        return [{"error_kind": k, "count": v} for k, v in ordered]

    async def match_chunks_windowed(
        self,
        *,
        query_embedding: list[float],
        window_days: int | None,
        project_id: UUID | None,
        match_count: int = 8,
    ) -> list[ChunkMatch]:
        """Windowed similarity search via the `match_chunks_windowed` RPC.

        S13-PHASE-02. Pre-filters on `(project_id, captured_at)` BEFORE the
        vector ANN — exactly the order chunks_project_captured_at_idx is
        built for. `window_days=None` (or <=0) disables the temporal cap;
        `project_id=None` matches across projects (rare; caller decides).

        S18-MONGO-READ-01 — when ``GECKO_CHUNK_STORE=mongo`` dispatches to
        :func:`gecko_core.db.mongo_reads.match_chunks_windowed_mongo` which
        applies the same project + temporal filter via ``$vectorSearch.filter``.
        """
        from gecko_core.db import get_chunk_store

        if get_chunk_store() == "mongo":
            from gecko_core.db.mongo_reads import match_chunks_windowed_mongo

            rows = await match_chunks_windowed_mongo(
                query_embedding=query_embedding,
                window_days=window_days,
                project_id=project_id,
                match_count=match_count,
            )
            return [ChunkMatch.model_validate(r) for r in rows]

        params: dict[str, Any] = {
            "query_embedding": query_embedding,
            "window_days": window_days,
            "p_project_id": str(project_id) if project_id else None,
            "match_count": match_count,
        }

        def _rpc() -> list[dict[str, Any]]:
            res = self._client.rpc("match_chunks_windowed", params).execute()
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_rpc)
        return [ChunkMatch.model_validate(r) for r in rows]

    # ------------------------------------------------------------------
    # Projects (per-project vaults)
    # ------------------------------------------------------------------
    #
    # v1 model: a project is a named, budgeted grouping of sessions owned by
    # a single frames.ag user. Budget enforcement is client-side — callers
    # check `project_budget_remaining` before issuing a paid run. The
    # `wallet_address` / `wallet_provider` columns exist for v2 forward-compat
    # (Privy direct wallets) and are NULL / 'frames-policy' in v1.

    PROJECTS_TABLE = "projects"

    async def create_project(
        self,
        username: str,
        name: str,
        budget_usd: float | None = None,
    ) -> UUID:
        """Insert a new project row for `username`. Returns its UUID.

        Unique on (frames_username, name); the supabase-py client will raise
        on conflict so callers can map that to a 409.
        """
        payload: dict[str, Any] = {
            "frames_username": username,
            "name": name,
            "budget_usd": budget_usd,
        }

        def _insert() -> dict[str, Any]:
            res = self._client.table(self.PROJECTS_TABLE).insert(payload).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("project insert returned no rows")
            return cast(dict[str, Any], data[0])

        row = await asyncio.to_thread(_insert)
        return UUID(str(row["id"]))

    async def get_project_by_id_for_user(
        self,
        username: str,
        project_id: UUID,
    ) -> dict[str, Any] | None:
        """Look up a project by (owner, id). None if missing, wrong owner, or soft-deleted.

        Used by per-project endpoints (S2-09 economics, S2-05 wallet ops) to
        enforce ownership without leaking existence — an authenticated user
        querying someone else's project sees the same 404 they'd see for an
        unknown UUID.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.PROJECTS_TABLE)
                .select("*")
                .eq("frames_username", username)
                .eq("id", str(project_id))
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        return rows[0] if rows else None

    async def get_project(self, username: str, name: str) -> dict[str, Any] | None:
        """Look up a project by (owner, name). None if missing or soft-deleted."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.PROJECTS_TABLE)
                .select("*")
                .eq("frames_username", username)
                .eq("name", name)
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        return rows[0] if rows else None

    async def list_projects(self, username: str) -> list[dict[str, Any]]:
        """Return all live projects for a frames.ag user, newest first."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.PROJECTS_TABLE)
                .select("*")
                .eq("frames_username", username)
                .is_("deleted_at", None)
                .order("created_at", desc=True)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        return await asyncio.to_thread(_select)

    async def set_session_project(
        self,
        session_id: UUID,
        project_id: UUID,
        paid_from_wallet_address: str | None = None,
    ) -> None:
        """Bind a session to a project and snapshot the paying wallet.

        Called from the api_client right after the payment intent is created
        so the audit trail captures which wallet actually paid, even if the
        project's wallet later rotates.
        """
        patch: dict[str, Any] = {"project_id": str(project_id)}
        if paid_from_wallet_address is not None:
            patch["paid_from_wallet_address"] = paid_from_wallet_address

        def _update() -> None:
            (
                self._client.table(self.SESSIONS_TABLE)
                .update(patch)
                .eq("id", str(session_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def project_budget_remaining(self, project_id: UUID) -> float | None:
        """Return budget_usd - SUM(cost_total_usd) via the SQL RPC.

        None when the project has no budget set (unlimited) or doesn't
        exist. Negative values signal an overrun — surface that to callers
        rather than clamping at 0.
        """

        def _rpc() -> Any:
            res = self._client.rpc(
                "gecko_project_budget_remaining",
                {"p_project_id": str(project_id)},
            ).execute()
            return res.data

        data = await asyncio.to_thread(_rpc)
        return _as_float_or_none(data)

    async def project_total_spent(self, project_id: UUID) -> float:
        """Sum cost_total_usd across all live sessions in a project.

        Implemented as a direct aggregation (not the RPC) so callers that
        only want spend — not remaining — don't pay for the project lookup.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("cost_total_usd")
                .eq("project_id", str(project_id))
                .is_("deleted_at", None)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        return float(sum((r.get("cost_total_usd") or 0) for r in rows))

    # ------------------------------------------------------------------
    # Cross-session embedding cache (migration 20260430052216, S8-INGEST-03).
    #
    # Keyed on (url_hash, chunk_index). The pipeline checks this BEFORE
    # calling the embedder so re-ingesting the same URL (e.g. same Tavily
    # result returned by a later idea in the dogfood matrix) reuses the
    # vectors instead of paying OpenAI again. Pure cache — safe to wipe.
    # ------------------------------------------------------------------

    async def get_chunk_cache(
        self,
        url_hash: str,
        indices: list[int],
        *,
        embed_model: str | None = None,
    ) -> dict[int, list[float]]:
        """Return {chunk_index: embedding} for the given (url_hash, idx) pairs.

        Empty dict on miss / unavailable cache table. Best-effort: a
        Supabase failure here must not crash ingestion (the caller catches
        and falls through to a live embed).

        S16-INGEST-03 — `embed_model` filters on the cache PK fingerprint.
        When None (legacy callers), no model filter is applied — the
        lookup behaves like pre-S16 and may return any cached row. New
        callers (the pipeline) should always pass the active model from
        `IngestionSettings().embed_model` so a model swap forces a
        re-embed instead of returning a poisoned vector.

        S18-MONGO-WRITE-01 — Mongo branch lives in
        :func:`gecko_core.db.mongo_chunks.get_chunk_cache_mongo`.
        """
        if not indices:
            return {}

        from gecko_core.db import get_chunk_store

        if get_chunk_store() == "mongo":
            from gecko_core.db.mongo_chunks import get_chunk_cache_mongo

            return await get_chunk_cache_mongo(url_hash, indices, embed_model=embed_model)

        def _select() -> list[dict[str, Any]]:
            q = (
                self._client.table(self.CHUNK_EMBEDDING_CACHE_TABLE)
                .select("chunk_index,embedding,embed_model")
                .eq("url_hash", url_hash)
                .in_("chunk_index", indices)
            )
            if embed_model is not None:
                q = q.eq("embed_model", embed_model)
            res = q.execute()
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        out: dict[int, list[float]] = {}
        evict_indices: list[int] = []
        for r in rows:
            idx = r.get("chunk_index")
            emb = r.get("embedding")
            if idx is None or emb is None:
                continue
            # Supabase returns pgvector as either a list or its string
            # representation depending on PostgREST version — normalize.
            if isinstance(emb, str):
                try:
                    import json as _json

                    emb = _json.loads(emb)
                except Exception:
                    continue
            if not isinstance(emb, list):
                continue
            vec = [float(v) for v in emb]
            # S16-INGEST-02 — dim-validate cache hit. FM-2 (poisoned
            # cache after a model change) returns a wrong-dim vector
            # that would trip Postgres `vector(1536)` mismatch on the
            # downstream insert, fail the whole batch, and surface as
            # `dim_mismatch` after a wasted DB round-trip. Catch it here
            # and evict so the pipeline re-embeds.
            if len(vec) != self.EMBED_DIM:
                logger.warning(
                    "store.cache.dim_mismatch_evict",
                    extra={
                        "url_hash": url_hash,
                        "chunk_index": int(idx),
                        "got_dim": len(vec),
                        "expected_dim": self.EMBED_DIM,
                        "error_kind": "dim_mismatch",
                    },
                )
                evict_indices.append(int(idx))
                continue
            out[int(idx)] = vec

        # Evict poisoned rows OUT OF BAND of the read path's contract:
        # the cache is best-effort, so a delete failure here must not
        # fail the lookup. The pipeline will simply re-embed the missing
        # indices on this run; if the eviction also fails, next run
        # tries again. Idempotency wins.
        if evict_indices:
            try:
                await asyncio.to_thread(self._evict_chunk_cache, url_hash, evict_indices)
            except Exception as exc:  # pragma: no cover — best-effort
                logger.info(
                    "store.cache.evict_failed url_hash=%s err=%s",
                    url_hash,
                    exc.__class__.__name__,
                )
        return out

    def _evict_chunk_cache(self, url_hash: str, indices: list[int]) -> None:
        """Delete poisoned cache rows. Sync helper invoked via to_thread.

        Kept private; the only caller is `get_chunk_cache`'s eviction path.
        Filtered by `url_hash` first so RLS / index pruning does the right
        thing even on a large cache.
        """
        (
            self._client.table(self.CHUNK_EMBEDDING_CACHE_TABLE)
            .delete()
            .eq("url_hash", url_hash)
            .in_("chunk_index", indices)
            .execute()
        )

    async def put_chunk_cache(
        self,
        url_hash: str,
        rows: list[tuple[int, str, list[float]]],
        *,
        embed_model: str | None = None,
    ) -> None:
        """Upsert (url_hash, chunk_index, embed_model) → (text, embedding) rows.

        ON CONFLICT DO NOTHING semantics via supabase-py upsert with
        ignore_duplicates=True — first writer wins, concurrent ingests of
        the same URL don't fight. Best-effort: errors are logged by the
        caller, never raised.

        S16-INGEST-03 — `embed_model` writes the model fingerprint into
        the new PK column. When None, the column DEFAULT
        ('text-embedding-3-small') applies — preserves pre-S16 behavior
        for any legacy caller that hasn't been updated.

        S18-MONGO-WRITE-01 — Mongo branch in
        :func:`gecko_core.db.mongo_chunks.put_chunk_cache_mongo`.
        """
        if not rows:
            return

        from gecko_core.db import get_chunk_store

        if get_chunk_store() == "mongo":
            from gecko_core.db.mongo_chunks import put_chunk_cache_mongo

            await put_chunk_cache_mongo(url_hash, rows, embed_model=embed_model)
            return
        payload: list[dict[str, Any]] = [
            {
                "url_hash": url_hash,
                "chunk_index": idx,
                "text": text,
                "embedding": embedding,
                **({"embed_model": embed_model} if embed_model is not None else {}),
            }
            for idx, text, embedding in rows
        ]

        def _upsert() -> None:
            (
                self._client.table(self.CHUNK_EMBEDDING_CACHE_TABLE)
                .upsert(
                    payload,
                    on_conflict="url_hash,chunk_index,embed_model",
                    ignore_duplicates=True,
                )
                .execute()
            )

        await asyncio.to_thread(_upsert)

    async def set_source_chunk_count(self, source_id: UUID, count: int) -> None:
        """Update sources.chunk_count after chunk insert."""

        def _update() -> None:
            (
                self._client.table(self.SOURCES_TABLE)
                .update({"chunk_count": count})
                .eq("id", str(source_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    # ------------------------------------------------------------------
    # Projects (Phase B5 v1) — supplemental helpers.
    #
    # The core CRUD (create_project, get_project, list_projects,
    # project_budget_remaining, project_total_spent, set_session_project)
    # is implemented above by the data-engineer. The two helpers below are
    # CLI-driven niceties (`gecko project budget --set`, `gecko project show`)
    # that don't need their own RPC.
    # ------------------------------------------------------------------

    async def delete_project(self, username: str, name: str) -> bool:
        """Soft-delete a project by (owner, name). Returns True if a row updated.

        Sets ``deleted_at`` to now(); subsequent reads filter on
        ``deleted_at IS NULL`` so the project disappears from list/get.
        """
        now_iso = datetime.now().astimezone().isoformat()

        def _update() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.PROJECTS_TABLE)
                .update({"deleted_at": now_iso})
                .eq("frames_username", username)
                .eq("name", name)
                .is_("deleted_at", None)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_update)
        return bool(rows)

    async def set_project_budget(self, project_id: UUID, budget_usd: float | None) -> None:
        """Update the project's budget_usd ceiling."""

        def _update() -> None:
            (
                self._client.table(self.PROJECTS_TABLE)
                .update({"budget_usd": budget_usd})
                .eq("id", str(project_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def set_project_wallet(
        self,
        project_id: UUID,
        *,
        privy_wallet_id: str,
        privy_wallet_address: str,
    ) -> None:
        """Bind a Privy v2 embedded wallet to a project (Sprint 2 S2-05).

        Sets both columns in one update so callers don't have to deal with
        a half-provisioned project. Idempotent at the row level — calling
        twice with the same values is a no-op; calling with a different
        `privy_wallet_id` will trip the unique index from migration 013.
        """
        patch: dict[str, Any] = {
            "privy_wallet_id": privy_wallet_id,
            "privy_wallet_address": privy_wallet_address,
        }

        def _update() -> None:
            (
                self._client.table(self.PROJECTS_TABLE)
                .update(patch)
                .eq("id", str(project_id))
                .execute()
            )

        await asyncio.to_thread(_update)

    async def get_project_wallet(self, project_id: UUID) -> ProjectWallet | None:
        """Return the project's wallet + budget snapshot, or None if missing.

        Filters out soft-deleted projects. `privy_wallet_id` /
        `privy_wallet_address` are NULL for projects that haven't been
        lazy-provisioned yet — callers must handle that case.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.PROJECTS_TABLE)
                .select("privy_wallet_id,privy_wallet_address,budget_cap_usd,spent_usd")
                .eq("id", str(project_id))
                .is_("deleted_at", None)
                .limit(1)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        if not rows:
            return None
        row = rows[0]
        return ProjectWallet(
            privy_wallet_id=row.get("privy_wallet_id"),
            privy_wallet_address=row.get("privy_wallet_address"),
            budget_cap_usd=Decimal(str(row.get("budget_cap_usd") or 0)),
            spent_usd=Decimal(str(row.get("spent_usd") or 0)),
        )

    async def increment_project_spent(
        self,
        project_id: UUID,
        delta_usd: Decimal,
    ) -> Decimal:
        """Atomically add `delta_usd` to projects.spent_usd. Returns the new total.

        Implemented via the `gecko_increment_project_spent` RPC so concurrent
        session-completion handlers can't race on a read-modify-write. The
        RPC performs `UPDATE projects SET spent_usd = spent_usd + $1
        WHERE id = $2 RETURNING spent_usd` — see migration 013 (or its
        follow-up that ships the RPC) for the exact definition.

        NOTE: until the trigger lands (web3-engineer, Sprint 2), the API is
        responsible for calling this on each session settle. Negative deltas
        are allowed for refund flows.
        """

        def _rpc() -> Any:
            res = self._client.rpc(
                "gecko_increment_project_spent",
                {
                    "p_project_id": str(project_id),
                    "p_delta_usd": str(delta_usd),
                },
            ).execute()
            return res.data

        data = await asyncio.to_thread(_rpc)
        if data is None:
            raise RuntimeError(f"increment_project_spent: project {project_id} not found")
        return Decimal(str(data))

    async def list_project_sessions(
        self,
        project_id: UUID,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Recent sessions for a project — id, idea, status, cost, created_at."""

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("id,idea,status,cost_total_usd,created_at")
                .eq("project_id", str(project_id))
                .is_("deleted_at", None)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        return await asyncio.to_thread(_select)

    async def list_project_paid_sessions(
        self,
        project_id: UUID,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Recent paid sessions for the project economics view (S2-09).

        Selects only the columns the project-scoped economics endpoint needs:
        tier, price paid, cost rollup, on-chain tx signature, network, and
        timestamp. Filters to sessions that actually settled (have a
        non-null x402_tx_signature) so the dashboard isn't polluted by 402s
        the user abandoned mid-flow.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("id,tier,price_usd,cost_total_usd,x402_tx_signature,network,created_at")
                .eq("project_id", str(project_id))
                .is_("deleted_at", None)
                .not_.is_("x402_tx_signature", None)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        return await asyncio.to_thread(_select)

    # ------------------------------------------------------------------
    # Pro tier SSE event buffer (migration 010_pro_events).
    #
    # Append-only log; producers (the AG2 GroupChat runner) call
    # `append_pro_event` per turn, the SSE endpoint poll-tails via
    # `tail_pro_events`. No business logic here — both helpers are thin
    # passthroughs over the supabase-py client to match the rest of this
    # store's pattern.
    # ------------------------------------------------------------------

    PRO_EVENTS_TABLE = "pro_events"

    async def append_pro_event(
        self,
        session_id: UUID,
        seq: int,
        event_type: ProEventType,
        agent: str | None,
        content: str,
        tokens_in: int,
        tokens_out: int,
        ts: float,
    ) -> int:
        """Append a Pro-tier event row. Returns the new BIGSERIAL `id`.

        Raises if the (session_id, seq) UNIQUE constraint is violated —
        callers are responsible for monotonically incrementing `seq`.
        """
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "seq": seq,
            "event_type": event_type,
            "agent": agent,
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "ts": ts,
        }

        def _insert() -> dict[str, Any]:
            res = self._client.table(self.PRO_EVENTS_TABLE).insert(payload).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("pro_events insert returned no rows")
            return cast(dict[str, Any], data[0])

        row = await asyncio.to_thread(_insert)
        return int(row["id"])

    async def tail_pro_events(
        self,
        session_id: UUID,
        after_id: int,
        limit: int = 50,
    ) -> list[ProEventRow]:
        """Return events for `session_id` with `id > after_id`, ascending.

        Backed by `idx_pro_events_tail (session_id, id)`. The SSE handler
        loops on this with the highest `id` it last emitted; pass 0 on the
        first call to drain from the start.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.PRO_EVENTS_TABLE)
                .select("*")
                .eq("session_id", str(session_id))
                .gt("id", after_id)
                .order("id", desc=False)
                .limit(limit)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        return [ProEventRow.model_validate(r) for r in rows]

    # ------------------------------------------------------------------
    # Pro tier per-agent cost ledger (migration 009_session_costs_agent).
    #
    # Distinct from the rolled-up `sessions.cost_*` columns Basic tier uses.
    # Pro tier writes one row per LLM call (with `agent` populated) so the
    # per-agent rollup in the Pro UI doesn't have to re-derive attribution
    # from event logs.
    # ------------------------------------------------------------------

    SESSION_COSTS_TABLE = "session_costs"

    async def append_session_cost(
        self,
        session_id: UUID,
        line_item: Literal["llm", "embed", "tavily", "deepgram"],
        agent: str | None,
        cost_usd: Decimal,
    ) -> int:
        """Append a per-call cost row. Returns the new BIGSERIAL `id`.

        Pro tier callers populate `agent` on `line_item='llm'` rows; Basic
        tier should not write here (it uses the rolled-up sessions.cost_*
        columns via `add_cost`).
        """
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "line_item": line_item,
            "agent": agent,
            "cost_usd": str(cost_usd),
        }

        def _insert() -> dict[str, Any]:
            res = self._client.table(self.SESSION_COSTS_TABLE).insert(payload).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("session_costs insert returned no rows")
            return cast(dict[str, Any], data[0])

        row = await asyncio.to_thread(_insert)
        return int(row["id"])

    async def get_pro_agent_costs(self, session_id: UUID) -> dict[str, Decimal]:
        """Per-agent LLM cost rollup for a Pro session.

        Returns `{agent: total_cost_usd}` over `line_item='llm' AND agent IS
        NOT NULL`. Empty dict if the session has no Pro-tier LLM rows yet.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSION_COSTS_TABLE)
                .select("agent,cost_usd")
                .eq("session_id", str(session_id))
                .eq("line_item", "llm")
                .not_.is_("agent", None)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        totals: dict[str, Decimal] = {}
        for r in rows:
            agent = r.get("agent")
            if not agent:
                continue
            cost = r.get("cost_usd")
            if cost is None:
                continue
            totals[agent] = totals.get(agent, Decimal("0")) + Decimal(str(cost))
        return totals

    # ------------------------------------------------------------------
    # Gecko Flywheel — internal precedent corpus (migration 015).
    #
    # Every Pro session writes one row: an LLM-abstracted category summary
    # (NEVER verbatim user text), the verdict, comparables, and the
    # idea_summary's embedding. New sessions retrieve top-N similar via
    # cosine on the ivfflat index. Privacy guardrail (S2X-05) lives in CI.
    # ------------------------------------------------------------------

    GECKO_PRECEDENT_TABLE = "gecko_precedent"

    async def append_gecko_precedent(
        self,
        *,
        session_id: UUID,
        user_id: UUID | None,
        idea_summary: str,
        idea_hash: str,
        category_tags: list[str],
        verdict: Verdict,
        key_comparables: list[Any],
        embedding: list[float],
    ) -> UUID:
        """Insert one flywheel row. Returns the new precedent UUID.

        Caller is responsible for the privacy contract on `idea_summary`
        (LLM-abstracted 1-sentence category, not verbatim). `idea_hash`
        is sha256 of the normalized idea — used as a dedup signal by the
        write hook (S2X-04), not enforced as UNIQUE here so re-runs of the
        same idea with a different verdict still record both data points.
        """
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "user_id": str(user_id) if user_id else None,
            "idea_summary": idea_summary,
            "idea_hash": idea_hash,
            "category_tags": category_tags,
            "verdict": verdict,
            "key_comparables": key_comparables,
            "embedding": embedding,
        }

        def _insert() -> dict[str, Any]:
            res = self._client.table(self.GECKO_PRECEDENT_TABLE).insert(payload).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("gecko_precedent insert returned no rows")
            return cast(dict[str, Any], data[0])

        row = await asyncio.to_thread(_insert)
        return UUID(str(row["id"]))

    async def retrieve_gecko_precedent(
        self,
        *,
        embedding: list[float],
        similarity_threshold: float = 0.78,
        limit: int = 5,
    ) -> list[GeckoPrecedent]:
        """Top-`limit` precedents above `similarity_threshold`, by cosine sim desc.

        Backed by the `gecko_precedent_match` RPC (migration 015) which
        pre-filters on threshold so the ivfflat index does the work.
        """

        def _rpc() -> list[dict[str, Any]]:
            res = self._client.rpc(
                "gecko_precedent_match",
                {
                    "query_embedding": embedding,
                    "similarity_threshold": similarity_threshold,
                    "match_limit": limit,
                },
            ).execute()
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_rpc)
        out: list[GeckoPrecedent] = []
        for r in rows:
            sid = r.get("session_id")
            uid = r.get("user_id")
            comparables = r.get("key_comparables") or []
            out.append(
                GeckoPrecedent(
                    id=UUID(str(r["id"])),
                    session_id=UUID(str(sid)) if sid else None,
                    user_id=UUID(str(uid)) if uid else None,
                    idea_summary=str(r["idea_summary"]),
                    verdict=cast(Verdict, r["verdict"]),
                    # `outcome` was added in migration
                    # 20260430054542_precedent_outcomes. RPCs deployed against
                    # databases that haven't run that migration yet won't
                    # return the column — default to 'unknown' so the
                    # transition window is graceful.
                    outcome=cast(PrecedentOutcome, r.get("outcome") or "unknown"),
                    key_comparables=list(comparables) if isinstance(comparables, list) else [],
                    similarity=_as_float_or_none(r.get("similarity")),
                )
            )
        return out

    async def delete_gecko_precedent(
        self,
        *,
        user_id: UUID,
        precedent_id: UUID,
    ) -> bool:
        """Delete one of the user's own precedent rows. Returns True if deleted.

        Self-service privacy escape. Filter is owner-scoped at the
        application layer in addition to the RLS policy so service-role
        callers (which bypass RLS) still can't accidentally nuke another
        user's row.
        """

        def _delete() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.GECKO_PRECEDENT_TABLE)
                .delete()
                .eq("id", str(precedent_id))
                .eq("user_id", str(user_id))
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_delete)
        return bool(rows)

    # ------------------------------------------------------------------
    # Pulse runs (S5-API-02) — Advisor Panel pulse history.
    #
    # One row per `run_pulse` invocation. The advisor walks this table
    # backwards by created_at to find the immediately-prior panel for
    # delta detection. Project-scoped queries take precedence; falling
    # back to session-scoped is back-compat for v1 single-shot pulses.
    # ------------------------------------------------------------------

    PULSE_RUNS_TABLE = "pulse_runs"

    async def insert_pulse_run(
        self,
        *,
        session_id: UUID,
        project_id: UUID | None,
        panel_json: dict[str, Any],
        deltas_json: list[dict[str, Any]],
    ) -> UUID:
        """Append a pulse run. Returns the new row's UUID."""
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "project_id": str(project_id) if project_id else None,
            "panel_json": panel_json,
            "deltas_json": deltas_json,
        }

        def _insert() -> dict[str, Any]:
            res = self._client.table(self.PULSE_RUNS_TABLE).insert(payload).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("pulse_runs insert returned no rows")
            return cast(dict[str, Any], data[0])

        row = await asyncio.to_thread(_insert)
        return UUID(str(row["id"]))

    async def get_latest_pulse_run(
        self,
        *,
        session_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent pulse_runs row for a project or session.

        Project takes precedence when both are given (matches `run_pulse`'s
        documented contract). Returns None when there's no prior row in
        scope — the caller treats that as "no prior pulse on file".
        """
        if project_id is None and session_id is None:
            raise ValueError("get_latest_pulse_run: project_id or session_id required")

        def _select() -> list[dict[str, Any]]:
            q = self._client.table(self.PULSE_RUNS_TABLE).select("*")
            if project_id is not None:
                q = q.eq("project_id", str(project_id))
            else:
                q = q.eq("session_id", str(session_id))
            res = q.order("created_at", desc=True).limit(1).execute()
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        return rows[0] if rows else None

    async def _project_spend(self, project_id: UUID) -> tuple[float, int]:
        """Sum cost_total_usd + count sessions for a project.

        Helper for the free `/sessions/spent-by-project/{id}` endpoint —
        avoids two round-trips when the caller wants both.
        """

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.SESSIONS_TABLE)
                .select("cost_total_usd")
                .eq("project_id", str(project_id))
                .is_("deleted_at", None)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        total = float(sum((r.get("cost_total_usd") or 0) for r in rows))
        return total, len(rows)


def _as_float_or_none(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "ChunkMatch",
    "CostKind",
    "GeckoPrecedent",
    "PaymentMode",
    "PrecedentOutcome",
    "ProEventRow",
    "ProEventType",
    "ProjectWallet",
    "SessionEconomics",
    "SessionPhase",
    "SessionRecord",
    "SessionStore",
    "SettlementNetwork",
    "Verdict",
]
