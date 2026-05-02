"""GenericBazaarAdapter — the catalog-led default.

Fetches a discovered Bazaar resource over HTTP, handles the x402 402
challenge by routing the requirement through the injected
``X402Consumer``, retries with the ``X-PAYMENT`` header, then normalizes
the response by content-type into ``BazaarChunk`` records.

This is the **whole point** of S16-BAZAAR-CONSUMER-03: any new resource
the catalog returns flows through this adapter unless its response
defeats the heuristic. Vendor shims are escape hatches, not the default.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx

from gecko_core.models import PaymentReceipt
from gecko_core.payments.bazaar_discovery import (
    BazaarResource,
    PaymentRequirements,
)
from gecko_core.payments.x402_consumer import BudgetExceededError, X402Consumer
from gecko_core.sources.bazaar.adapters import BazaarAdapter  # noqa: F401  (Protocol)
from gecko_core.sources.bazaar.types import BazaarChunk

logger = logging.getLogger(__name__)

# 8 KB per-chunk cap. Long enough for a hotel review or a small JSON
# record; short enough that the embedder never trips on a TOAST-sized
# payload (Sprint 16 Track A explicitly defended the chunk write path
# against this — see docs/diagnostics/2026-05-01-chunk-write-failures.md).
_MAX_CHUNK_BYTES = 8 * 1024


def _truncate(text: str, *, limit: int = _MAX_CHUNK_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _select_requirement(
    resource: BazaarResource, *, max_usd: Decimal
) -> PaymentRequirements | None:
    """Pick the cheapest accepts[] entry within budget, or None if none fit.

    Resources whose ``max_amount_required`` field is None (advertised
    price wasn't parseable as USD) are treated as "unknown price" and
    skipped — we don't pay something we can't bound.
    """
    eligible = [
        r
        for r in resource.accepts
        if r.max_amount_required is not None and r.max_amount_required <= max_usd
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda r: r.max_amount_required if r.max_amount_required is not None else Decimal(0),
    )


def _payment_header(receipt: PaymentReceipt) -> str:
    """Encode a receipt for the X-PAYMENT retry header.

    x402 spec puts the signed payment payload in this header. For the
    scaffold we ship the receipt's ``tx_signature`` (good enough for stub
    + replay tests). Live mode uses the facilitator's signed payload —
    web3-eng's ``X402Consumer`` conformer is responsible for surfacing
    that on the receipt; we just round-trip whatever shape it provides.
    """
    return receipt.get("tx_signature") or ""


def _chunks_from_json(
    payload: Any,
    *,
    resource: BazaarResource,
    cost_usd: Decimal,
) -> list[BazaarChunk]:
    """Normalize a parsed JSON payload into chunks.

    Heuristics, in priority order:
      1. Top-level array → one chunk per item.
      2. Top-level object whose first list-valued field is non-empty
         → one chunk per item in that array.
      3. Plain object → single chunk with the serialized object.
    """
    base_meta: dict[str, Any] = {
        "resource_url": resource.resource_url,
        "source_directory": resource.source_directory,
        "cost_usd": str(cost_usd),
    }

    def _mk(text: str) -> BazaarChunk:
        return BazaarChunk(
            text=_truncate(text),
            provider_kind=f"bazaar:{resource.resource_type}",
            cost_usd=cost_usd,
            metadata=dict(base_meta),
        )

    if isinstance(payload, list):
        return [_mk(json.dumps(item, ensure_ascii=False)) for item in payload]

    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value:
                return [_mk(json.dumps(item, ensure_ascii=False)) for item in value]
        return [_mk(json.dumps(payload, ensure_ascii=False))]

    return [_mk(str(payload))]


def _chunks_from_text(
    text: str,
    *,
    resource: BazaarResource,
    cost_usd: Decimal,
) -> list[BazaarChunk]:
    """Split text on paragraph breaks; drop empties; cap each at 8 KB."""
    base_meta: dict[str, Any] = {
        "resource_url": resource.resource_url,
        "source_directory": resource.source_directory,
        "cost_usd": str(cost_usd),
    }
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [
        BazaarChunk(
            text=_truncate(p),
            provider_kind=f"bazaar:{resource.resource_type}",
            cost_usd=cost_usd,
            metadata=dict(base_meta),
        )
        for p in paragraphs
    ]


class GenericBazaarAdapter:
    """The catalog-led default adapter."""

    name: str = "generic"

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        # Test seam: callers (or tests) may inject a pre-configured client
        # so respx / MockTransport can intercept it.
        self._client = http_client

    def applies_to(self, resource: BazaarResource) -> bool:
        # Universal fallback. Always last in priority order.
        return True

    async def fetch_and_normalize(
        self,
        resource: BazaarResource,
        x402_consumer: X402Consumer,
        *,
        max_usd: Decimal,
    ) -> list[BazaarChunk]:
        requirement = _select_requirement(resource, max_usd=max_usd)
        if requirement is None and resource.accepts:
            raise BudgetExceededError(
                f"all accepts[] entries exceed max_usd={max_usd} for {resource.resource_url}"
            )

        # S16-INTEGRATE-01 — stub-mode short-circuit. When the consumer
        # is in stub mode AND no httpx client was injected, synthesize a
        # chunk from resource metadata instead of GETting the live URL
        # (which would 402 against real USDC or fail with no auth). The
        # ``self._client is None`` predicate is the test seam: existing
        # contract tests inject a MockTransport-backed client to exercise
        # the live HTTP path; production stub-mode dispatch never does.
        # Live mode (``mode != "stub"``) keeps the original HTTP path.
        if getattr(x402_consumer, "mode", "live") == "stub" and self._client is None:
            return await self._stub_fetch_and_normalize(
                resource,
                x402_consumer,
                requirement=requirement,
                max_usd=max_usd,
            )

        receipt: PaymentReceipt | None = None
        cost_usd = Decimal("0")

        client_was_provided = self._client is not None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.get(resource.resource_url)
            if resp.status_code == 402:
                if requirement is None:
                    raise BudgetExceededError(
                        f"402 from {resource.resource_url} but no priced accepts[] entry"
                    )
                receipt = await x402_consumer.pay(requirement, max_usd=max_usd)
                cost_usd = requirement.max_amount_required or Decimal("0")
                resp = await client.get(
                    resource.resource_url,
                    headers={"X-PAYMENT": _payment_header(receipt)},
                )
            resp.raise_for_status()

            content_type = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
            if content_type == "application/json":
                try:
                    payload = resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning(
                        "bazaar.generic: invalid JSON from %s: %s",
                        resource.resource_url,
                        exc,
                    )
                    return []
                return _chunks_from_json(payload, resource=resource, cost_usd=cost_usd)
            if content_type.startswith("text/"):
                return _chunks_from_text(resp.text, resource=resource, cost_usd=cost_usd)

            logger.warning(
                "bazaar.generic: unsupported content-type %r from %s",
                content_type,
                resource.resource_url,
            )
            return []
        finally:
            if not client_was_provided:
                await client.aclose()

    async def _stub_fetch_and_normalize(
        self,
        resource: BazaarResource,
        x402_consumer: X402Consumer,
        *,
        requirement: PaymentRequirements | None,
        max_usd: Decimal,
    ) -> list[BazaarChunk]:
        """Stub-mode: settle synthetically, build one chunk from metadata.

        Exercises the full settle-then-normalize path without touching
        the live resource server. The receipt the stub consumer returns
        is recorded against the spend ledger by the provider's recording
        consumer just as in the live path; only the resource HTTP fetch
        is replaced by a deterministic synthesis from
        ``resource.metadata`` so the chunk content is non-empty.
        """
        cost_usd = Decimal("0")
        if requirement is not None:
            await x402_consumer.pay(requirement, max_usd=max_usd)
            cost_usd = requirement.max_amount_required or Decimal("0")

        meta = resource.metadata or {}
        description = str(meta.get("description") or "").strip()
        category = str(meta.get("category") or "").strip()
        domain = str(meta.get("domain") or meta.get("provider") or "").strip()
        synth_lines: list[str] = [
            f"[stub:bazaar] resource={resource.resource_url}",
        ]
        if description:
            synth_lines.append(f"description: {description}")
        if category:
            synth_lines.append(f"category: {category}")
        if domain:
            synth_lines.append(f"provider: {domain}")
        synth_lines.append(f"settled stub spend: ${cost_usd} USDC")
        synth_text = "\n".join(synth_lines)

        return [
            BazaarChunk(
                text=_truncate(synth_text),
                provider_kind=f"bazaar:{resource.resource_type}",
                cost_usd=cost_usd,
                metadata={
                    "resource_url": resource.resource_url,
                    "source_directory": resource.source_directory,
                    "cost_usd": str(cost_usd),
                    "stub": True,
                },
            )
        ]


__all__ = ["GenericBazaarAdapter"]
