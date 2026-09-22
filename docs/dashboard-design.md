# DRBR dashboard — design specification

What the dashboard looks like and why. The rules here are enforced by
`dashboard/static/dashboard.css`; the templates only apply them.

This is a presentation document. It changes no figure, no query and no
terminology. Every number on screen still comes from `report.py`.

---

## 0. Identity

The product is **Dr. Brainrot**; `DRBR` stays the short form in body copy and
in code. The wordmark sets `Dr.` in muted medium weight against `Brainrot` in
bold, so the joke is in the typography rather than in an illustration.

The mark is a **brain with three green specks** — the rot, made literal — in
a soft pink with a darker outline, 21px, drawn once in `base.html` and reused
as the tab icon through an inline `data:` URI. No extra request, no second
file to keep in step.

Two earlier marks were tried and rejected on evidence, both rendered at 160px,
40px and 19px side by side. A **spiral** — inward, going nowhere, and already
the motif at the centre of the POP logo this grew out of — is handsome large
and collapses to an indistinct dot at header size. A **pulse line** held at
every size and suited the clinical framing, but it said "medical instrument"
rather than "brain rot", and the name deserves the joke. The brain reads at
all three sizes; at 21px the specks register as flecks rather than as dots,
which is enough.

## 1. Visual direction

A restrained analytical tool, not a product dashboard. Neutral surfaces,
strong typography, hairline structure, and colour held back until it means
something. The page should read as a *measuring instrument*: dense, calm, and
obviously uninterested in flattering the reader.

Two constraints from the application shape everything below, and they are the
reason this does not look like a generic admin template:

1. **DRBR makes no good/bad claim.** There are no targets, scores or streaks.
   So there is no "good" colour and no "bad" colour for consumption. The
   wasteful/worthwhile split is told apart by **lightness, not hue**.
2. **Provenance is never flattened.** Every figure carries where it came from.
   Provenance is metadata and appears on nearly every row, so it is styled to
   be *quiet*; a genuine warning must always win the page.

---

## 2. Colour

Every colour has one job. A colour used for a second job is a bug.

### Surfaces

| Token | Value | Role | Not for |
|---|---|---|---|
| `--bg` | `#F5F7FA` | Page ground, so white panels read as raised without a shadow. | Any panel or table. |
| `--surface` | `#FFFFFF` | Panels, tables, header. The plane data sits on. | The page itself. |
| `--surface-sunk` | `#EFF3F8` | Table headers, toolbars, totals rows, excluded rows. Signals "not the data". | Ordinary rows. |
| `--surface-hover` | `#F2F6FC` | Row hover only — a transient response to the pointer. | Any resting state. |

### Lines

| Token | Value | Role |
|---|---|---|
| `--hairline` | `#E4E8EE` | Row separators. Decorative weight: enough to guide the eye across a row, not enough to stripe the table. |
| `--border` | `#D3D9E2` | Panel and control edges. Structural. |
| `--border-strong` | `#B4BAC3` | The line under a table head, and the total row's top rule. Marks a real boundary. |

Zebra striping is deliberately absent. Rows already carry chips and badges;
striping would be a second competing noise system on top of that.

### Text — measured, not estimated

| Token | Value | On `--surface` | On `--surface-sunk` | Role |
|---|---|---|---|---|
| `--text` | `#16181D` | **17.8:1** | 16.0:1 | Data, values, headings. |
| `--text-secondary` | `#555B66` | **6.8:1** | 6.1:1 | Column headers, labels, captions. |
| `--text-muted` | `#656B75` | **5.4:1** | **4.8:1** | Metadata, helper text, excluded rows. |

`--text-muted` was darkened from `#6E747E` because at that value it measured
4.2:1 on `--surface-sunk`, which fails AA for the 11px text that actually uses
it (the `tool` chip, excluded rows, the aside metric). All three pass now.

### Accent — navigation, links, focus, selection. Nothing else.

| Token | Value | Contrast | Role |
|---|---|---|---|
| `--accent` | `#1D5FD1` | 5.8:1 on white; white on it 5.8:1 | Links, the active nav item, the focus ring, the selected segment, the one primary button per toolbar. |
| `--accent-weak` | `#EAF0FC` | accent on it 5.1:1 | Ground for the active nav item and the selected label segment. |
| `--info-bg` / `--info-border` | `#EFF5FE` / `#C6DAF6` | body text 15:1 | Explainer panels. Says "read me", never "act". |

The neutrals carry a trace of blue rather than being pure grey, so a table or
a toolbar reads as a surface instead of as an absence of colour. Beyond that,
accent appears in exactly three non-interactive places: a 3px mark beside each
section heading, so you can find your way down a long page; a 2px edge along
the top of the summary strip, marking it as the page's headline; and the
dotted underline under an explainable term. None of them colours a figure.

Blue because it reads as *functional* rather than emotional: a reader does not
mistake it for a status. It never appears on a figure, a row, or a chart.

### Semantic — reserved, and narrow

| Token | Value | Paired bg | Contrast | Reserved for | Never for |
|---|---|---|---|---|---|
| `--warn` | `#9A6700` | `--warn-bg` `#FFF8C5` | **4.5:1** | Incomplete, partial or missing imports; unlabelled sources; the `lower confidence` marker on OCR figures. | Anything about how much was consumed. |
| `--danger` | `#B42318` | `--danger-bg` `#FEEBE9` | **5.7:1** | An import that genuinely failed, and a rejected filter value. | Emphasis, or a figure being high. |
| `--ok` | `#1A6B4C` | `--ok-bg` `#E7F3EE` | **5.7:1** | One thing only: an import arrived. | Any verdict on the user's consumption. |

Amber is the app's busiest semantic colour because uncertainty is the thing it
most often has to say. It is never used to make the page interesting.

Green is deliberately confined to pipeline health. "The Cold Turkey export
arrived" is a fact about the plumbing. Nothing on this dashboard is allowed to
tell the user they did well.

**Colour is never the only signal.** Every state carries text (`partial`,
`missing`, `lower confidence`, `unlabelled → counts as wasteful`); every
provenance tier carries a dot *shape* as well as a weight; the chart carries
direct value labels above each bar.

### Label colours

Added at the user's explicit request, after the neutral scheme below had been
built and shown to them. They name **which label you chose** — they are not a
verdict by DRBR, and the word is always printed inside the chip, so colour is
still never the only signal.

| Token | Text on its own tint | Role |
|---|---|---|
| `--lbl-wasteful-*` | `#A32F2F` on `#FDECEC` — **6.1:1** | The wasteful chip. |
| `--lbl-worthwhile-*` | `#1F7A47` on `#EAF6EE` — **4.8:1** | The worthwhile chip. |
| `--lbl-tool-*` | `#41637F` on `#EDF3FB` — **5.7:1** | The tool chip. |

Three deliberate choices inside that:

- The tool blue is a **desaturated slate**, not `--accent`. Accent means
  interactive, and a tool chip is not a link. The chip's tint and border
  separate it further.
- These tints are lighter and flatter than the semantic colours, and they
  appear **only inside a chip** — never on a figure. Red therefore still
  reads as failure where failure is a filled notice panel or bold status
  text, which is a different component at a different size.
- Honest caveat: reserving red for failure alone was cleaner. It was given up
  deliberately, and the separation now rests on component rather than hue.

### Chart

The bars follow the labels — a legend that disagreed with the chips beside it
would be worse than either scheme alone.

| Token | Value | Contrast | Role |
|---|---|---|---|
| `--series-wasteful` | `#C37C79` | 3.2:1 on white | Wasteful (includes unlabelled). |
| `--series-wasteful-edge` | `#A32F2F` | **7.0:1** | Its stroke — the wasteful chip's own red. |
| `--series-worthwhile` | `#79C399` | 2.1:1 fill, **stroked** | Worthwhile. |
| `--series-worthwhile-edge` | `#1F7A47` | **5.3:1** | Its stroke — the worthwhile chip's green. |

**A bar is a chip, drawn tall.** Each series takes its label's tint one shade
stronger as the fill, and its label's text colour as a 1.5px stroke — the
exact construction of every chip on the page. This is the third attempt and
the first that belongs: a dark saturated `#9E4440` read as a solid block in a
document made of light tints, and a mid-tone `#C37C79` was still the only
heavy object on the page. Setting the bar beside the actual chip it is meant
to match is what settled it, and is the check worth repeating.

**The cost, stated plainly.** Matching the chips leaves only ~6 luminance
points between the two fills, so a reader who cannot separate red from green
gets little from the colour here. Four things carry the meaning instead: the
segments are in a **fixed order** (wasteful below, worthwhile above) in a
*stacked* bar where position is the primary encoding; the exact per-series
figures sit in a table **beside** the chart; the legend swatches use the same
fills and strokes; and each bar carries its total as a direct label. This
trade is only available because the chart is stacked and ordered. In a grouped
chart or a pie, colour would be the only channel and these values would be the
wrong ones.

Hue-free on purpose. The obvious research answer here was the Okabe–Ito
colourblind-safe pair (`#0072B2` blue / `#E69F00` orange); it was rejected for
two specific reasons. Orange would collide with the amber already reserved for
uncertain data, and giving the two series different hues implies a judgement
the application explicitly refuses to make. A lightness ramp separates them,
survives greyscale and every form of colour-vision deficiency by construction,
and stays silent about which is better. The light series was darkened from
`#A9B1BD` (2.2:1) so the bar clears the 3:1 non-text contrast threshold
against the page.

---

### Copy

Plain and direct, in the second person. The rule is that a sentence should
survive being read once.

**Three absences, three verbs.** A figure can be missing for three different
reasons and the copy must never blur them, because keeping them apart is the
whole point of the diagnostic. A *tool* is something you decided is not
content, so DRBR **ignores** it. An *unmeasured* row is one DRBR has **no
figure** for — never zero. A *covered elsewhere* row was **already counted**
by another source and is excluded so nothing is double-counted. Using one
tidy phrase for all three reads better and says less; it was tried and
reverted. "Wasteful (including unlabelled) content minutes that
never passed through capture, lock and triage" became "Minutes of wasteful
content that never went through capture, the lock and triage — you just opened
it." The terminology the diagnostic depends on — wasteful, worthwhile, tool,
reflex, gated, keeper — is kept exactly, because renaming it would break the
link to `report.py` and to the brief. Everything around it is ordinary English,
and anything that stays jargon gets a hint.

## 3. Typography

System stack: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
Arial, sans-serif`. No webfont: this is a local tool that must render instantly
and identically offline.

| Token | Size | Used for |
|---|---|---|
| `--fs-title` | 20px | Page title (`h1`) |
| `--fs-metric` | 26px | Summary metric values |
| `--fs-section` | 16px | Section heading (`h2`) |
| `--fs-body` | 14px | Prose, nav, scope line |
| `--fs-sub` | 13px | Sub-heading (`h3`) |
| `--fs-table` | 13px | All table text |
| `--fs-meta` | 12px | Column headers, helper text, captions |
| `--fs-micro` | 11px | Chips, sub-lines, units |

A deliberately flat scale. Hierarchy is carried by **weight, colour and
spacing first**, and only then by size — which is why the largest thing on the
page is 26px, not 48px. The old 32px reflex figure is gone; that number now
sits in the summary strip at 26px, where it is comparable with the four
figures beside it instead of shouting over them.

`font-variant-numeric: tabular-nums` is applied to every numeric column, every
metric value, the countdown and every date, so digits align down a column and
a changing figure does not shift its neighbours.

---

## 4. Layout

- **Content width is not one number.** Data runs the **full width of the
  window**; prose is capped at `--measure` (`78ch`). At 2560px the Records
  table is 2510px wide while its explanatory notes are still 590px. Forcing
  both to one width was the main cause of the old layout's cramped tables
  sitting beside empty space, and a single page-wide cap was the same mistake
  one level up.
- **A box must hug its content.** Capping prose at `--measure` while leaving
  its *container* full width produces the worst of both: a readable paragraph
  inside 1,280px of empty panel. `.rule` and `.generated` are therefore capped
  to their text plus padding. Above 1100px `.generated` goes further and puts
  its provenance line in a second column, so the width is used rather than
  merely reduced.
- **Narrow content pairs up rather than sitting alone.** A two-week chart is
  150px wide and its table is 700px; stacked, each left most of a screen
  empty. They sit side by side as one unit, and the two funnel panels do the
  same instead of being spread apart by a flex grow.
- **Every full-width table names one column that absorbs the slack** — a
  title, a reason, a warning (`.col-flex`). Without it, auto table layout
  spreads surplus across all ten columns and pushes a value away from the
  value it is meant to be compared against. The absorber is chosen so that
  related columns stay adjacent: on the queue it is the last column, not
  Source, because that list is read name → minutes.
- **Spacing** is a 4px scale, `--s1` (4px) to `--s10` (40px). Sections are
  separated by `--s8`; a heading sits `--s3` from its content; table cells use
  `--s2`/`--s3`.
- **Table density** is compact: 13px text, `8px 12px` cell padding, hairline
  row rules, ~30px single-line rows. Long free text is clamped with an
  ellipsis and its full value kept in `title`.
- **The clamp goes on an inner block, not the cell.** A `max-width` on a `<td>`
  is advisory under `table-layout: auto`, so a single 101-character video
  title was widening the whole Item column and forcing the Records table into
  horizontal scroll. `.clamp` is a block inside the cell, which the column
  must then size to.
- **`.col-flex` applies only above 1500px, and the clamp cap lifts with it.**
  `width: 100%` on a cell feeds back into the table's own preferred width, so
  inside a scrolling wrapper it *inflates* the table rather than absorbing
  slack — it took Records from 1471px to 1769px at a 1280px window. Above the
  breakpoint the column absorbs and the clamp follows the column; below it the
  fixed cap is what stops the longest title widening the column at all.
  Measured: 1870px with no scroll at 1920, 1471px at 1280.
- **A column is never widened by a marker most rows do not carry.** The
  `lower confidence` chip stays beside the figure it qualifies, as `app.py`
  requires, but wraps under it rather than adding its own width to every row.
- **Radius**: controls 5px, panels 8px, chips 4px.
- **Shadow** exists only as `--shadow-pop`, for floating elements. Ordinary
  sections are framed by a border, never lifted off the page.

---

## 5. Components

**Navigation.** One row: wordmark, then three destinations. The active page is
marked with `aria-current="page"` and carries the accent on `--accent-weak` —
colour *and* weight, not colour alone.

**Diagnostic status.** A second, quieter row under the nav: 12px, secondary
colour, tabular figures. Present on every page, subordinate to the nav by
size and colour. Its one loud state is the end of the diagnostic, which takes
a full-bleed dark band — the only inverted surface in the application, because
it is the only moment the tool has something urgent to say.

**Metrics.** Related figures share **one bordered panel with internal
dividers**, not one card each. Label above (12px secondary), value (26px
semibold tabular), qualifier below (11px muted). **Every value in a strip is
the same size.** A figure excluded from the count — tool minutes — is demoted
by its ground (`--surface-sunk`) and its colour (`--text-secondary`), never by
being smaller: one number at a different size in a row of five reads as a
mistake rather than as a distinction.

**Tables.** Sentence-case 12px headers on a sunk ground with a `--border-strong`
underline. Numeric columns right-aligned and tabular. Hover tints the row.
Totals live in a `tfoot` with a top rule — a table states its own total rather
than repeating it as a headline above. Excluded rows recede to the sunk
surface and muted text. Every table is wrapped in `.table-wrap`, which scrolls
horizontally rather than letting columns collapse.

**Filters.** A single bordered toolbar per page instead of loose inline
controls, with the record or source count right-aligned inside it — visible,
but not competing with the data.

**Chips, two tiers, and they must never be confused:**

- *Provenance* (`.prov-*`) — metadata, on almost every row. No fill, no
  border, 11px: a small dot plus text. The evidence tier is encoded in the
  dot's **shape** (solid → filled grey → hollow → open) and the text weight
  (650 → 550 → 500 → italic), falling as the evidence gets less direct.
- *State* (`.state-*`, `.lowconf`, `.import-*`) — tinted fill, border, medium
  weight. Reserved for what the reader may need to act on.

A filled tint always outranks a dot. `page data` can never be mistaken for a
warning, and `lower confidence` can never be mistaken for a provenance label.

**Label control.** A segmented control, not three equal buttons. The current
label is the selected, disabled segment in accent-on-`--accent-weak`; the two
alternatives are quiet. The row's state is legible without reading the
buttons, because the Current label column carries its own chip — amber for
`unlabelled`, since that is the only row state that asks for action.

**Alerts.** Import problems are stated in the row that owns them, as
`status` + a muted explanatory sub-line, rather than as a separate banner.
The failure text stays verbatim.

**Flash.** The outcome of an action the user just took, at the top of `main`
on every page. It says what happened in words; the tint only says how much
attention it wants — `ok` on `--ok-bg`, `warn` on `--warn-bg`, with a 3px
left edge. There is no `error` level, because a failed write aborts with a
4xx rather than redirecting.

**Sync.** One action belongs to the whole application rather than to a page,
so it sits in the status row opposite the countdown: a compact secondary
button, never the accent fill. It is absent once the diagnostic has ended —
there is nothing left to fetch.

**Tabs.** Labels has two views — the queue (a worklist) and all sources (a
reference). They are tabs under one nav destination, not two destinations, so
the primary nav stays at three. Tabs are deliberately weaker than nav: no
fill, an underline, and the active tab carried by weight as well as colour.
The undecided count rides the tab as an amber badge, because an unlabelled
source is the one state in the system that asks the user to act.

**Hints.** A jargon term — *reflex*, *gated*, *provenance*, *keeper*, *the
lock* — carries its plain-English meaning in a `title`, marked by a dotted
underline so the explanation is visibly available rather than a secret. Native
`title` only: no library, no JavaScript, and it degrades to plain text. Hints
sit on column headers and on the few terms in body copy that the diagnostic
cannot do without.

**Generated prose.** The AI summary is the one thing on the dashboard that is
not a counted fact, so it is the only surface that is a paragraph rather than
a figure. It carries a chip naming what wrote it (`written by a model` /
`computed on this Mac`), and a footer stating when it was written, by which
model, and the last day its figures cover. Its heading names the week it
describes, because the strip above it defaults to the *current* week while the
summary is about the *last complete* one — two adjacent weeks with nothing
saying so is the confusion DR5.7 was written to end. When no model wrote it,
the verbatim reason is printed in the amber treatment. When a newer complete
week has finished, the panel keeps the summary and adds a line: a summary
written on Tuesday is still true about Tuesday. `insight is None` renders
nothing at all, never an empty panel.

**Folds.** Everything past the allowance — gated, cost per keeper, funnels,
imports, blind spots — is supporting evidence rather than the day's reading,
so each is a `<details>` closed by default. The summary carries the heading
and a one-line hint of what is inside, so a closed fold still tells you
whether to open it. Native `<details>`: keyboard-operable, findable by the
browser's own find, and it needs no JavaScript. Blind spots sits last, because
a list of what the tool cannot see is the right note to end a diagnostic on.

**Notice.** A refused or failed action, on its own page: a bordered panel
with a red left edge, the reason in full, and a muted second line saying that
nothing changed and why the rule exists. Red appears here and on a genuinely
failed import, nowhere else — it is the rarest colour in the system, and the
sentence carries the meaning with or without it.

**Bulk actions.** The bar sits *above* the table and sticks to the top of the
viewport. The queue runs to a hundred rows, so an action bar below the table
is unreachable from any row worth acting on. The running count of selected
rows is stated in words; the header checkbox carries a real indeterminate
state when the selection is partial.

---

## 6. Interaction and accessibility

- Focus is a two-ring `box-shadow` (white inner, accent outer) so it stays
  visible on every surface including tinted chips and the dark band.
- Disabled segments keep their selected styling; they are state, not a dead
  control.
- Text contrast is AA or better everywhere, measured rather than assumed; the
  two failures found during implementation (`--text-muted` on sunk surfaces,
  and the light chart series against the page) were fixed by darkening the
  tokens, not by waiving the rule.
- Heading order is `h1` → `h2` per section → `h3` per sub-block.
- The label counts on the Labels page recompute from the rows after an HTMX
  swap; without JavaScript the form falls back to POST + redirect and the
  server renders them afresh, so they are never stale either way.
- The queue's select-all and its selected-count are the same kind of
  enhancement: without JavaScript every checkbox and both submit buttons still
  work, and only the convenience is lost.

**One known limitation.** Long item titles and source names are clamped with
an ellipsis and the full value kept in `title`. A screen reader gets the whole
string, but a sighted keyboard or touch user has no way to reveal it without a
pointer. The alternative — wrapping — destroys the uniform row height that
makes a 400-row table scannable, so the trade was made deliberately rather
than overlooked. The full value is always available on the Records page
filters and in the row's own `title`.

---

## 7. Responsive

Desktop-primary. Two breakpoints:

- **≤1100px** — page padding tightens, clamped title columns narrow.
- **≤760px** — the header wraps to two lines, metrics go two-up, filter
  controls flex to fill the row.

Tables are never converted to cards. They keep their columns and scroll
horizontally, because in a diagnostic the ability to compare a value against
the one below it is the point of the table.

A third, quieter breakpoint at **1500px** governs whether tables absorb spare
width or simply size to their content. It is not a layout change the reader
would notice — no column moves, nothing stacks — which is why it is not listed
with the two above.
