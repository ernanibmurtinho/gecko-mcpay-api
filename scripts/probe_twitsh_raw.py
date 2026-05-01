"""Inspect the raw twit.sh response shape (post-402 settle) — no normalization.

Captures top-level keys, the first tweet's keys, and pagination tokens so we
can wire a Gecko-side provider with confidence about the V2 API-compatible
JSON shape.
"""

from __future__ import annotations

import asyncio
import json
import sys

from gecko_core.sources.twit_sh import _build_x402_client  # type: ignore[attr-defined]


async def main() -> int:
    client = _build_x402_client()
    if client is None:
        print("client unavailable")
        return 2
    try:
        resp = await client.get("/tweets/search", params={"words": "x402 stablecoin agent"})
        print(f"status={resp.status_code}")
        body = resp.json()
        print("top_level_keys=", sorted(body.keys()) if isinstance(body, dict) else type(body))
        if isinstance(body, dict):
            for k in ("tweets", "data", "results"):
                if k in body and isinstance(body[k], list) and body[k]:
                    print(f"first_in[{k}]_keys=", sorted(body[k][0].keys()))
                    print("first_in[", k, "] sample:")
                    print(json.dumps(body[k][0], indent=2, default=str)[:2500])
                    break
            print(
                "meta:",
                json.dumps(body.get("meta") or body.get("pagination") or {}, default=str)[:400],
            )
            print(
                "includes:",
                list((body.get("includes") or {}).keys())
                if isinstance(body.get("includes"), dict)
                else None,
            )
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
