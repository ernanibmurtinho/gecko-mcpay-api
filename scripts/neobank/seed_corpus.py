"""S22-N5 — seed the neobank knowledge base in Mongo Atlas.

One-shot operator script. Pulls three free discovery surfaces:

  1. **pay.sh** (`paysh_manifest` source) — well-known agent-skills
     manifest (5 meta-skills) + provider catalog (72 paid services).
     Tag ``vertical="neobank"`` only on the providers
     :func:`gecko_core.sources.paysh_manifest.filter_neobank_relevant`
     accepts; the rest go in with ``vertical="unknown"`` so the corpus
     keeps the full provider diversity.

  2. **Bazaar** (`bazaar_manifest` source) — Coinbase Agentic Wallet's
     ``/v1/services`` catalog (50 services). Same tagging logic via
     :func:`gecko_core.sources.bazaar_manifest.filter_neobank_relevant`.

  3. **Web seeds** (`web` source) — ~10 canonical neobank build
     references defined in ``scripts/neobank/web_seeds.yaml``. SSRF-guarded
     via :func:`gecko_core.ingestion.web.validate_url`. Always tagged
     ``vertical="neobank"``.

Defaults to **dry-run**. ``--apply`` is required to write to Mongo.

This script DOES NOT call live x402 endpoints. ``paysh_live`` /
``bazaar_live`` modules are operator-only and live behind a budget cap;
they are out of scope here. Per the dogfood pivot
(``project_supabase_storage_cap``), chunks land in Mongo only — no
Supabase chunk write.

Idempotency: each source URL maps to a deterministic ``source_id`` via
``uuid5(NAMESPACE, source_url)``. Re-running the script collides on the
chunks-collection ``(source_id, chunk_index)`` unique index, so a re-run
adds zero new docs (per :func:`insert_chunks_mongo`'s ordered=False
duplicate-key tolerance).

Usage::

    # Dry-run plan (default).
    uv run python scripts/neobank/seed_corpus.py

    # Live write to Mongo Atlas. Operator-only.
    uv run python scripts/neobank/seed_corpus.py --apply

    # Skip web seeds (offline plan from manifests only).
    uv run python scripts/neobank/seed_corpus.py --max-web-seeds 0
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import click
import yaml

logger = logging.getLogger("seed_corpus")

# Web fetch is best-effort: a single broken URL must not torpedo the run.
# We bound web chunks per source to avoid one giant page hijacking the
# corpus shape.
DEFAULT_WEB_SEEDS_PATH = Path(__file__).parent / "web_seeds.yaml"
DEFAULT_MAX_WEB_SEEDS = 10
WEB_CHUNK_PER_SOURCE_ESTIMATE = 3  # conservative dry-run estimate.

# Stable namespace for deterministic source_ids. Re-running the seeder
# with the same URL produces the same source_id and the
# ``(source_id, chunk_index)`` Mongo unique key skips the dup.
SEED_NAMESPACE = NAMESPACE_URL


# ---------------------------------------------------------------------------
# Plan record — what the dry-run prints and what --apply executes.
# ---------------------------------------------------------------------------


@dataclass
class SourcePlan:
    """One source row's plan: a URL, its tagging, and the chunks to write."""

    source_url: str
    source: str  # KnowledgeSource literal
    vertical: str  # Vertical literal
    chunks: list[str] = field(default_factory=list)
    # Free-form metadata — printed for operator review, not persisted as-is.
    note: str = ""

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


@dataclass
class SeedPlan:
    """Aggregate of all SourcePlans across the three sub-seeders."""

    paysh: list[SourcePlan] = field(default_factory=list)
    bazaar: list[SourcePlan] = field(default_factory=list)
    web: list[SourcePlan] = field(default_factory=list)
    # Estimated chunk count for web sources we did not fetch (dry-run).
    web_estimated: int = 0

    @property
    def total_chunks(self) -> int:
        return (
            sum(p.chunk_count for p in self.paysh)
            + sum(p.chunk_count for p in self.bazaar)
            + sum(p.chunk_count for p in self.web)
            + self.web_estimated
        )

    @property
    def total_sources(self) -> int:
        return len(self.paysh) + len(self.bazaar) + len(self.web)


# ---------------------------------------------------------------------------
# Per-source taggers — pure functions. Tested directly per
# `feedback_lighter_tests.md` (no orchestrator firing).
# ---------------------------------------------------------------------------


def tag_paysh(*, fqn: str, neobank_relevant_fqns: set[str]) -> str:
    """Return the ``vertical`` literal for a pay.sh catalog provider.

    Pure: no IO. ``neobank_relevant_fqns`` is the set of FQNs the
    ``paysh_manifest.filter_neobank_relevant`` filter accepted.
    """
    return "neobank" if fqn in neobank_relevant_fqns else "unknown"


def tag_bazaar(*, sid: str, neobank_relevant_ids: set[str]) -> str:
    """Return the ``vertical`` literal for a Bazaar catalog service."""
    return "neobank" if sid in neobank_relevant_ids else "unknown"


def tag_web(*, url: str) -> str:
    """Web seeds are hand-curated for the neobank vertical — always tagged."""
    return "neobank"


# ---------------------------------------------------------------------------
# Sub-seeder: pay.sh manifest + catalog.
# ---------------------------------------------------------------------------


async def seed_paysh() -> list[SourcePlan]:
    """Fetch the pay.sh manifest + catalog. Returns one SourcePlan per entry.

    Manifest entries (5 meta-skills) and catalog entries (72 providers)
    each become one source row carrying one chunk — the manifest pins
    these as already self-contained, so we skip token-level chunking.
    The ``filter_neobank_relevant`` slice on the catalog drives the
    ``vertical`` tag; manifest entries are always tagged ``neobank``
    (the meta-skills like ``find-provider`` are vertical-agnostic but
    structurally relevant to neobank discovery).
    """
    from gecko_core.sources import paysh_manifest as pm

    plans: list[SourcePlan] = []

    manifest_result = await pm.fetch_manifest()
    if manifest_result.fired:
        for chunk in manifest_result.payload.get("chunks", []):
            meta = chunk.get("metadata", {})
            url = meta.get("source_url") or pm.PAYSH_MANIFEST_URL
            plans.append(
                SourcePlan(
                    source_url=url,
                    source="paysh_manifest",
                    vertical="neobank",
                    chunks=[chunk["text"]],
                    note=f"manifest:{meta.get('name', '')}",
                )
            )
    else:
        logger.warning("paysh manifest fetch failed: %s", manifest_result.error)

    catalog_result = await pm.fetch_catalog()
    if catalog_result.fired:
        # Re-run the filter to derive the FQN set without re-fetching.
        # The catalog payload already has the providers normalized as
        # chunks, but `filter_neobank_relevant` operates on the raw
        # `PayshCatalogProvider` model. Cheapest path: parse the chunks'
        # citation_ids back into FQNs by walking metadata.fqn.
        neobank_fqns: set[str] = set()
        for chunk in catalog_result.payload.get("chunks", []):
            meta = chunk.get("metadata", {})
            fqn = meta.get("fqn")
            if not fqn:
                continue
            # Reconstruct a PayshCatalogProvider-shaped record from
            # chunk metadata so the filter helper stays the source of
            # truth (Pattern A — no re-implementation of the selection
            # logic).
            provider = pm.PayshCatalogProvider(
                fqn=fqn,
                description=meta.get("description", "") or chunk.get("text", ""),
                use_case=meta.get("use_case", ""),
                category=meta.get("category", ""),
            )
            if pm.filter_neobank_relevant([provider]):
                neobank_fqns.add(fqn)

        for chunk in catalog_result.payload.get("chunks", []):
            meta = chunk.get("metadata", {})
            url = meta.get("source_url") or pm.PAYSH_CATALOG_URL
            fqn = meta.get("fqn", "")
            plans.append(
                SourcePlan(
                    source_url=url,
                    source="paysh_manifest",
                    vertical=tag_paysh(fqn=fqn, neobank_relevant_fqns=neobank_fqns),
                    chunks=[chunk["text"]],
                    note=f"catalog:{fqn}",
                )
            )
    else:
        logger.warning("paysh catalog fetch failed: %s", catalog_result.error)

    return plans


# ---------------------------------------------------------------------------
# Sub-seeder: Bazaar manifest.
# ---------------------------------------------------------------------------


async def seed_bazaar() -> list[SourcePlan]:
    """Fetch the Bazaar /v1/services catalog. One SourcePlan per service."""
    from gecko_core.sources import bazaar_manifest as bm

    plans: list[SourcePlan] = []

    catalog_result = await bm.fetch_catalog()
    if not catalog_result.fired:
        logger.warning("bazaar catalog fetch failed: %s", catalog_result.error)
        return plans

    # Reconstruct the neobank-relevant id set via the canonical filter.
    neobank_ids: set[str] = set()
    for chunk in catalog_result.payload.get("chunks", []):
        meta = chunk.get("metadata", {})
        sid = meta.get("id")
        if not sid:
            continue
        service = bm.BazaarService(
            id=sid,
            description=chunk.get("text", ""),
            category=meta.get("category", ""),
        )
        if bm.filter_neobank_relevant([service]):
            neobank_ids.add(sid)

    for chunk in catalog_result.payload.get("chunks", []):
        meta = chunk.get("metadata", {})
        url = meta.get("source_url") or bm.BAZAAR_CATALOG_URL
        sid = meta.get("id", "")
        plans.append(
            SourcePlan(
                source_url=url,
                source="bazaar_manifest",
                vertical=tag_bazaar(sid=sid, neobank_relevant_ids=neobank_ids),
                chunks=[chunk["text"]],
                note=f"bazaar:{sid}",
            )
        )

    return plans


# ---------------------------------------------------------------------------
# Sub-seeder: web seeds.
# ---------------------------------------------------------------------------


def load_web_seeds(path: Path = DEFAULT_WEB_SEEDS_PATH) -> list[dict[str, str]]:
    """Parse ``web_seeds.yaml`` into ``[{url, note}, ...]``. Pure."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    seeds = raw.get("seeds") or []
    out: list[dict[str, str]] = []
    for entry in seeds:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        out.append({"url": url.strip(), "note": str(entry.get("note", "") or "")})
    return out


async def seed_web(
    *,
    seeds: list[dict[str, str]],
    dry_run: bool,
    max_seeds: int = DEFAULT_MAX_WEB_SEEDS,
) -> tuple[list[SourcePlan], int]:
    """Web fetch + chunk for each seed URL.

    Returns ``(plans, estimated_chunks_for_unfetched)``. In dry-run we do
    NOT hit the network — operators reviewing the plan don't need a
    Tavily charge or a 30-second wait. Predicted chunk count uses
    :data:`WEB_CHUNK_PER_SOURCE_ESTIMATE` as a conservative floor.
    """
    plans: list[SourcePlan] = []
    seeds = seeds[:max_seeds]
    if not seeds:
        return plans, 0

    if dry_run:
        # Pure dry-run — no network. Estimated chunks reported separately
        # so the summary doesn't lie about what's actually been chunked.
        estimated = len(seeds) * WEB_CHUNK_PER_SOURCE_ESTIMATE
        for entry in seeds:
            plans.append(
                SourcePlan(
                    source_url=entry["url"],
                    source="web",
                    vertical=tag_web(url=entry["url"]),
                    chunks=[],  # filled in --apply path
                    note=entry["note"],
                )
            )
        return plans, estimated

    # Live path — defer imports so the dry-run path doesn't pull
    # ingestion settings (which might require Voyage/OpenAI keys).
    from gecko_core.ingestion import web as web_extractor
    from gecko_core.ingestion.chunker import chunk as chunk_text
    from gecko_core.ingestion.web import UnsafeURLError

    web_extractor.bind_tavily_extract_budget()

    for entry in seeds:
        url = entry["url"]
        try:
            web_extractor.validate_url(url)
        except UnsafeURLError as exc:
            logger.warning("web seed rejected by SSRF guard url=%s err=%s", url, exc)
            continue
        try:
            text, _tavily_cost = await web_extractor.extract(url)
        except Exception as exc:
            logger.warning("web seed extract failed url=%s err=%s", url, exc.__class__.__name__)
            continue
        if not text:
            logger.info("web seed produced no content url=%s", url)
            continue
        pieces = [p for p in chunk_text(text) if p and p.strip()]
        if not pieces:
            logger.info("web seed chunked empty url=%s", url)
            continue
        plans.append(
            SourcePlan(
                source_url=url,
                source="web",
                vertical=tag_web(url=url),
                chunks=pieces,
                note=entry["note"],
            )
        )

    return plans, 0


# ---------------------------------------------------------------------------
# Mongo write path — mirrors `insert_chunks_mongo`'s contract directly.
# ---------------------------------------------------------------------------


def deterministic_source_id(source_url: str) -> UUID:
    """Stable UUID5 for re-run idempotency. Same URL → same source_id."""
    return uuid5(SEED_NAMESPACE, source_url)


async def _write_plan_to_mongo(plan: SeedPlan, *, session_id: UUID) -> dict[str, int]:
    """Embed + insert every plan's chunks into Mongo. Returns per-source totals.

    Uses the canonical ingestion embedder + Mongo writer — no parallel
    re-implementation. ``vertical="neobank"`` plans pick a default
    category of ``technical_engineering`` (the dominant category for
    SDK / API / catalog references); a richer chunk classifier pass is
    out of scope for the seed corpus and would gate on OpenAI keys.
    Operators who want classifier-driven categories should run the
    standard ingestion pipeline against these URLs instead.
    """
    from gecko_core.db.mongo_chunks import insert_chunks_mongo
    from gecko_core.ingestion.embedder import embed

    counts: dict[str, int] = {"paysh_manifest": 0, "bazaar_manifest": 0, "web": 0}

    for source_plan in (*plan.paysh, *plan.bazaar, *plan.web):
        if not source_plan.chunks:
            continue
        source_id = deterministic_source_id(source_plan.source_url)
        try:
            vectors, _tokens = await embed(source_plan.chunks)
        except Exception as exc:
            logger.warning(
                "seed.embed_failed source=%s url=%s err=%s",
                source_plan.source,
                source_plan.source_url,
                exc.__class__.__name__,
            )
            continue
        rows = list(zip(range(len(source_plan.chunks)), source_plan.chunks, vectors, strict=True))
        try:
            inserted = await insert_chunks_mongo(
                session_id,
                source_id,
                rows,
                category="technical_engineering",
                subcategory="infra",
                vertical=source_plan.vertical,  # type: ignore[arg-type]
                source=source_plan.source,  # type: ignore[arg-type]
                source_url=source_plan.source_url,
            )
        except Exception as exc:
            logger.warning(
                "seed.insert_failed source=%s url=%s err=%s",
                source_plan.source,
                source_plan.source_url,
                exc.__class__.__name__,
            )
            continue
        counts[source_plan.source] = counts.get(source_plan.source, 0) + inserted

    return counts


# ---------------------------------------------------------------------------
# Summary report — what the CLI prints. Pure on the SeedPlan input.
# ---------------------------------------------------------------------------


def build_summary(plan: SeedPlan, *, dry_run: bool) -> dict[str, Any]:
    """Render an operator-facing summary dict. Pure."""

    def _by_vertical(plans: list[SourcePlan]) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in plans:
            out[p.vertical] = out.get(p.vertical, 0) + p.chunk_count
        return out

    summary = {
        "mode": "dry-run" if dry_run else "apply",
        "totals": {
            "sources": plan.total_sources,
            "chunks_realized": plan.total_chunks - plan.web_estimated,
            "chunks_estimated_web": plan.web_estimated,
            "chunks_total_predicted": plan.total_chunks,
        },
        "by_source": {
            "paysh_manifest": {
                "sources": len(plan.paysh),
                "chunks": sum(p.chunk_count for p in plan.paysh),
                "by_vertical": _by_vertical(plan.paysh),
            },
            "bazaar_manifest": {
                "sources": len(plan.bazaar),
                "chunks": sum(p.chunk_count for p in plan.bazaar),
                "by_vertical": _by_vertical(plan.bazaar),
            },
            "web": {
                "sources": len(plan.web),
                "chunks_realized": sum(p.chunk_count for p in plan.web),
                "chunks_estimated": plan.web_estimated,
                "by_vertical": _by_vertical(plan.web) or {"neobank": 0},
            },
        },
    }
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    """Emit a human-readable summary to stdout. Side-effecting."""
    click.echo("=" * 64)
    click.echo(f"Neobank seed corpus plan ({summary['mode']})")
    click.echo("=" * 64)
    totals = summary["totals"]
    click.echo(f"  total sources:     {totals['sources']}")
    click.echo(f"  total chunks:      {totals['chunks_total_predicted']}")
    if totals["chunks_estimated_web"]:
        click.echo(f"    realized:        {totals['chunks_realized']}")
        click.echo(
            f"    estimated (web): {totals['chunks_estimated_web']} "
            f"(@ {WEB_CHUNK_PER_SOURCE_ESTIMATE}/seed; --apply for actuals)"
        )
    click.echo("")
    for source_name, block in summary["by_source"].items():
        click.echo(f"  [{source_name}]")
        click.echo(f"    sources: {block['sources']}")
        if "chunks" in block:
            click.echo(f"    chunks:  {block['chunks']}")
        else:
            click.echo(
                f"    chunks:  realized={block['chunks_realized']} "
                f"estimated={block['chunks_estimated']}"
            )
        for vert, n in sorted(block["by_vertical"].items()):
            click.echo(f"      vertical={vert}: {n}")
    click.echo("=" * 64)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


async def _run(
    *,
    apply: bool,
    max_web_seeds: int,
    web_seeds_path: Path,
) -> int:
    dry_run = not apply

    paysh_plans = await seed_paysh()
    bazaar_plans = await seed_bazaar()

    seeds = load_web_seeds(web_seeds_path)
    web_plans, web_estimated = await seed_web(
        seeds=seeds,
        dry_run=dry_run,
        max_seeds=max_web_seeds,
    )

    plan = SeedPlan(
        paysh=paysh_plans,
        bazaar=bazaar_plans,
        web=web_plans,
        web_estimated=web_estimated,
    )

    summary = build_summary(plan, dry_run=dry_run)
    print_summary(summary)

    if dry_run:
        click.echo("\nDry-run only. Pass --apply to write to Mongo Atlas.")
        return 0

    click.echo("\nWriting to Mongo Atlas...")
    session_id = uuid4()
    counts = await _write_plan_to_mongo(plan, session_id=session_id)
    click.echo(f"Wrote chunks: {counts}")
    return 0


@click.command(help=__doc__)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Write chunks to Mongo Atlas. Default is dry-run.",
)
@click.option(
    "--dry-run",
    "dry_run_flag",
    is_flag=True,
    default=False,
    help="Force dry-run (default). Mutually exclusive with --apply.",
)
@click.option(
    "--max-web-seeds",
    type=int,
    default=DEFAULT_MAX_WEB_SEEDS,
    show_default=True,
    help="Cap on the number of web seeds fetched (0 disables web seeding).",
)
@click.option(
    "--web-seeds-path",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=DEFAULT_WEB_SEEDS_PATH,
    show_default=False,
    help="Path to web_seeds.yaml.",
)
def main(
    apply: bool,
    dry_run_flag: bool,
    max_web_seeds: int,
    web_seeds_path: Path,
) -> None:
    if apply and dry_run_flag:
        raise click.UsageError("--apply and --dry-run are mutually exclusive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    rc = asyncio.run(
        _run(
            apply=apply,
            max_web_seeds=max_web_seeds,
            web_seeds_path=web_seeds_path,
        )
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
