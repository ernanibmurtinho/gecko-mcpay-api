"""Session persistence layer.

`SessionStore` is the single seam between gecko-core business logic and the
Supabase `sessions` / `sources` tables. Async surface; the underlying
supabase-py client is sync, so calls are dispatched via asyncio.to_thread to
avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel
from supabase import Client

from gecko_core.db import create_supabase_client
from gecko_core.models import SessionStatus, SourceInfo, Tier

PaymentMode = Literal["stub", "live", "frames", "cdp"]
"""Session-row payment-mode literal.

Kept in sync with `gecko_core.payments.modes.PAYMENT_MODES`. We can't
re-export from there directly because `gecko_core.payments.__init__`
imports `gate`, which imports `SessionStore` — a circular import the
moment session-store-load triggers payments-package-init.

The drift is caught at runtime: `tests/test_payment_mode_consistency.py`
asserts `typing.get_args(PaymentMode)` matches `PAYMENT_MODES` exactly,
and a startup-time assertion below catches it before any test runs."""

CostKind = Literal["llm", "embed", "tavily", "deepgram", "twitsh", "v1_sources"]

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
    ) -> UUID:
        """Insert a new session row and return its UUID. Status starts at 'pending'."""
        payload: dict[str, Any] = {
            "idea": idea,
            "tier": tier,
            "payment_mode": payment_mode,
            "status": "pending",
        }

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
        type_: Literal["youtube", "web"],
    ) -> UUID | None:
        """Insert a source row idempotently.

        Returns the new row's UUID, or None if the (session_id, url_hash)
        already exists. Uses upsert with ignore_duplicates so re-ingest of the
        same URL is a no-op rather than an error.
        """
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "url": url,
            "url_hash": url_hash,
            "type": type_,
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

    async def insert_chunks(
        self,
        session_id: UUID,
        source_id: UUID,
        chunks: list[tuple[int, str, list[float]]],
    ) -> int:
        """Bulk-insert chunks for a source. Returns the count inserted.

        `chunks` is a list of (chunk_index, text, embedding) tuples.
        """
        if not chunks:
            return 0
        rows: list[dict[str, Any]] = [
            {
                "session_id": str(session_id),
                "source_id": str(source_id),
                "chunk_index": idx,
                "text": text,
                "embedding": embedding,
            }
            for idx, text, embedding in chunks
        ]

        def _insert() -> list[dict[str, Any]]:
            res = self._client.table(self.CHUNKS_TABLE).insert(rows).execute()
            return cast(list[dict[str, Any]], res.data or [])

        inserted = await asyncio.to_thread(_insert)
        return len(inserted)

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
    ) -> dict[int, list[float]]:
        """Return {chunk_index: embedding} for the given (url_hash, idx) pairs.

        Empty dict on miss / unavailable cache table. Best-effort: a
        Supabase failure here must not crash ingestion (the caller catches
        and falls through to a live embed).
        """
        if not indices:
            return {}

        def _select() -> list[dict[str, Any]]:
            res = (
                self._client.table(self.CHUNK_EMBEDDING_CACHE_TABLE)
                .select("chunk_index,embedding")
                .eq("url_hash", url_hash)
                .in_("chunk_index", indices)
                .execute()
            )
            return cast(list[dict[str, Any]], res.data or [])

        rows = await asyncio.to_thread(_select)
        out: dict[int, list[float]] = {}
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
            if isinstance(emb, list):
                out[int(idx)] = [float(v) for v in emb]
        return out

    async def put_chunk_cache(
        self,
        url_hash: str,
        rows: list[tuple[int, str, list[float]]],
    ) -> None:
        """Upsert (url_hash, chunk_index) → (text, embedding) rows.

        ON CONFLICT DO NOTHING semantics via supabase-py upsert with
        ignore_duplicates=True — first writer wins, concurrent ingests of
        the same URL don't fight. Best-effort: errors are logged by the
        caller, never raised.
        """
        if not rows:
            return
        payload: list[dict[str, Any]] = [
            {
                "url_hash": url_hash,
                "chunk_index": idx,
                "text": text,
                "embedding": embedding,
            }
            for idx, text, embedding in rows
        ]

        def _upsert() -> None:
            (
                self._client.table(self.CHUNK_EMBEDDING_CACHE_TABLE)
                .upsert(
                    payload,
                    on_conflict="url_hash,chunk_index",
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
    "CostKind",
    "GeckoPrecedent",
    "PaymentMode",
    "PrecedentOutcome",
    "ProEventRow",
    "ProEventType",
    "ProjectWallet",
    "SessionEconomics",
    "SessionRecord",
    "SessionStore",
    "SettlementNetwork",
    "Verdict",
]
