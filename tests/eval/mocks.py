"""Canned 5-agent transcripts for mock-mode eval runs.

Each entry maps `idea_id -> {agent_name: turn_text}` for the 5 fixed agents
(analyst, critic, architect, scoper, judge). The shapes are crafted so the
deterministic mock-rubric returns *predictable, varied* scores: kill ideas
get weak grounding + a kill verdict, ship ideas get stronger grounding +
a ship verdict, and a couple of intentionally messy entries score lower
on agent_voice so the rubric calibration is testable.

Why hand-curated text rather than templated: the rubric in mock mode keys
off heuristic markers (citation count, "kill"/"ship" verbs in the judge,
critic adversarial cues). Real strings exercise those heuristics like a
production transcript would.

NOTE: these are NOT examples of good Pro tier output — they are *test
fixtures* sized to be cheap to read in CI logs.
"""

from __future__ import annotations

from typing import TypedDict


class MockTurn(TypedDict):
    text: str
    tokens_in: int
    tokens_out: int


MockTranscript = dict[str, MockTurn]  # agent_name -> turn


# Each idea gets a 5-turn canned transcript. Token counts are picked so the
# median across the 20 ideas is ~8000 with a ±15% spread; a couple of
# ideas (`bad-crypto-social`, `good-faa-part107-checklist`) intentionally
# blow past the median so the cost_predictability axis returns <10.
MOCK_TRANSCRIPTS: dict[str, MockTranscript] = {
    # --- KILL ---
    "bad-uber-for-dogwalkers": {
        "analyst": {
            "text": (
                "Saturated marketplace. Rover ($1.7B GMV, S-1 2021) and Wag own "
                "consumer mindshare. Suburban CA TAM ~$120M (source: IBISWorld "
                "Pet Care 2024). No demand wedge visible."
            ),
            "tokens_in": 1200,
            "tokens_out": 320,
        },
        "critic": {
            "text": (
                "Why would a walker switch off Rover? Why would an owner? "
                "You're proposing a two-sided marketplace with no liquidity "
                "wedge. This is a kill."
            ),
            "tokens_in": 1400,
            "tokens_out": 240,
        },
        "architect": {
            "text": (
                "Stack is trivial (Next.js + Stripe Connect) but irrelevant — "
                "the technical bar is not the problem here."
            ),
            "tokens_in": 1300,
            "tokens_out": 180,
        },
        "scoper": {
            "text": "V1 would need 50+ walkers in one zip code. Acquisition CAC dwarfs LTV.",
            "tokens_in": 1250,
            "tokens_out": 200,
        },
        "judge": {
            "text": (
                "Verdict: KILL. Saturated, no wedge, two-sided liquidity moat "
                "impossible at indie scale. Recommendation: pivot to a "
                "narrower vertical (e.g. service-dog handler logistics) or "
                "drop entirely."
            ),
            "tokens_in": 1500,
            "tokens_out": 280,
        },
    },
    "bad-another-todo-app": {
        "analyst": {
            "text": "Things, Todoist, TickTick, Obsidian Tasks. Saturated b2c. No data on differentiation.",
            "tokens_in": 1100,
            "tokens_out": 180,
        },
        "critic": {
            "text": "What's the wedge? E2E encryption isn't one — Standard Notes, Proton already there. Kill.",
            "tokens_in": 1200,
            "tokens_out": 160,
        },
        "architect": {
            "text": "Tech is a weekend. Marketing is a decade. Skip.",
            "tokens_in": 1150,
            "tokens_out": 100,
        },
        "scoper": {
            "text": "V1 ships in a week. Distribution dies in month 2.",
            "tokens_in": 1100,
            "tokens_out": 120,
        },
        "judge": {
            "text": "Verdict: KILL. Saturated b2c with no acquisition wedge.",
            "tokens_in": 1200,
            "tokens_out": 140,
        },
    },
    "bad-tax-ai-no-lawyer": {
        "analyst": {
            "text": (
                "Per IRS Circular 230 §10.3, paid tax preparers need PTINs. "
                "Liability per filing is real (source: TurboTax 2023 10-K, "
                "$30M liability reserves)."
            ),
            "tokens_in": 1400,
            "tokens_out": 280,
        },
        "critic": {
            "text": "No CPA-in-the-loop = regulatory landmine. State vet board equivalents apply. Kill.",
            "tokens_in": 1350,
            "tokens_out": 200,
        },
        "architect": {
            "text": "RAG over Form 1040 + state schedules is doable; the moat is the human review layer you said you'd skip.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
        "scoper": {
            "text": "V1 = unlicensed practice of accountancy in 30+ states.",
            "tokens_in": 1200,
            "tokens_out": 140,
        },
        "judge": {
            "text": "Verdict: KILL. Regulatory exposure exceeds any plausible margin.",
            "tokens_in": 1350,
            "tokens_out": 180,
        },
    },
    "bad-gpt-wrapper-marketing": {
        "analyst": {
            "text": "Jasper ($125M ARR 2023), Copy.ai, ChatGPT direct. No data moat.",
            "tokens_in": 1100,
            "tokens_out": 160,
        },
        "critic": {
            "text": "What's the wedge? Pricing? Distribution? Neither is visible. Kill.",
            "tokens_in": 1150,
            "tokens_out": 140,
        },
        "architect": {
            "text": "Wrapper. Same APIs as 50 other YC batches.",
            "tokens_in": 1100,
            "tokens_out": 120,
        },
        "scoper": {
            "text": "V1 ships fast, churn at $29/mo will be 15%/mo.",
            "tokens_in": 1050,
            "tokens_out": 130,
        },
        "judge": {
            "text": "Verdict: KILL. Commodity wrapper.",
            "tokens_in": 1100,
            "tokens_out": 100,
        },
    },
    "bad-crypto-social": {
        # Intentionally over-budget to exercise cost_predictability axis.
        "analyst": {
            "text": "Lens, Farcaster exist. Steem, BitClout, Diaspora — long graveyard. Token incentives consistently fail social.",
            "tokens_in": 2400,
            "tokens_out": 480,
        },
        "critic": {
            "text": "New L1 is irrelevant to the user. Token-as-incentive is a known anti-pattern. Kill.",
            "tokens_in": 2200,
            "tokens_out": 420,
        },
        "architect": {
            "text": "Custom L1 is 18 months and $5M minimum. Existing L1 (Solana, Base) is 100x cheaper, but doesn't change the social dynamics.",
            "tokens_in": 2100,
            "tokens_out": 400,
        },
        "scoper": {
            "text": "V1 minimum: 12 months. Burn-to-launch: $2M. Kill before scoping further.",
            "tokens_in": 2000,
            "tokens_out": 360,
        },
        "judge": {
            "text": "Verdict: KILL. Repeatedly tried, repeatedly failed pattern.",
            "tokens_in": 2200,
            "tokens_out": 320,
        },
    },
    "bad-ai-resume-builder": {
        "analyst": {
            "text": "Teal, Rezi, Kickresume, Zety — saturated. ChatGPT free.",
            "tokens_in": 1100,
            "tokens_out": 140,
        },
        "critic": {
            "text": "One-time-use SaaS = brutal churn. Kill.",
            "tokens_in": 1100,
            "tokens_out": 100,
        },
        "architect": {
            "text": "Tech is trivial.",
            "tokens_in": 1000,
            "tokens_out": 80,
        },
        "scoper": {
            "text": "V1 ships in a weekend, dies in 60 days.",
            "tokens_in": 1000,
            "tokens_out": 100,
        },
        "judge": {
            "text": "Verdict: KILL. No moat, brutal churn.",
            "tokens_in": 1100,
            "tokens_out": 120,
        },
    },
    "bad-ar-shopping-glasses": {
        "analyst": {
            "text": "Vision Pro install base <500k (source: IDC Q4 2024). Retailer-API permissions impossible.",
            "tokens_in": 1300,
            "tokens_out": 200,
        },
        "critic": {
            "text": "Hardware-bound platform risk plus retailer veto. Kill.",
            "tokens_in": 1200,
            "tokens_out": 160,
        },
        "architect": {
            "text": "Vision Pro SDK is fine. Shelf-recognition CV is a 6-month problem. Distribution unsolved.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "scoper": {
            "text": "V1 needs both Vision Pro penetration AND retailer agreements. Neither is achievable indie.",
            "tokens_in": 1100,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: KILL. Platform-and-channel-bound.",
            "tokens_in": 1200,
            "tokens_out": 160,
        },
    },
    "bad-gpt-therapy": {
        "analyst": {
            "text": "Woebot pivoted away from consumer; Replika lawsuit history. FDA SaMD class II likely.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
        "critic": {
            "text": "Liability for self-harm cases is uninsurable at indie scale. Kill.",
            "tokens_in": 1300,
            "tokens_out": 180,
        },
        "architect": {
            "text": "RAG over CBT manuals is feasible. Compliance layer (HIPAA, FDA) dwarfs the product.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
        "scoper": {
            "text": "V1 = legal entity + insurance + clinical advisory board. 12+ months.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: KILL. Regulated and uninsurable.",
            "tokens_in": 1300,
            "tokens_out": 160,
        },
    },
    "bad-nft-loyalty": {
        "analyst": {
            "text": "Punch cards already work. Square Loyalty is free for merchants. NFT layer adds friction not value.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
        "critic": {
            "text": "Wallet UX is anti-feature for a $5 latte buyer. Kill.",
            "tokens_in": 1100,
            "tokens_out": 140,
        },
        "architect": {
            "text": "Solana cNFT mint is cheap, but customer-side wallet onboarding is the moat against you.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "scoper": {
            "text": "V1 ships fast; merchant adoption is the choke.",
            "tokens_in": 1100,
            "tokens_out": 140,
        },
        "judge": {
            "text": "Verdict: KILL. No merchant pain solved, customer UX hostile.",
            "tokens_in": 1200,
            "tokens_out": 160,
        },
    },
    "bad-meeting-summarizer": {
        "analyst": {
            "text": "Otter ($100M ARR), Fireflies, Zoom AI Companion bundled, MS Teams Premium bundled. Commodity.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
        "critic": {
            "text": "You're competing with bundled features at $0 marginal. Kill.",
            "tokens_in": 1100,
            "tokens_out": 140,
        },
        "architect": {
            "text": "Whisper + GPT-4o is a weekend. Distribution is the wall.",
            "tokens_in": 1100,
            "tokens_out": 160,
        },
        "scoper": {
            "text": "V1 ships fast, has no buyer.",
            "tokens_in": 1000,
            "tokens_out": 120,
        },
        "judge": {
            "text": "Verdict: KILL. Bundled feature, not a product.",
            "tokens_in": 1100,
            "tokens_out": 140,
        },
    },
    # --- SHIP ---
    "good-devbrief": {
        "analyst": {
            "text": (
                "Recruiter pain: LinkedIn doesn't surface code quality. GitHub "
                "API quota free up to 5k req/hr (source: GitHub REST docs). "
                "Dev-tools market $7B (source: Gartner 2024)."
            ),
            "tokens_in": 1300,
            "tokens_out": 280,
        },
        "critic": {
            "text": (
                "Strongest concern: why pay vs ChatGPT-and-screenshot? "
                "Counter: layout + commit synthesis is a real artifact, not a chat reply. Plausible."
            ),
            "tokens_in": 1300,
            "tokens_out": 240,
        },
        "architect": {
            "text": (
                "CLI: Click + httpx + Jinja2 HTML template. GitHub GraphQL "
                "for contribution graph. Resvg for PNG export. 2-day build."
            ),
            "tokens_in": 1250,
            "tokens_out": 260,
        },
        "scoper": {
            "text": "V1: public repos only, HTML output. V2: PDF + private repos. V3: recruiter-facing share links.",
            "tokens_in": 1200,
            "tokens_out": 220,
        },
        "judge": {
            "text": (
                "Verdict: SHIP. Narrow, dogfoodable, dev audience accessible. "
                "Pricing: free CLI + $9 hosted share links is the wedge."
            ),
            "tokens_in": 1300,
            "tokens_out": 240,
        },
    },
    "good-mcp-postgres-explainer": {
        "analyst": {
            "text": "DBA workflow real (source: PostgresWeekly survey 2024). Claude Code MCP install is one command. Narrow infra.",
            "tokens_in": 1200,
            "tokens_out": 220,
        },
        "critic": {
            "text": "Risk: pg_stat_activity is restricted on RDS without superuser. Mitigate by reading pg_stat_statements only.",
            "tokens_in": 1250,
            "tokens_out": 200,
        },
        "architect": {
            "text": "MCP server in Python, asyncpg, pgquery rewriter. 1-week build.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
        "scoper": {
            "text": "V1: read-only schema introspection. V2: write-trace. V3: cross-DB graph.",
            "tokens_in": 1100,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: SHIP. Narrow agentic infra, real DBA wedge.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
    },
    "good-vet-tele-rx": {
        "analyst": {
            "text": "Rural mixed-animal practices: ~5,400 in US (source: AVMA 2023 census). Paper forms still dominant. DEA e-prescribe mandatory in 30+ states.",
            "tokens_in": 1300,
            "tokens_out": 260,
        },
        "critic": {
            "text": "DEA EPCS certification is non-trivial — Surescripts integration is the real moat. Doable but funded.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
        "architect": {
            "text": "FastAPI + Postgres + Surescripts EPCS adapter. Two-factor for DEA. Stack is mundane; the moat is compliance.",
            "tokens_in": 1250,
            "tokens_out": 240,
        },
        "scoper": {
            "text": "V1: paper-replacement herd records. V2: DEA EPCS. V3: state-board reporting.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "judge": {
            "text": "Verdict: SHIP. Regulatory boundary is the wedge, narrow buyer, $5K ACV plausible.",
            "tokens_in": 1300,
            "tokens_out": 240,
        },
    },
    "good-repo-grounded-research": {
        "analyst": {
            "text": "Platform/staff eng teams already do RFC review manually. Repo-grounded RAG is differentiated vs ChatGPT.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "critic": {
            "text": "Risk: enterprise security review on private repos. Mitigate with self-hosted option.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
        "architect": {
            "text": "Tree-sitter for AST, pgvector for chunks, GitHub App for PR webhooks.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "scoper": {
            "text": "V1: open-source CLI, public repos. V2: GitHub App. V3: self-hosted enterprise.",
            "tokens_in": 1150,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: SHIP. Repo-grounded RAG over real artifact, narrow buyer.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
    },
    "good-clinical-trial-screener": {
        "analyst": {
            "text": "Trial coordinators screen ~30 letters/day manually (source: SCRS 2023 benchmark). ClinicalTrials.gov is structured public data.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
        "critic": {
            "text": "HIPAA exposure on PHI in referral letters. Mitigate with on-prem or BAA-covered cloud.",
            "tokens_in": 1300,
            "tokens_out": 200,
        },
        "architect": {
            "text": "Pydantic schema for eligibility criteria, RAG over CT.gov XML dump, FastAPI.",
            "tokens_in": 1250,
            "tokens_out": 220,
        },
        "scoper": {
            "text": "V1: oncology only, manual letter upload. V2: EMR webhook. V3: full screening dashboard.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "judge": {
            "text": "Verdict: SHIP. Narrow buyer, structured public data, automatable manual task.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
    },
    "good-cli-stripe-replay": {
        "analyst": {
            "text": "Payments-eng teams know this pain. Stripe CLI doesn't replay-from-prod (source: Stripe CLI docs).",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "critic": {
            "text": "Risk: webhook signature must be re-signed for replay; document this clearly.",
            "tokens_in": 1200,
            "tokens_out": 160,
        },
        "architect": {
            "text": "Click + httpx, capture sidecar via Stripe webhook endpoint. SQLite event log.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "scoper": {
            "text": "V1: CLI + SQLite. V2: idempotency-collision UI. V3: PagerDuty integration.",
            "tokens_in": 1100,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: SHIP. Specific dev pain, $9/mo to focused buyer.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
    },
    "good-mcp-airtable-typed": {
        "analyst": {
            "text": "Airtable: 450k paying customers (source: Airtable.com 2024). RevOps audience already on it.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "critic": {
            "text": "Risk: schema drift when bases change. Auto-resync on tool call.",
            "tokens_in": 1150,
            "tokens_out": 180,
        },
        "architect": {
            "text": "MCP server, Pydantic v2 codegen from Airtable metadata API, CRUD wrappers with diff preview.",
            "tokens_in": 1200,
            "tokens_out": 220,
        },
        "scoper": {
            "text": "V1: read + diff. V2: typed write. V3: scheduled sync.",
            "tokens_in": 1100,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: SHIP. Typed schema is the moat, MCP install zero-friction.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
    },
    "good-cap-table-diff": {
        "analyst": {
            "text": "Carta serves 30k+ companies (source: Carta 2024). SAFE diff is a manual lawyer task at $400/hr.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
        "critic": {
            "text": "Risk: legal-advice-not-being-given disclaimer must be tight. Mitigate with explicit non-advisory framing.",
            "tokens_in": 1250,
            "tokens_out": 200,
        },
        "architect": {
            "text": "PDF parser → JSON term sheet schema → calc engine → side-by-side React UI.",
            "tokens_in": 1250,
            "tokens_out": 220,
        },
        "scoper": {
            "text": "V1: SAFE-only diff. V2: priced-round support. V3: Carta integration.",
            "tokens_in": 1200,
            "tokens_out": 200,
        },
        "judge": {
            "text": "Verdict: SHIP. Narrow doc class, calc-engine moat, sellable to Carta.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
    },
    "good-faa-part107-checklist": {
        # Intentionally over-budget to exercise cost_predictability.
        "analyst": {
            "text": "Part 107 pilots: 380k+ active (source: FAA Aerospace Forecast 2024). Sporadic-flight pricing aligns with usage.",
            "tokens_in": 2300,
            "tokens_out": 460,
        },
        "critic": {
            "text": "Risk: NOTAM data freshness — must call FAA SWIM XML on every flight, not cache. Doable.",
            "tokens_in": 2200,
            "tokens_out": 420,
        },
        "architect": {
            "text": "Mobile app (Expo) + FAA SWIM XML parser + Stripe pay-per-flight.",
            "tokens_in": 2100,
            "tokens_out": 400,
        },
        "scoper": {
            "text": "V1: pre-flight checklist + airspace lookup. V2: NOTAM. V3: log-of-record.",
            "tokens_in": 2000,
            "tokens_out": 360,
        },
        "judge": {
            "text": "Verdict: SHIP. Regulated buyer, pay-per-use aligned, narrow.",
            "tokens_in": 2200,
            "tokens_out": 380,
        },
    },
    "good-grant-budget-rewriter": {
        "analyst": {
            "text": "Research admins manage ~50 grants/year per institution. NSF/NIH/Horizon templates change yearly (source: NSF PAPPG 2024 update).",
            "tokens_in": 1300,
            "tokens_out": 240,
        },
        "critic": {
            "text": "Risk: funder template drift. Mitigate by version-pinning templates per funder revision.",
            "tokens_in": 1300,
            "tokens_out": 200,
        },
        "architect": {
            "text": "DOCX in/out + funder-specific Pydantic schemas + diff highlight in Word.",
            "tokens_in": 1250,
            "tokens_out": 220,
        },
        "scoper": {
            "text": "V1: NSF only. V2: NIH. V3: Horizon Europe.",
            "tokens_in": 1200,
            "tokens_out": 180,
        },
        "judge": {
            "text": "Verdict: SHIP. $200/grant willingness, narrow, template moat by staying current.",
            "tokens_in": 1300,
            "tokens_out": 220,
        },
    },
}


def get_mock_transcript(idea_id: str) -> MockTranscript:
    """Return the canned 5-agent transcript for an idea_id, or a fallback."""
    if idea_id in MOCK_TRANSCRIPTS:
        return MOCK_TRANSCRIPTS[idea_id]
    # Fallback: a generic neutral transcript so unknown ideas don't crash the
    # harness. Scores predictably as a borderline-pivot.
    return {
        "analyst": {
            "text": "Generic market read; no specific source.",
            "tokens_in": 1100,
            "tokens_out": 150,
        },
        "critic": {"text": "Generic concerns.", "tokens_in": 1100, "tokens_out": 120},
        "architect": {"text": "Generic stack.", "tokens_in": 1100, "tokens_out": 120},
        "scoper": {"text": "Generic V1/V2.", "tokens_in": 1100, "tokens_out": 120},
        "judge": {"text": "Verdict: PIVOT.", "tokens_in": 1100, "tokens_out": 100},
    }
