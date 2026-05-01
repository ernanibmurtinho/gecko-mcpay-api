"""S14-TWITSH-01: one-shot real probe of the twit.sh x402 source.

Loads env via `set -a; source .env; set +a` (caller's responsibility).
Disables the Mongo cache so we measure true wire shape + spend.
Invokes `TwitshSource.fetch()` with one realistic crypto query.
Dumps the wire response (normalized + raw count) for the diagnostic doc.

Spend cap is the source's built-in $0.05 / session. Single call.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# Force-disable cache regardless of env.
os.environ["TWITSH_BYPASS_CACHE"] = "true"

from gecko_core.sources.twit_sh import TwitshSource


async def main() -> int:
    # Sanity check config without printing secrets.
    addr = os.environ.get("TWITSH_WALLET_ADDRESS", "")
    enabled = os.environ.get("TWITSH_ENABLED", "false")
    base = os.environ.get("TWITSH_BASE_URL", "https://x402.twit.sh")
    print(f"[config] enabled={enabled} addr_present={bool(addr)} base={base}")
    print(f"[config] pk_present={bool(os.environ.get('TWITSH_WALLET_PRIVATE_KEY'))}")

    src = TwitshSource()
    applies = await src.applies_to(categories={"crypto", "defi"})
    print(f"[gate] applies_to(crypto+defi)={applies}")
    if not applies:
        print("[gate] source not applicable; abort probe")
        await src.aclose()
        return 2

    idea = "AI agent x402 payments stablecoin micropayment"
    categories = {"crypto", "defi"}
    print(f"[fetch] idea={idea!r} categories={sorted(categories)}")
    t0 = time.monotonic()
    try:
        result = await src.fetch(idea=idea, categories=categories)
    except Exception as exc:
        print(f"[fetch] EXC {type(exc).__name__}: {exc}")
        await src.aclose()
        return 3
    dt = time.monotonic() - t0
    print(f"[fetch] elapsed={dt:.2f}s fired={result.fired} cost_usd={result.cost_usd}")
    if result.error:
        print(f"[fetch] error={result.error}")

    payload = result.payload or {}
    tweets = payload.get("tweets", [])
    print(f"[result] tweets_count={len(tweets)} from_cache={payload.get('from_cache')}")
    print(f"[result] spend_usd={payload.get('spend_usd')}")

    # Dump up to 3 normalized tweets for the diagnostic.
    print("[result] sample (up to 3):")
    print(json.dumps(tweets[:3], indent=2, default=str))

    await src.aclose()
    return 0 if result.fired else 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
