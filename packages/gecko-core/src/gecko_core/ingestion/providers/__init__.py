"""Source-provider Protocol seam.

Sprint 12 S12-PROVIDER-01: a thin Protocol that lets us add new ingestion
backends (CDP Bazaar, Paragraph, Cloudflare, vertical APIs) as a config
change rather than a refactor of `discovery.py` + `pipeline.ingest()`.

Today's Tavily/web/youtube dispatch is wrapped as the default
`FreeProvider` in `free_provider.py`. **No new providers are wired in
this ticket.** The seam is the deliverable; downstream sprints (S13+)
plug in `BazaarProvider`, `ParagraphProvider`, etc.

Architectural rationale: see
`docs/strategy/bazaar-composer-staff-eng-review-2026-04-30.md` § 1a.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from gecko_core.models import SourceCandidate

# What kind of trust + payment flow a provider operates under. This drives
# (a) the receipt-rendering shape (free providers don't surface a
# PaymentReceipt), and (b) the provider router's budget enforcement in
# downstream sprints. The literal stays narrow on purpose — adding a new
# kind is a deliberate decision, not an accident.
ProviderKind = Literal["free", "x402-bazaar", "first-party"]


class SourceChunk(BaseModel):
    """A unit of evidence emitted by a provider.

    Today the pipeline owns chunking + embedding internally — providers
    return `SourceCandidate`-shaped pointers and the existing extraction
    code chunks downstream. We keep `SourceChunk` as the forward-looking
    Protocol return type so paid providers (S13+ Bazaar, vertical APIs)
    that already deliver chunked + summarized payloads can hand them
    back without a round-trip through the URL-fetch path.

    For the FreeProvider the contract is degenerate: each `SourceChunk`
    wraps a single `SourceCandidate` and the pipeline takes over from
    there. The `text` and `metadata` fields stay empty in V1.
    """

    candidate: SourceCandidate
    text: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class ProviderHealth(BaseModel):
    """5-minute health snapshot for the provider.

    `success_rate` is the fraction of `fetch()` calls that succeeded in
    the rolling window. The dispatcher (S13+) uses `available=False` to
    skip a paid call and emit a `degraded_sources` note rather than
    blowing the per-session budget on a flapping provider.
    """

    available: bool = True
    success_rate: float = 1.0
    last_error: str | None = None


@runtime_checkable
class SourceProvider(Protocol):
    """Protocol every ingestion backend conforms to.

    `name` is the stable identifier surfaced in `Provenance.provider_name`
    on each Citation. `kind` matches `ProviderKind` and drives receipt
    rendering + budget enforcement.

    `cost_estimate(query)` returns the expected per-call USD price so the
    dispatcher can bound the per-session budget upfront. Free providers
    return ~0; paid providers return their listed price.

    `health()` reports whether the provider is currently available.
    Returning `available=False` causes the dispatcher to skip the call
    and surface the provider name in `IngestionResult.degraded_sources`.

    `fetch(query)` is the actual work. The dispatcher calls it under a
    per-provider timeout; failures are caught and the provider's `name`
    is added to `degraded_sources` rather than aborting the run.
    """

    name: str
    kind: ProviderKind

    async def cost_estimate(self, query: str) -> float: ...

    async def health(self) -> ProviderHealth: ...

    async def fetch(self, query: str) -> list[SourceChunk]: ...


__all__ = [
    "ProviderHealth",
    "ProviderKind",
    "SourceChunk",
    "SourceProvider",
]
