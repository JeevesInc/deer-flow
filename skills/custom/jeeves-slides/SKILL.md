---
name: jeeves-slides
description: >
  Use this skill whenever you are building a PowerPoint (.pptx) presentation for Jeeves -- investor decks, diligence sessions, lender presentations, board materials, portfolio reviews, or any slide deck. This skill defines the ONLY approved Jeeves slide format. Never use any other color scheme, font, or layout when building slides for Brian. Triggers on: make a deck, build slides, create a presentation, slide deck, pptx, diligence deck, investor presentation, board deck, or any request to produce a .pptx file.
---

# Jeeves Slides -- Format Specification

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


All Jeeves presentations must match the **2026.02 Company Overview** Google Slides deck exactly.
Drive ID: `1bx_A-i0xM8IrjZVOsEN6mICPoJOCJHCd0wBoJzwv2W0`

**Do not improvise the format. Do not use Calibri, navy blue, or light backgrounds.
Do not reference the Macropay PPTX as a format guide -- it is a single-slide balance chart, not a presentation template.**

---

## Visual Identity

| Property | Value |
|----------|-------|
| Background | #08080A (near-black) -- every slide |
| Card / panel fill | #111114 (slightly lighter dark) |
| Gold accent | #C7B06C -- section labels, highlights, second headline line |
| Main headline | #F0EEEA (warm off-white), bold |
| Body text | #D4D1CC |
| Secondary / muted | #CCCCCC |
| Dim captions | #999999 |
| Footer (CONFIDENTIAL) | #6A6A6A |
| Primary font | Urbanist (headlines, body) |
| Secondary font | Arial (section labels, captions, stat card labels) |
| Slide size | 10.0" x 5.625" (widescreen 16:9) |

---

## Slide Anatomy

### Every content slide has:
1. **Section label** -- top-left, 8pt Arial, gold #C7B06C, all-caps (e.g. COMPANY OVERVIEW)
2. **Headline** -- ~26-30pt Urbanist bold, warm off-white. Key word or phrase often in gold.
3. **CONFIDENTIAL** footer -- bottom center, 7pt, #6A6A6A
4. **Dark background** -- #08080A always

### Section divider slides:
- Large ghost number (e.g. 02) in #111114 -- visually recessed, very large (~96pt)
- Two-line title: first line white, second line gold
- Section name in gold Arial 8pt at top
- Duration or descriptor in muted #CCCCCC below title

### Cover slide:
- JEEVES wordmark top-left, 11pt bold white
- Presentation type top-right, 8pt gold Arial
- Large two-line title: counterparty/topic (white) + descriptor (gold)
- Date in muted text
- Bottom row of agenda preview cards (dark #111114 panels, gold top border, gold section number)

### KPI / stat cards:
- Panel fill #111114
- Label: 7pt Arial, #999999
- Value: 20pt Urbanist bold, #F0EEEA
- Sub-label: 8pt Arial, #C7B06C

### Placeholder / content panels:
- Panel fill #111114
- Thin gold top border line
- Label: 9pt Arial bold, #C7B06C
- Placeholder note: 9pt Urbanist italic, #CCCCCC

---

## Python Build Pattern

Use python-pptx. The reference helper script is at:
C:/Jeeves/redshift-bot/deer-flow/skills/custom/jeeves-slides/scripts/build_deck_reference.py

Always copy the helper functions from that script -- set_bg, rect, txt, section_label,
headline, placeholder, stat_card, confidential -- into your build script.
Do not rewrite them from scratch.

Key setup:

    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN

    prs = Presentation()
    prs.slide_width  = Inches(10.0)
    prs.slide_height = Inches(5.625)
    blank = prs.slide_layouts[6]  # blank layout

---

## Urbanist Font Note

Urbanist is not a default system font. When the PPTX is opened on a machine without
Urbanist installed, PowerPoint will substitute a fallback font. To ensure fidelity:
- Always set the font name to 'Urbanist' in the PPTX regardless.
- Do NOT substitute Calibri or any other font. The spec is Urbanist.
- If Brian reports font substitution issues, the fix is to install Urbanist on the
  target machine, not to change the spec.

---

## Standard Slide Types

| Slide type | When to use |
|------------|-------------|
| Cover | First slide of every deck |
| Agenda | Second slide, always |
| Section divider | Before each major section |
| Content (2-col) | Two side-by-side placeholder panels |
| Content (full-width) | Single wide panel |
| KPI row + content | Stat cards on top, panels below |
| Team intro | Two-column: Jeeves left, counterparty right |
| Closing / Thank You | Last slide always |

---

## Board Deck Content Standard — MANDATORY for any Board of Directors deck

**Learned 2026-07-22 (Q3 BoD Capital Section thread, 2 rounds of correction on the same slides):**
Board decks are strategic narrative documents, not status trackers. Brian corrected a
first draft twice in the same session:
1. "this is for a board meeting, not a daily standup -- look at some of my old board
   slides for the type of content. should be storytelling."
2. Even after a revision: "still a little too tactical. strategic impact, expected
   completion is what we're focusing on."

Before drafting any BoD slide content:
- **Lead with strategic impact, not task status.** Each initiative/deal slide should
  answer "why does this matter to the company" before "what did we do this week."
  Do not list line-item tactical steps (e.g. "sent X email", "fixed Y bug in the report").
- **Always include expected completion** for any in-flight item (a date or quarter),
  not just current state.
- **Frame as a narrative arc** (problem -> action -> outcome/impact), not a bulleted
  activity log. Pull the actual structure from a real prior board deck Brian approved
  (search Drive for his most recent BoD deck, not the 2026.02 Company Overview format
  deck, which is for external/investor use) before drafting new content.
- **Surface negotiated wins explicitly** -- e.g. when a refinance or amendment produces
  a concrete concession (fee waiver, expanded facility, freed-up capacity), call it
  out as its own narrative beat, not a footnote. (Example: NB refinance -> CIM waived
  prepayment penalties + expanded MX facility to $150M, which let CIM grow in MX
  instead of tying up the corp refinance -- this is the level of insight a board slide
  needs.)
- If unsure whether a fact is "board-level" vs "operational," default to cutting it --
  ask Brian rather than including tactical detail and waiting for a correction.

---

## Metric Charts on Slides — MANDATORY

**Learned 2026-07-30 (Aug 2026 BoD slide 20, 50 turns for one chart):**

- **Confirm the metric definition against the actual columns of the source file before plotting.** Brian: "the chart should be GTV based, you have total disbursements in the recv file. **It's jp disbursements + disbursements**." The definition was already available in the file the agent itself had generated. Read the header row, do not assume the series you happened to compute is the series he means.
- **When Brian is choosing a metric or a periodicity, produce every standard cut in ONE image** — quarterly, monthly, and rolling-3-month, and both candidate numerators when the definition is ambiguous — as a small multiples panel. Serving one cut per turn turned a single chart into four sequential asks ("can you look at it monthly?", "can we do a rolling 3mo average", "does recv balance look better?"). Deliver the menu, then let him pick.
- **Back-fit any new construction to the existing bars before proposing it.** If prior-period bars on the live slide were built a specific way (e.g. average monthly flow ÷ average daily cash), reproduce those values first; a series that does not tie to the slide it is replacing is not a candidate.
- **Explain the shape, not just the trend** — a QoQ dip that traces to a one-month cash spike is the answer Brian wants, not a restatement of the numbers.
- **Never write a literal newline inside a string literal in a generated chart script.** `labels = [p.strftime("%b\n%y") ...]` written with a real line break produced `SyntaxError: unterminated string literal` and burned a turn. Build multi-line tick labels with `chr(10)`.
- **Edit an existing deck through this skill's slides helpers, not a hand-rolled Slides API call in an ad-hoc workspace script** — an improvised `presentations().get/batchUpdate` chain failed on the first run (2026-07-30). Copy the working script, do not re-derive the API.

---

## Source Discipline — MANDATORY

**Every factual claim in any Jeeves deck must be traceable to a source document or verified data pull. No exceptions.**

- **Investor names**: Only list investors explicitly confirmed in a source document provided by Brian or already in verified memory. The confirmed Jeeves investor list is: CRV, Tencent, Andreessen Horowitz, GIC, Stanford StartX. Do NOT add any other investor (including Visa) without explicit sourcing.
- **Financial figures**: Must come from a Redshift query result, a Drive document Brian has shared, or numbers Brian has explicitly provided in the conversation. Never estimate or infer.
- **Company facts** (ARR, TPV, headcount, products, geographies, partnerships): Pull from the source deck or doc Brian provides. If no source doc is provided, ask before writing.
- **If you cannot source a claim**: Leave it blank and flag it as "[Needs Confirmation]" — do not fill the gap with general knowledge or plausible-sounding content.

**Before finalizing any investor-facing deck, re-read every factual claim and confirm you can point to its source. If you cannot, flag it to Brian before delivering.**

---

## File Naming

Follow the standard convention: Deck - {Descriptor} - {YYYYMMDD}.pptx

Examples:
- Deck - NB Diligence Session - 20260420.pptx
- Deck - Fasanara Investor Presentation - 20260415.pptx
- Deck - Board Materials Q1 2026 - 20260401.pptx

---

## Upload

After building, always:
1. Save to OUTPUTS_PATH
2. Upload to the correct Drive folder (use jeeves-capital-markets skill to find it)
3. Share the Drive link

    python /mnt/skills/custom/google-drive/upload_to_drive.py "/mnt/user-data/outputs/<filename>.pptx" --folder "<TARGET_FOLDER_ID>"
