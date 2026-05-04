"""Judge skill.md synthesizer (S21-JUDGE-CORPUS-01).

Reads a judge's stored corpus, calls the v5.5 ``judge_synth`` prompt
with ``response_format=json_object``, validates the JSON envelope, and
formats it into a ``skill.md`` draft ready to hand to the judge for
in-person review.

Hard rules are enforced by the prompt; this module is the I/O wrapper
plus a deterministic markdown formatter (so a fixture-driven unit test
can assert the output shape without round-tripping the LLM).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from gecko_core.judges.corpus import JudgeTweet, load_corpus
from gecko_core.orchestration.pro.post_processors import _build_client
from gecko_core.orchestration.pro.prompts import _BUNDLED_VERSIONS
from gecko_core.orchestration.settings import (
    get_orchestration_settings,
    resolve_llm_config,
)
from gecko_core.routing.catalog import AgentRole, Tier, resolve_model_for_router

logger = logging.getLogger(__name__)

# LLM-hygiene Commit B — judge_synth sits at the quality tier of the
# creative_writing task profile (Claude Sonnet 4.6 by default). The corpus
# is small but the output ships to a real human judge for sign-off, so this
# is one of the few orchestration sites where the cost is justified.
_SYNTH_TIER = Tier.quality
_SYNTH_TEMPERATURE = 0.2
_GENERATOR_TAG = "gecko `bb judges synth --handle <handle>` (v0.1.6)"


def _resolve_synth_model() -> str:
    cfg = resolve_llm_config(settings=get_orchestration_settings())
    router_name = cfg.source.split(":", 1)[1] if cfg.source.startswith("router:") else "openai"
    return resolve_model_for_router(AgentRole.judge_synth, _SYNTH_TIER, router_name)


class JudgeSynthEnvelope(BaseModel):
    """JSON shape the prompt must produce. Markdown is rendered from this."""

    display_name: str = Field(..., min_length=1)
    voice_summary: str
    cares_about: list[str] = Field(default_factory=list)
    evaluation_lens: str
    hard_nos: list[str] = Field(default_factory=list)
    phrasings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    insufficient_sections: list[str] = Field(default_factory=list)


def _load_judge_synth_prompt() -> str:
    """Pull the ``judge_synth`` prompt from the bundled v5.5 JSON file."""
    path = _BUNDLED_VERSIONS["v5.5"]
    data = json.loads(path.read_text(encoding="utf-8"))
    val = data.get("judge_synth")
    if not isinstance(val, str) or not val.strip():
        raise RuntimeError(
            "judge_synth prompt missing from _default_prompts_v5_5.json — "
            "this module relies on a top-level 'judge_synth' key."
        )
    return val.strip()


def _format_corpus_for_prompt(tweets: list[JudgeTweet]) -> str:
    """One tweet per block, dated; truncated to 280 chars for prompt frugality."""
    lines: list[str] = []
    for t in tweets:
        body = t.text.strip().replace("\n", " ")
        if len(body) > 280:
            body = body[:277] + "..."
        date_part = (t.posted_at or "")[:10]
        lines.append(f"[{date_part}] (likes={t.likes} reposts={t.reposts}) {body}")
    return "\n".join(lines)


def _bullet_or_insufficient(
    items: list[str],
    section: str,
    insufficient: set[str],
) -> str:
    """Render a bullet list, or the explicit "(insufficient corpus)" line."""
    if section in insufficient or not items:
        return "(insufficient corpus — re-ingest in 7 days)"
    return "\n".join(f"- {x}" for x in items if x.strip())


def format_skill_md(
    *,
    handle: str,
    envelope: JudgeSynthEnvelope,
    n_tweets: int,
    oldest_date: str,
    newest_date: str,
    today: str | None = None,
) -> str:
    """Render the JSON envelope into the skill.md template.

    Pure function — no I/O. Takes the validated envelope plus
    provenance fields and returns a markdown string.
    """
    today_s = today or date.today().isoformat()
    insufficient = {s.strip() for s in envelope.insufficient_sections if s.strip()}
    handle_clean = handle.lstrip("@").lower()

    cares = _bullet_or_insufficient(envelope.cares_about, "cares_about", insufficient)
    nos = _bullet_or_insufficient(envelope.hard_nos, "hard_nos", insufficient)
    phrasings = _bullet_or_insufficient(envelope.phrasings, "phrasings", insufficient)
    # Open questions is mandatory: if the prompt left it empty, we still
    # render a placeholder so the operator knows to fill it in person.
    if envelope.open_questions:
        questions = "\n".join(f"- {q}" for q in envelope.open_questions if q.strip())
    else:
        questions = "- (none surfaced — confirm with judge in person)"

    eval_lens = envelope.evaluation_lens.strip()
    if "evaluation_lens" in insufficient or not eval_lens:
        eval_lens = "(insufficient corpus — re-ingest in 7 days)"

    voice_summary = envelope.voice_summary.strip()
    if "voice_summary" in insufficient or not voice_summary:
        voice_summary = "(insufficient corpus — re-ingest in 7 days)"

    return (
        f"# {envelope.display_name} — Voice Draft (DRAFT, awaiting approval)\n\n"
        f"> This is an automatically-drafted voice configuration built from "
        f"@{handle_clean}'s public X corpus, generated {today_s}. It is NOT "
        f"yet approved by the judge; do not deploy without sign-off.\n\n"
        f"**Source:** Last {n_tweets} tweets from @{handle_clean}, posted "
        f"between {oldest_date or 'unknown'} and {newest_date or 'unknown'}.\n"
        f"**Status:** DRAFT — pending review by @{handle_clean}.\n\n"
        f"## Voice summary\n{voice_summary}\n\n"
        f"## What this judge cares about\n{cares}\n\n"
        f"## Evaluation lens (when reading a builder's pitch)\n{eval_lens}\n\n"
        f'## Hard "no"s\n{nos}\n\n'
        f"## Phrasings & tone\n{phrasings}\n\n"
        f"## Open questions for the judge\n{questions}\n\n"
        f"## Provenance\n"
        f"- Tweets ingested: {n_tweets}\n"
        f"- Date range: {oldest_date or 'unknown'} — {newest_date or 'unknown'}\n"
        f"- Generated: {today_s}\n"
        f"- Generator: {_GENERATOR_TAG.replace('<handle>', handle_clean)}\n"
        f"- Reviewer (to fill): <name>, <date>, <signature>\n"
    )


async def _call_judge_synth(
    *,
    handle: str,
    corpus_text: str,
    today: str,
) -> dict[str, Any]:
    """One LLM round-trip; returns the parsed JSON dict.

    Raises on transport / JSON / empty content failures so the caller
    can decide between "skip this judge" and "fail loudly".
    """
    system = _load_judge_synth_prompt()
    user = (
        f"Today: {today}\n"
        f"Judge handle: @{handle}\n\n"
        f"Corpus (one tweet per line, newest first):\n{corpus_text}\n\n"
        "Respond with the JSON object specified in the system message."
    )

    client = _build_client()
    try:
        resp = await client.chat.completions.create(
            model=_resolve_synth_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=_SYNTH_TEMPERATURE,
        )
    finally:
        await client.close()
    content = resp.choices[0].message.content
    if not content:
        raise RuntimeError("judge_synth returned empty content")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("judge_synth JSON was not an object")
    return parsed


async def synth_skill_md(
    handle: str,
    *,
    today: str | None = None,
) -> tuple[str, int]:
    """Synthesize a skill.md draft for ``handle``. Returns ``(markdown, n_tweets)``.

    Raises ``RuntimeError`` when the corpus is empty — callers (CLI)
    should catch and skip that handle without failing the whole run.
    """
    clean = handle.lstrip("@").lower()
    tweets = await load_corpus(clean, limit=200)
    if not tweets:
        raise RuntimeError(
            f"judge corpus for @{clean} is empty; run `bb judges ingest --handle {clean}` first."
        )

    today_s = today or date.today().isoformat()
    corpus_text = _format_corpus_for_prompt(tweets)

    raw = await _call_judge_synth(handle=clean, corpus_text=corpus_text, today=today_s)
    try:
        envelope = JudgeSynthEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeError(f"judge_synth envelope validation failed: {exc}") from exc

    dates = sorted(t.posted_at[:10] for t in tweets if t.posted_at)
    oldest = dates[0] if dates else ""
    newest = dates[-1] if dates else ""

    md = format_skill_md(
        handle=clean,
        envelope=envelope,
        n_tweets=len(tweets),
        oldest_date=oldest,
        newest_date=newest,
        today=today_s,
    )
    return md, len(tweets)


__all__ = [
    "JudgeSynthEnvelope",
    "format_skill_md",
    "synth_skill_md",
]
