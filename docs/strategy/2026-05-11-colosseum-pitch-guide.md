# what colosseum judges actually look for in your pitch

> Based on public-record evaluations from 34 Colosseum judges across 4+ Solana hackathon cycles. Same source corpus as the demo-video guide; here we focus on the live / interactive pitch, not the recorded demo. Verbatim quotes from public X threads.

A demo video is monologue. A pitch is dialogue. The judge can interrupt you, ask the question your slide doesn't answer, and watch how you handle it. Different signals matter.

---

## the 4 evaluation axes (pitch edition)

### 1. team / market / product / distribution — billy's lens

> "Strong pitch + GitHub team page wins initial attention."

Billy (attn.markets, prior Colosseum winner) explicitly recommends Dave Hsu's Notion framework. Every pitch he praises hits all four corners; pitches that miss one get a public reply asking which corner you forgot.

what lands:
- **team:** one sentence each on why the founders are the right ones for this market — prior shipped work, domain insight, lived experience
- **market:** named, sized, and named *small enough to be specific*. Not "the $X trillion crypto market." "Vibe traders with $50–$1,000 of conviction capital who use Phantom + Backpack" is right.
- **product:** what the user does, in their voice, in one sentence
- **distribution:** how the user finds you. Be specific — "Discord post in Superteam Brasil, an OKX skill-catalogue listing, a curl-pipe-bash from the README." Not "marketing channels."

what kills it:
- "We're a team of X engineers" with no shipped artifact
- "TAM is $X billion" with no segment
- "Word of mouth" / "social media" as a distribution answer

### 2. greenfield or iterative — adam's lens

> "Greenfield = experimental rigor + falsifiable hypotheses. Iterative = organic users + feedback loops + category-specific PMF + no airdrop-farmer dependency."

Adam (Phantom) calibrates on category awareness. He will ask, point-blank, *"is this greenfield or iterative?"* in Q&A. If you answer wrong, the rest of the pitch is downhill.

what lands:
- name your position explicitly — most teams are iterative; that's fine
- if iterative: show the analog (the obvious incumbent in the same category), name your wedge against it, prove an organic-user feedback loop. *"frames.ag is the marketplace; we're the verdict layer above it."*
- if greenfield: show the hypothesis you're testing and what evidence would falsify it
- demonstrate awareness of airdrop-farming risk if your model touches token incentives

what kills it:
- claiming greenfield when there's a visible analog
- iterative-without-feedback-loop ("we plan to acquire users")
- token incentives without a path to organic retention

### 3. force of will — qiao's lens

> "Contrarian thinking, willingness-to-be-wrong, lean-and-honest mode, AI-tool productivity step-function."

Qiao (Alliance DAO) scores founder posture. He's said publicly that he defaults to "unclear" when no founder context appears — meaning if your pitch never reveals how you *think*, he can't grade you up.

what lands:
- one contrarian claim, defended specifically. *"The marketplaces structurally can't ship the verdict layer because they'd have to pick a side on every entry in their own catalog."*
- one place you were recently wrong + what you did about it — *"We had a retrieval bug last week that hid our corpus from the panel. We caught it, shipped a fix, validated in production smoke."* This is the single highest-EV line you can deliver.
- lean-mode signals — small team, AI-tool leverage, no bloat
- one specific opinion about a competitor, said respectfully

what kills it:
- all-wins narrative
- generic agreeable answers in Q&A
- "we have it all figured out" energy
- avoiding the hard question by rephrasing the easy one

### 4. clarity bar — gui's lens

> "Who is this for? Can I answer in 5 seconds?"

Gui (Solana products, ex-Vercel) reviews landing pages publicly, but the same bar applies to pitches: the *opening sentence* must name the user and the value prop. He penalizes pitches that take 2+ minutes to "get to it."

what lands:
- title slide with the 1-liner, before any framing
- opening sentence specifies who + what
- trust signals visible early (tx hash, smoke test, GitHub stars, real users)
- "actionable next step" for the judge — try it now, here's the URL, here's the one-line install

what kills it:
- bio slide before product slide
- "let me give you some context first..."
- a wall of logos as the trust slide

---

## what judges actually ask in Q&A (from public-thread examples)

These are paraphrased from real Colosseum mentor threads. Prep an answer for each. If your pitch surfaces these proactively, you avoid the question entirely:

| Question | What the judge is testing | The bad answer | The good answer |
|---|---|---|---|
| "Is this greenfield or iterative?" | Category awareness (Adam) | "It's totally new" | "Iterative — incumbent is X. Our wedge is Y. Feedback loop is Z." |
| "Who's your user — specifically?" | Specificity (Adam, Billy) | "Crypto users" | A named persona, capital tier, and an existing tool they use today |
| "What if a marketplace ships this themselves?" | Moat awareness (Billy) | "We'll be first" | A structural reason they *can't* — conflict of interest, regulatory posture, distribution shape |
| "What's the riskiest assumption you haven't tested?" | Honesty (Qiao) | "Nothing major" | Name a specific assumption + the cheapest experiment to falsify it |
| "Walk me through the GitHub" | Tech depth (Gui, Billy) | "It's not public yet" | Open it, click into the load-bearing module, name the trade-off you made there |
| "Why won't users farm and leave?" | Organic-vs-mercenary (Adam) | "Tokenomics" | Explain the durable utility once incentives stop |
| "Why this team?" | Founder/market fit | List of prior employers | One sentence each on lived experience that maps to the user problem |

---

## the 3-5 minute pitch structure that lands

**Minute 1 — clarity + hook**
- 1-liner title slide
- specific user pain in one sentence
- the wedge in one sentence
- one verifiable artifact on screen (tx hash, smoke result, real number)

**Minute 2 — product, demonstrated**
- show it running, not slides describing it running
- one moment of dissent / one moment of "the system disagreed with us" — earns honesty points
- mention the category position explicitly (greenfield vs iterative, with the analog named)

**Minute 3 — durable surface**
- team / market / product / distribution, one sentence each
- the moat claim, defended structurally
- the force-of-will moment (recent bug caught + shipped + validated)

**Minutes 3–5 (if you have them)**
- the roadmap with horizon dates and graduation gates
- the funding ask, contextualized to a specific milestone
- Q&A — invite the hard questions

---

## what NOT to do

- **Don't open with the founder bio.** Open with the user.
- **Don't show a TAM slide before the product slide.** The judge stopped listening at "$1 trillion."
- **Don't dodge the "is this greenfield or iterative" question.** Get there yourself in the pitch.
- **Don't rehearse the willingness-to-be-wrong moment.** It has to feel genuinely admitted, not staged. If you have to fake it, find a real one to admit.
- **Don't bring a co-founder slide deck without a co-founder.** If the team is one person, own it; lean-honest mode is a feature.

---

## one-line close

The judge doesn't need you to win an argument with them. They need you to win an argument *with the cofounder you don't yet have*, on a contrarian claim, in 60 seconds, with one falsifiable test and one shipped artifact. Pitch to that bar.
