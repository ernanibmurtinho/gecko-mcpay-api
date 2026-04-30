"""GitHub URL adapter (S6-V2-02) — fixture-based.

We exercise both shapes:

  * Repo URL → README on raw.githubusercontent.com (with main/master
    branch fallback).
  * Discussion URL → REST API for body + comments.

No network. ``GITHUB_TOKEN`` is unset for these tests; the anonymous
header path is what we want to verify.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from gecko_core.sources.dispatcher import discover_adapter
from gecko_core.sources.github import extract, matches


def test_matches_accepts_repo_and_discussion() -> None:
    assert matches("https://github.com/anthropics/claude-code")
    assert matches("https://github.com/anthropics/claude-code/")
    assert matches("https://github.com/anthropics/claude-code/tree/dev")
    assert matches("https://github.com/anthropics/claude-code/discussions/42")


def test_matches_rejects_other_paths() -> None:
    # Issues, PRs, blob views are out of scope for V2.
    assert not matches("https://github.com/anthropics/claude-code/issues/1")
    assert not matches("https://github.com/anthropics/claude-code/pull/2")
    assert not matches("https://example.com/anthropics/claude-code")


@pytest.mark.asyncio
async def test_extract_repo_readme_main_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    url = "https://github.com/octocat/hello"
    main_url = "https://raw.githubusercontent.com/octocat/hello/main/README.md"

    with respx.mock(assert_all_called=False) as router:
        router.get(main_url).mock(return_value=httpx.Response(200, text="# hello\n\nA test repo."))
        async with httpx.AsyncClient() as client:
            text, cost = await extract(url, client=client)

    assert cost == 0.0
    assert "hello" in text
    assert "A test repo" in text


@pytest.mark.asyncio
async def test_extract_repo_readme_master_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When main returns 404 we should fall back to master."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    url = "https://github.com/legacy/proj"
    main_md = "https://raw.githubusercontent.com/legacy/proj/main/README.md"
    master_md = "https://raw.githubusercontent.com/legacy/proj/master/README.md"

    with respx.mock(assert_all_called=False) as router:
        router.get(main_md).mock(return_value=httpx.Response(404))
        # The candidate-name loop also probes README.MD, Readme.md, etc., on main.
        router.get(url__regex=r"https://raw\.githubusercontent\.com/legacy/proj/main/.*").mock(
            return_value=httpx.Response(404)
        )
        router.get(master_md).mock(return_value=httpx.Response(200, text="# legacy\n\nfrom master"))
        async with httpx.AsyncClient() as client:
            text, _ = await extract(url, client=client)

    assert "from master" in text


@pytest.mark.asyncio
async def test_extract_discussion_renders_body_and_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    url = "https://github.com/octo/repo/discussions/7"
    api = "https://api.github.com/repos/octo/repo/discussions/7"
    comments_api = f"{api}/comments"

    with respx.mock(assert_all_called=True) as router:
        router.get(api).mock(
            return_value=httpx.Response(
                200,
                json={
                    "title": "How do you ship?",
                    "body": "We ship every day.",
                    "user": {"login": "asker"},
                    "state": "open",
                },
            )
        )
        router.get(comments_api).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"user": {"login": "alice"}, "body": "CI gates everything."},
                    {"user": {"login": "bob"}, "body": "Trunk-based, no exceptions."},
                ],
            )
        )
        async with httpx.AsyncClient() as client:
            text, cost = await extract(url, client=client)

    assert cost == 0.0
    assert "How do you ship?" in text
    assert "We ship every day." in text
    assert "alice" in text and "CI gates everything." in text
    assert "bob" in text and "Trunk-based" in text


@pytest.mark.asyncio
async def test_extract_uses_token_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_xyz")
    url = "https://github.com/octo/repo/discussions/9"
    api = "https://api.github.com/repos/octo/repo/discussions/9"

    captured: dict[str, str] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200, json={"title": "t", "body": "b" * 50, "user": {"login": "x"}, "state": "open"}
        )

    with respx.mock(assert_all_called=False) as router:
        router.get(api).mock(side_effect=_record)
        router.get(f"{api}/comments").mock(return_value=httpx.Response(200, json=[]))
        async with httpx.AsyncClient() as client:
            await extract(url, client=client)

    assert captured["auth"] == "Bearer ghp_secret_xyz"


def test_discover_adapter_picks_github() -> None:
    pick = discover_adapter("https://github.com/owner/repo")
    assert pick is not None
    name, _fn = pick
    assert name == "github"
