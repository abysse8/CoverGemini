# Direction: from ATS auto-submission → AI-assisted spontaneous outreach

_Handoff brief for the agents picking this up. Written 2026-07-13._

## The pivot

Stop fighting applicant tracking systems. The `browser_apply.py` / `form_catalog.py` /
`scripts/discover_ats.py` path (headless form-fill behind a one-time CAPTCHA "blessing"
per ATS host) was a smart 80/20 hack, but it's an unwinnable arms race: every ATS is
different, anti-bot detection keeps tightening, and the whole surface is fragile.

Move to **AI-assisted spontaneous candidacy (candidature spontanée) via LinkedIn + email.**
This targets the *hidden* job market (roles never posted), sidesteps ATS entirely, and
fits the French market where unsolicited applications are a respected norm — especially
for alternance in embedded systems. The model to copy: people who land roles through
LinkedIn + email only win on *relevance and relationships*, not volume.

## The hard constraint (read before building anything)

**Do NOT build a LinkedIn automation bot.** Automated connects / DMs / scraping violate
LinkedIn's User Agreement and their behavioral anti-bot systems will restrict or ban the
account — catastrophic mid-search. Cold email has GDPR/CNIL limits on unsolicited B2B in
France.

The safe, effective pattern is **AI as a research-and-personalization copilot; the human
presses send.** That's not a compromise — it's *why* the LinkedIn-only approach works.
AI's leverage is: find the right people, research them fast, draft something genuinely
personal, track the pipeline. Keep the human in the loop and you're both effective and
compliant.

## The system: three layers (most already exist)

1. **Credibility surface — `abysse8.github.io` (live).** The portfolio the outreach points
   to. Now backed by an `/ideas` room of 64 real ideas mined from 3 years of history
   (spark → crystallization, honest attribution). This is the depth behind the pitch.
2. **Personalization engine — this repo (CoverAI).** Flip the I/O: input is no longer "a
   job posting to auto-submit" but "a target (company + person + a specific hook)"; output
   is no longer an ATS form but **a tailored outreach message + a tailored CV/DC**.
3. **Orchestration + lightweight CRM — the personal agentic workflow.** target list →
   research each → draft outreach → *(human sends)* → track replies → follow up.

## Concrete reuse vs. retire (in this repo)

**Reuse / repurpose:**
- The tailoring LLM call + output schema — add a short `outreach_message` / `pitch` field
  alongside the existing tailored `letter`. The `letter` field is already the precedent for
  "short tailored blurb per target." One field + one prompt line, same call.
- Context grounding (the CV/DC ingestion from `context/` / `library/`) — reuse directly to
  ground an outreach pitch in the candidate's real background.
- `coverai/agent.py` + `coverai/mcp_server.py` — the agentic spine. Expose the outreach
  loop as MCP tools (Tool Runner fits well). This is where the "multiple agents" plug in.
- `coverai/explorer.py` — retarget from job-board discovery to company/people discovery.
- `coverai/coach.py` — repurpose as an outreach-message quality critic.
- `coverai/storage.py` / `models.py` — extend into a target/lead table.
- `sms_bridge.py` — notify the human when a draft needs a send/decision.

**Retire / shelve (keep as a learning artifact, don't delete):**
- `coverai/browser_apply.py`, `coverai/form_catalog.py`, `scripts/discover_ats.py`,
  `coverai/submission_packet.py` — the ATS-submission machinery.

> Note: this repo (full `coverai/` package) and the local `OneDrive/.../CoverAI` working
> copy (a flatter `server.py`/`main.py` version) have diverged — reconcile them before
> building on top.

## What needs building

- **Target/lead data model** — company, person, title, URL, source, a specific hook,
  status, `outreach_message`, `sent_at`. The current schema has no concept of a *person/contact*.
- **Research step** — gather public info on a target to personalize (manual paste is fine
  to start; keep it human-in-the-loop, no LinkedIn scraping-at-scale).
- **Outreach output + queue** — generate the LinkedIn/email draft, route to a review/send
  step (none exists). Human sends.
- **Idea → content bridge** — the `abysse8` idea-mining produces reusable idea fragments.
  Add an on-demand generator that turns a chosen fragment into a LinkedIn post
  (hook → body → CTA). LinkedIn thought-leadership drawn from genuine ideas is the
  ToS-safe way to build presence and attract spontaneous candidacies. One idea, many
  surfaces: the /ideas card and the LinkedIn post are two consumers of the same fragment.

## First slice (suggested)

One vertical: paste a target (company + person + hook) → generate a tailored LinkedIn
message + a matching CV/DC referencing something specific about them and linking to the
portfolio → save to a lead record → human reviews and sends → mark sent, schedule a
follow-up. Ship that end-to-end before generalizing into a platform.
