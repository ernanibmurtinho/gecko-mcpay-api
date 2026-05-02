# Verdict URL — Frontend Handoff (gecko-mcpay-app)

**Sprint:** S20-VERDICT-URL-IMPL-01 (#10)
**For:** frontend-engineer in `~/PycharmProjects/Gecko/gecko-mcpay-app`
**Source contract:** `docs/strategy/2026-05-02-verdict-url-api-contract.md` in this repo.

This is the design + integration brief. Routes / components / state management are your call — I name the must-haves.

---

## Route

`app/v/[hash]/page.tsx` — Next.js App Router, dynamic segment.

## Server Component fetch

```ts
const res = await fetch(`${process.env.GECKO_API_BASE}/v1/verdict/${params.hash}`, {
  next: { revalidate: 3600 },
});
if (res.status === 404) notFound();
if (res.status === 302) redirect(res.headers.get("Location")!);
const v = await res.json();
```

The `revalidate: 3600` cache is conservative — verdicts are immutable post-stamp, so even longer is fine. 1h gives editorial time to update the rendering layout without invalidating CDN.

## Render layout (unauth teaser, the only view shipping in #10)

1. `<h1>` — `v.idea_text`
2. **Verdict badge** — color-coded:
   - `GO` → green
   - `REFINE` → blue
   - `PIVOT` → amber
   - `KILL` → red
   Confirm exact palette with `brand.md` / `product-designer`.
3. `<p>` — `v.judge_prose_excerpt`
4. Small mono token: `<code>{v.verdict_hash_short}</code>` (muted, clipboard-copyable)
5. **CTA**: "Buy full verdict — ${v.price_usdc || '2.50'} USDC (coming soon)"
   - Disabled button, `aria-disabled="true"`
   - Goes live in #11
   - Read price from API response, NOT hardcoded — when tiered pricing lands, no frontend redeploy needed
6. **Footer**:
   - Relative `created_at` ("23 minutes ago")
   - `tier` chip ("basic" | "pro")
   - If `provider_mix_flag !== 'balanced' && provider_mix_flag !== null`: small warning chip ("⚠ thin diversity" or "⚠ single provider dominates")

## OG image

`app/v/[hash]/opengraph-image.tsx` — `next/og` rendering verdict badge + truncated idea snippet for shareable links. Same 1h revalidate.

## Crawl rules

- **Not in `sitemap.xml`** — verdicts are user-generated; no crawl.
- **`robots.txt`**: add `Disallow: /v/` to prevent indexing while still allowing direct link sharing (humans get the URL via the CLI footer or share buttons).

## Wedge alignment

This page **is** the "buy, sell, or stake on" half of the wedge sentence rendered in product. The disabled CTA copy should reinforce: "This verdict is tradeable. Full debate transcript settles on x402."

After #11 lands and the CTA is live, the page becomes the user-facing surface for the entire tradeable-judgment thesis.

## CORS / cross-origin

The teaser endpoint allows `Access-Control-Allow-Origin: *`. Server-side fetches (Server Components) ignore CORS anyway, but client-side share buttons / `navigator.clipboard` flows benefit from the wide-open policy. After #11, `?detail=full` will be restricted to `https://app.geckovision.tech` only.

## Coordination notes

- **`GECKO_API_BASE`** env var must be set on the Vercel deployment (likely `https://api.geckovision.tech` or similar).
- Test against the live `/v1/verdict/<hash>` endpoint as soon as the Python repo lands #10. Use a known good hash from your own `bb research` runs.
- For preview environments, point at a staging API or use Mock Service Worker with the teaser shape from the contract doc.

## What's NOT in #10 (deferred to #11)

- The actual paid view (full citations, PRD, transcript, advisor voices)
- x402 paywall settlement (frames.ag wallet flow, USDC transfer, settlement receipt)
- Reseller cut UI ("buy and resell for X" CTA)
- Revocation / refund window UI

#10 ships the unauthenticated teaser; #11 ships the paid view + settlement.
