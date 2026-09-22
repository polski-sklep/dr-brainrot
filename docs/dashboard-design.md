# DRBR dashboard — design specification

What the dashboard looks like and why. The rules here are enforced by
`dashboard/static/dashboard.css`; the templates only apply them.

This is a presentation document. It changes no figure, no query and no
terminology. Every number on screen still comes from `report.py`.

---

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
| `--bg` | `#F6F7F9` | Page ground, so white panels read as raised without a shadow. | Any panel or table. |
| `--surface` | `#FFFFFF` | Panels, tables, header. The plane data sits on. | The page itself. |
| `--surface-sunk` | `#F1F3F5` | Table headers, toolbars, totals rows, excluded rows. Signals "not the data". | Ordinary rows. |
| `--surface-hover` | `#F6F8FA` | Row hover only — a transient response to the pointer. | Any resting state. |

### Lines

| Token | Value | Role |
|---|---|---|
| `--hairline` | `#E6E8EB` | Row separators. Decorative weight: enough to guide the eye across a row, not enough to stripe the table. |
| `--border` | `#D6DAE0` | Panel and control edges. Structural. |
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

### Chart

| Token | Value | Contrast | Role |
|---|---|---|---|
| `--series-wasteful` | `#3B4250` | 10.1:1 on white | Wasteful (includes unlabelled). |
| `--series-worthwhile` | `#8A94A3` | **3.1:1** on white, **3.3:1** against the dark series | Worthwhile. |

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

- **Content width is not one number.** Data gets `1560px`; prose is capped at
  `--measure` (`78ch`). Explanatory notes stay readable while wide tables use
  the viewport. Forcing both to one width was the main cause of the old
  layout's cramped tables sitting beside empty space.
- **Spacing** is a 4px scale, `--s1` (4px) to `--s10` (40px). Sections are
  separated by `--s8`; a heading sits `--s3` from its content; table cells use
  `--s2`/`--s3`.
- **Table density** is compact: 13px text, `8px 12px` cell padding, hairline
  row rules, ~30px single-line rows. Long free text is clamped with an
  ellipsis and its full value kept in `title`, so one long video title can no
  longer stretch a row or push later columns off screen.
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
semibold tabular), qualifier below (11px muted). Figures that are excluded
from the count — tool minutes — sit on `--surface-sunk` at a smaller size, so
the panel shows them without implying they are part of the total.

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

**Bulk actions.** The bar sits directly under the table it acts on rather
than floating over it, so the selection and the action are read together. The
running count of selected rows is stated in words; the header checkbox carries
a real indeterminate state when the selection is partial.

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
