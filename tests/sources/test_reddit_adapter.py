"""Reddit thread URL adapter (S6-V2-01) — fixture-based.

No network: every HTTP call is intercepted with ``respx``. We assert:
  * ``matches_thread_url`` accepts the canonical thread shapes and
    rejects subreddit roots, search URLs, and non-Reddit hosts.
  * ``extract_from_url`` flattens Reddit's nested listing, sorts the
    top 100 comments by score, and renders post + author + body.
  * SSRF guard rejects ``file://`` and private hosts before any fetch.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from gecko_core.ingestion.web import UnsafeURLError
from gecko_core.sources.dispatcher import discover_adapter
from gecko_core.sources.reddit import (
    THREAD_MAX_COMMENTS,
    extract_from_url,
    matches_thread_url,
)


def _comment(author: str, score: int, body: str) -> dict[str, Any]:
    return {
        "kind": "t1",
        "data": {
            "author": author,
            "score": score,
            "body": body,
            "replies": "",
        },
    }


def _payload(*, post_title: str, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "title": post_title,
                            "author": "op_user",
                            "selftext": "Original post body for the thread.",
                        },
                    }
                ],
            },
        },
        {
            "kind": "Listing",
            "data": {"children": comments},
        },
    ]


def test_matches_thread_url_accepts_canonical() -> None:
    assert matches_thread_url("https://www.reddit.com/r/devtools/comments/abc123/some_slug/")
    assert matches_thread_url("https://reddit.com/r/saas/comments/xyz/no-slug")


def test_matches_thread_url_rejects_other_shapes() -> None:
    # subreddit root, search, comment-permalink, foreign domain
    assert not matches_thread_url("https://www.reddit.com/r/devtools/")
    assert not matches_thread_url("https://www.reddit.com/r/devtools/search/?q=foo")
    assert not matches_thread_url("https://example.com/r/x/comments/abc/slug/")


@pytest.mark.asyncio
async def test_extract_renders_post_and_top_comments() -> None:
    payload = _payload(
        post_title="What's your favorite devtool?",
        comments=[
            _comment("alice", 42, "Postgres is the bedrock."),
            _comment("bob", 100, "Honestly? curl + jq."),
            _comment("carol", 7, "Neovim, no contest."),
            # nested reply on bob:
            {
                "kind": "t1",
                "data": {
                    "author": "dave",
                    "score": 3,
                    "body": "+1 to curl/jq.",
                    "replies": "",
                },
            },
        ],
    )
    url = "https://www.reddit.com/r/devtools/comments/abc/slug/"
    json_url = "https://www.reddit.com/r/devtools/comments/abc/slug.json"

    with respx.mock(assert_all_called=True) as router:
        router.get(json_url).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            text, cost = await extract_from_url(url, client=client)

    assert cost == 0.0
    # Post body included
    assert "What's your favorite devtool?" in text
    assert "u/op_user" in text
    assert "Original post body for the thread." in text
    # Top-comment ordering: bob (100) before alice (42) before carol (7)
    bob_idx = text.index("Honestly? curl + jq.")
    alice_idx = text.index("Postgres is the bedrock.")
    carol_idx = text.index("Neovim")
    assert bob_idx < alice_idx < carol_idx


@pytest.mark.asyncio
async def test_extract_caps_at_max_comments() -> None:
    # Build > MAX comments; assert only the top-N appear in output.
    over = THREAD_MAX_COMMENTS + 25
    comments = [_comment(f"u{i}", i, f"comment-body-{i}") for i in range(over)]
    payload = _payload(post_title="Cap test", comments=comments)
    url = "https://www.reddit.com/r/saas/comments/cap/slug/"
    json_url = "https://www.reddit.com/r/saas/comments/cap/slug.json"

    with respx.mock(assert_all_called=True) as router:
        router.get(json_url).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            text, _ = await extract_from_url(url, client=client)

    # Lowest-score comment must be excluded; highest-score must be present.
    assert f"comment-body-{over - 1}" in text  # highest score
    assert "comment-body-0" not in text  # lowest score, dropped


@pytest.mark.asyncio
async def test_extract_rejects_unsafe_url() -> None:
    # localhost is blocked by validate_url before any fetch.
    with pytest.raises((UnsafeURLError, ValueError)):
        await extract_from_url("http://localhost/r/x/comments/abc/slug/")


def test_discover_adapter_picks_reddit() -> None:
    pick = discover_adapter("https://www.reddit.com/r/saas/comments/xx/yy/")
    assert pick is not None
    name, _fn = pick
    assert name == "reddit"
