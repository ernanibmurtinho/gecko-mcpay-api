"""Minimal local reproduction of the basic-tier synthesis call.

Runs the EXACT same request shape that ``basic.py:_call_llm`` builds, twice:
  1. WITH the 0.2.10 ``extra_body`` provider pin
  2. WITHOUT it

Prints the HTTP status, OpenRouter provider header, finish_reason, content
length, and token usage for each. If one fails and the other succeeds,
we know precisely whether the ``extra_body`` is the bug.

Usage:
  uv run python scripts/debug_basic_synthesis.py [model]

The model arg defaults to ``openai/gpt-4.1-mini`` (the 0.2.10 catalog cell
for ``general_reasoning × balanced``). Pass any other slug to test it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY or API_KEY == "__unset__":
    print("ERROR: OPENROUTER_API_KEY not set or is the __unset__ sentinel.")
    sys.exit(1)

MODEL = sys.argv[1] if len(sys.argv) > 1 else "openai/gpt-4.1-mini"


def build_kwargs(model: str, with_extra_body: bool) -> dict[str, Any]:
    # Use the EXACT production system prompt and a realistic 7K-token context
    # so we exercise the same input shape that production sees.
    from gecko_core.orchestration.basic import _SYSTEM_PROMPT

    # Fake but realistically-sized context: ~30 chunks of ~200 chars each
    fake_chunks = []
    for i in range(1, 31):
        fake_chunks.append(
            f"[{i}] (source: https://example.com/article-{i}) "
            f"(chunk_index={i}, similarity=0.{600 + i})\n"
            f"This is a synthetic chunk about freelance invoicing tools, "
            f"payment reminders, and bank reconciliation. It mentions "
            f"competitors like Bonsai, Wave, FreshBooks, and QuickBooks. "
            f"The market for freelancer-focused invoice software is "
            f"estimated in the billions globally with ~70M freelancers in "
            f"the US alone. Common pain points include late payments, "
            f"manual reconciliation, and tax tracking. Chunk {i} adds "
            f"specific detail about feature {i % 5} for variety."
        )
    context = "\n\n".join(fake_chunks)
    # Use the EXACT idea the user has been testing in production
    user_msg = (
        f"Idea: Gecko: adversarial multi-agent startup validator with "
        f"X402 micropayments on Solana\n\n"
        f"Context:\n{context}\n\nReturn JSON only."
    )

    kw: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "seed": 42,
        "max_tokens": 6000,
    }
    if with_extra_body and model.startswith("openai/"):
        kw["extra_body"] = {
            "provider": {"order": ["OpenAI"], "allow_fallbacks": False},
            "transforms": [],
        }
    return kw


async def run_one(label: str, kwargs: dict[str, Any], client: AsyncOpenAI) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    print(f"model         = {kwargs['model']}")
    print(f"max_tokens    = {kwargs['max_tokens']}")
    print(f"extra_body    = {kwargs.get('extra_body', '<absent>')}")
    try:
        raw = await client.chat.completions.with_raw_response.create(**kwargs)
        resp = raw.parse()
        content = resp.choices[0].message.content
        finish = resp.choices[0].finish_reason
        provider = raw.headers.get("x-openrouter-provider", "<no header>")
        gen_id = resp.id
        usage = resp.usage
        print(f"\nHTTP status   = {raw.status_code}")
        print(f"provider      = {provider}")
        print(f"finish_reason = {finish}")
        print(f"gen_id        = {gen_id}")
        print(
            f"usage         = prompt={usage.prompt_tokens if usage else None} "
            f"completion={usage.completion_tokens if usage else None}"
        )
        print(f"content len   = {len(content) if content else 0}")
        if content:
            print(f"content[:200] = {content[:200]!r}")
            print(f"content[-200:] = {content[-200:]!r}")
        else:
            print("content       = <EMPTY/None>")
    except Exception as exc:
        print(f"\nEXCEPTION     = {type(exc).__name__}: {exc}")
        print("--- traceback ---")
        traceback.print_exc()


async def main() -> None:
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY,
        default_headers={
            "HTTP-Referer": "https://app.geckovision.tech",
            "X-Title": "Gecko-debug",
        },
    )
    await run_one(
        "TEST 1 — WITH extra_body (0.2.10 behavior)",
        build_kwargs(MODEL, with_extra_body=True),
        client,
    )
    await run_one(
        "TEST 2 — WITHOUT extra_body (0.2.9 / control)",
        build_kwargs(MODEL, with_extra_body=False),
        client,
    )


if __name__ == "__main__":
    asyncio.run(main())
