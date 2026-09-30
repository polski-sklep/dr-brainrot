"""dashboard/app.py — the DRBR dashboard. Phase 3, integration.

Implements CONTRACTS.md Part II §10 and the brief's "Dashboard" section: Flask
plus HTMX, server-rendered, bound to 127.0.0.1 only. Run it with

    .venv/bin/python -m dashboard.app

Why it is shaped like this.

1. **It renders; it does not compute.** Every figure comes from `report.py`,
   the label manager writes through `labels.set_source_label`, and the
   countdown is `evidence.diagnostic`. The helpers below only format, group
   rows for display, or add up figures `report` already computed. A number
   the dashboard derived on its own would be a second, untested definition
   of the same fact.

2. **Provenance is never flattened.** Every minute figure is printed next to
   a badge whose visual weight differs by tier, and a parsed-screenshot figure
   also carries a "lower confidence" marker beside the number itself, so it
   can never be read with an API figure's confidence.

3. **Absence is not zero.** A week whose evidence never arrived says "not
   received"; unmeasured items are a count; vault figures are counts and are
   never followed by "min".

4. **No targets, scores or streaks.** Allowance status is plain numbers, the
   stacked bar uses neutral greys rather than good/bad colours, and cost per
   keeper is ranked without any threshold.

5. **One connection per request.** Flask's threaded server handles each
   request on its own thread and sqlite3 refuses cross-thread use, so the
   connection is opened lazily inside the request and closed on teardown.
   The ingest blueprint shares it and never closes it itself.

6. **Content only (DR5.5).** DRBR measures brain-rot content. A source the
   user labels `tool` (not content) is out of every wasteful/worthwhile figure
   `report` produces, and its minutes are printed on their own so the label
   cannot hide anything. The label manager offers it as a third button.

7. **Loopback is not a trust boundary on its own.** Any page open in the
   browser can POST to 127.0.0.1. The ingest API has its token; the label
   toggle refuses requests the browser marks as cross-site
   (`Sec-Fetch-Site`), so a web page cannot relabel sources behind the user's
   back.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from flask import Flask, abort, g, redirect, render_template, request, url_for
from markupsafe import Markup, escape

import allowance
import clock
import config
import db
import drbr_types as T
import ingest_api
import insights
import jobs
import labels
import progress
import report
from clock import Clock
from config import Settings
from sources import screentime
from sources.youtube import DataApiClient

log = logging.getLogger("drbr.dashboard")

#: HTMX 2.x from jsdelivr, pinned. The SRI hash was computed from the file
#: served by jsdelivr, checked against unpkg's copy, and against jsdelivr's
#: published sha256 for the same file. Every page works without it.
HTMX_URL = "https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js"
HTMX_SRI = "sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"

#: Leaves the ingest blueprint room to answer an oversized body with its own
#: JSON message (25 MB) before Werkzeug's generic 413 fires.
MAX_CONTENT_LENGTH = ingest_api.MAX_BODY_BYTES + 1024 * 1024

GROUP_NAMES = {
    T.GROUP_PODCASTS: "Podcasts",
    T.GROUP_YOUTUBE: "YouTube",
    T.GROUP_X: "X",
    T.GROUP_IPHONE: "iPhone apps",
    T.GROUP_DESKTOP: "Desktop web",
    T.GROUP_DESKTOP_APPS: "Desktop apps",
    T.GROUP_WEB: "Web articles",
    T.GROUP_OTHER: "Other captures",
}

KIND_NAMES = {
    T.KIND_PODCAST_SHOW: "podcast show",
    T.KIND_YOUTUBE_CHANNEL: "YouTube channel",
    T.KIND_IPHONE_APP: "iPhone app",
    T.KIND_WEB_DOMAIN: "web domain",
    T.KIND_X: "X",
    T.KIND_DESKTOP_APP: "Mac app",
}

IMPORT_KIND_NAMES = {
    "youtube_history": "YouTube history",
    "screentime": "Screen Time",
    "coldturkey": "Cold Turkey",
    "vault_scan": "Vault scan",
    "qbserve": "Qbserve",
}

#: (short text, css tier) per provenance. The long text is T.PROVENANCE_TEXT.
PROVENANCE_BADGES = {
    T.PROV_API: ("API", "api"),
    T.PROV_PAGE: ("page data", "page"),
    T.PROV_SCREENSHOT: ("parsed screenshot", "screenshot"),
    T.PROV_EXPORT: ("manual export", "export"),
    T.PROV_APP_TRACKER: ("app tracker", "apptracker"),
    T.PROV_BROWSER_SYNC: ("phone sync", "browsersync"),
    T.PROV_VAULT: ("vault scan · count", "vault"),
    T.PROV_LABEL: ("your label", "label"),
}

BASIS_TEXT = {
    T.BASIS_ITEM_OVERRIDE: "item override",
    T.BASIS_SOURCE_LABEL: "source label",
    T.BASIS_DEFAULT: "unlabelled → wasteful",
}

#: Shown when a write is refused because it did not come from this dashboard.
#: A form this app served sends `Sec-Fetch-Site: same-origin`; anything else
#: (another localhost port, another site) is refused, since there is no CSRF
#: token to check instead.
WRITE_REFUSED = (
    "This change was refused because the request did not come from this dashboard. "
    "Open http://127.0.0.1:8750/ directly and try again."
)

UNKNOWN_CHANNEL_REASON = (
    "Not toggleable: this is a bucket for videos with no identifiable channel. "
    "A label here would apply to unrelated channels at once, so its minutes "
    "count as unlabelled (wasteful)."
)

#: How a tool label reads wherever a source's label is shown (DR5.5).
TOOL_TEXT = "tool → not content, not counted"

RECORDS_LIMIT = 500


# --------------------------------------------------------------------------
# View models (formatting and grouping only)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Countdown:
    state: str                 # 'not_started' | 'running' | 'ended'
    total_days: int
    weeks: int
    day: int | None = None
    days_left: int | None = None
    week_number: int | None = None
    start: str | None = None
    end: str | None = None
    reason: str | None = None


def countdown(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Countdown:
    """The header countdown, from `report.diagnostic_banner` and today's local date."""
    diag = report.diagnostic_banner(conn, clk, settings)
    total = 7 * settings.diagnostic_weeks
    today = T.local_date(clk.now(), settings.tz)
    if not diag.started:
        if settings.go_live and today < T.parse_date(settings.go_live):
            reason = (f"Measurement goes live on {settings.go_live}. The {settings.diagnostic_weeks} weeks "
                      "start at the first valid data on or after that date.")
        else:
            reason = ("The weeks start at the first valid data: a Screen Time, Cold Turkey or "
                      "YouTube import. None has arrived yet.")
        return Countdown("not_started", total, settings.diagnostic_weeks, reason=reason)
    if diag.ended:
        return Countdown("ended", total, settings.diagnostic_weeks, start=diag.start, end=diag.end)
    day = (today - T.parse_date(diag.start)).days + 1
    if day < 1:
        return Countdown("not_started", total, settings.diagnostic_weeks, start=diag.start, end=diag.end,
                         reason=f"The first valid data is dated {diag.start}; day 1 is that date.")
    return Countdown("running", total, settings.diagnostic_weeks, day=day, days_left=diag.days_remaining,
                     week_number=diag.week_number, start=diag.start, end=diag.end)


@dataclass(frozen=True)
class WeekChoice:
    week_start: str            # allowance week start, UTC ISO
    sunday: str                # local Sunday of the measurement week
    saturday: str
    is_current: bool
    prev_sunday: str | None    # None when the previous week is before the diagnostic
    next_sunday: str | None    # None when the next week is in the future
    current_sunday: str
    last_complete_sunday: str
    due: bool                  # the Monday this week's evidence is asked for has come
    due_on: str                # that Monday, so the page can name the day


def choose_week(clk: Clock, settings: Settings, raw: str | None,
                floor: str | None = None) -> WeekChoice:
    """`?week=YYYY-MM-DD` (any local day) -> that allowance week; default **this** week.

    The default is the current week, not the last complete one (DR5.7). The
    allowance section has always described the current week, so defaulting the
    rest of the page to the previous one put two different weeks side by side
    with nothing saying so — and before the diagnostic had run a full week, it
    opened on a week outside the diagnostic entirely.

    `floor` is the first local day the diagnostic covers: a week before it
    holds no diagnostic data, so a `?week=` pointing there clamps forward and
    the "previous week" link disappears rather than leading nowhere.
    """
    tz = settings.tz
    current = clock.iso(clock.week_start_for(clk.now(), tz))
    current_m = T.measurement_week_for_allowance_week(current, tz)
    last_m = T.measurement_week(current_m.sunday - timedelta(days=7))
    floor_m = T.measurement_week(T.parse_date(floor)) if floor else None
    if raw:
        try:
            chosen_m = T.measurement_week(T.parse_date(raw))
        except ValueError:
            abort(400, description=f"week must be a date YYYY-MM-DD, got {raw!r}")
    else:
        chosen_m = current_m
    if floor_m is not None and chosen_m.sunday < floor_m.sunday:
        chosen_m = floor_m
    if chosen_m.sunday > current_m.sunday:
        chosen_m = current_m
    week_start = T.allowance_week_start_for_day(chosen_m.sunday, tz)
    prev_sunday = chosen_m.sunday - timedelta(days=7)
    has_prev = floor_m is None or prev_sunday >= floor_m.sunday
    next_sunday = chosen_m.sunday + timedelta(days=7)
    return WeekChoice(
        week_start=week_start,
        sunday=chosen_m.start,
        saturday=chosen_m.end,
        is_current=chosen_m.sunday == current_m.sunday,
        prev_sunday=T.iso_date(prev_sunday) if has_prev else None,
        next_sunday=T.iso_date(next_sunday) if next_sunday <= current_m.sunday else None,
        current_sunday=current_m.start,
        last_complete_sunday=last_m.start,
        due=report.week_is_due(clk, settings, chosen_m),
        due_on=T.iso_date(report.due_on(chosen_m)),
    )


#: Flash messages survive a redirect as `?msg=` codes rather than a Flask
#: session, so the app still needs no secret key. Only these codes render.
FLASH_TEXT = {
    "synced": ("ok", "Sync finished. Everything that can be fetched automatically is up to date."),
    "sync-failed": ("warn", "Sync ran but some sources failed — see the source table below."),
    "labelled": ("ok", "Labels saved. Every week's figures were re-recorded."),
    "fixed": ("ok", "Reading corrected. The week's totals now include it."),
    "insight-written": ("ok", "Summary rewritten from the figures above."),
    "insight-fallback": ("warn", "The summary could not be generated, so the computed "
                                 "sentences are shown instead."),
}


def flash_from(args) -> list[tuple[str, str]]:
    """-> [(level, text)] for the codes in `?msg=`. Unknown codes are ignored."""
    return [FLASH_TEXT[code] for code in args.getlist("msg") if code in FLASH_TEXT]


#: Why a stored summary no longer describes the figures beside it (DR5.18).
STALE_NEWER_WEEK = "newer_week"
STALE_RELABELLED = "relabelled"


def insight_staleness(conn: sqlite3.Connection, insight, last_complete_sunday: str,
                      tz: str) -> str | None:
    """None when the summary still describes the page; otherwise why it doesn't.

    Two reasons, and they want different sentences, so they are returned
    rather than folded into one boolean:

    * `newer_week` — a later week has finished (`insight_is_stale`).
    * `relabelled` — a label that feeds the summarised figures changed after
      the paragraph was written. Labelling is the thing Jacob does most, and
      it moves every figure: forty-three sources bulk-labelled tool three
      hours after a summary was written left it saying "181 to 131" beside a
      panel saying "177 to 119", both claiming like for like, with no data
      having arrived. Staleness per week never sees that.

    Only labels that could have changed the summarised figures count: a source
    with a measurement on or before the last summarised day, any podcast show
    (its minutes come from listening, not measurements), and any per-item
    override. Labelling a source first seen this week does not stale a
    summary about last week. Regenerating is still a person's choice — an API
    call per labelling session would be wrong — so this only says so.
    """
    if insight is None or not insight.based_on_through:
        return None
    if insight_is_stale(insight, last_complete_sunday, tz):
        return STALE_NEWER_WEEK
    written, through = insight.created_at, insight.based_on_through
    relabelled = conn.execute(
        """
        SELECT 1 FROM sources s
         WHERE s.labelled_at IS NOT NULL AND s.labelled_at > ?
           AND (s.kind = ?
                OR EXISTS (SELECT 1 FROM measurements m
                            WHERE m.source_id = s.id AND m.period_start <= ?))
        UNION ALL
        SELECT 1 FROM item_labels WHERE set_at > ?
        LIMIT 1
        """,
        (written, T.KIND_PODCAST_SHOW, through, written),
    ).fetchone()
    return STALE_RELABELLED if relabelled else None


def insight_is_stale(insight, last_complete_sunday: str, tz: str) -> bool:
    """Does a completed week exist that is later than the one this summarises?

    Staleness is per **week**, not per import. Cold Turkey, Qbserve and the
    vault are re-read every morning over the last 8 days, so data newer than
    any summary exists within a day of it being written; a data-based test
    would be true six days in seven and mean nothing. A superseded *week* is
    the thing worth telling the reader about.

    The week it **describes** is the one that matters, not the week it was
    written in. `Insight.week_start` is the allowance week of *generation*: a
    summary written on Monday the 21st carries `week_start` of the week
    opening the 20th while describing the week ending the 19th. Testing that
    field fires a week late, leaving a superseded summary presented as current
    for the whole seven days the flag exists to cover. `based_on_through` is
    the last day the figures cover, already a local date, so the week it
    describes comes straight from it.
    """
    if insight is None or not insight.based_on_through:
        return False
    summarised = T.measurement_week(T.parse_date(insight.based_on_through)).sunday
    return summarised < T.parse_date(last_complete_sunday)


def split_bars(splits: list[report.WeekSplit], current_week_start: str, tz: str) -> dict:
    """Geometry for the stacked wasteful/undecided/worthwhile bars. Scale only; no judgement.

    Three bands, not two, since DR5.11. A bar drawn from `wasteful_min` alone
    would leave undecided time out of the geometry entirely — 290 of the 567
    minutes in the week of 13 Sep — so the chart that answers "is it going
    down" would be drawn from half the week, and would appear to have halved
    for a reason that is not behaviour. The undecided band keeps the bar the
    height of its week and shows the share that is still a decision.
    """
    height, bar_w, gap, top, label_h = 168, 44, 30, 26, 42
    peak = max((s.charged_min + s.worthwhile_min for s in splits), default=0.0)
    scale = (height / peak) if peak > 0 else 0.0
    bars = []
    for i, s in enumerate(splits):
        x = gap // 2 + i * (bar_w + gap)
        waste_h = s.wasteful_min * scale
        undec_h = s.unclassified_min * scale
        worth_h = s.worthwhile_min * scale
        base = top + height
        bars.append({
            "x": x,
            "cx": x + bar_w / 2,
            "waste_y": base - waste_h,
            "waste_h": waste_h,
            "undec_y": base - waste_h - undec_h,
            "undec_h": undec_h,
            "worth_y": base - waste_h - undec_h - worth_h,
            "worth_h": worth_h,
            "total_y": base - waste_h - undec_h - worth_h - 6,
            "sunday": T.iso_date(T.measurement_week_for_allowance_week(s.week_start, tz).sunday),
            "current": s.week_start == current_week_start,
            "split": s,
        })
    return {
        "bars": bars,
        "bar_w": bar_w,
        "width": max(1, len(splits)) * (bar_w + gap),
        "height": top + height + label_h,
        "base": top + height,
        "empty": peak == 0,
    }


def week_summary(splits: list[report.WeekSplit], week_start: str) -> report.WeekSplit | None:
    """The chosen week's split, picked out of the list `report.label_split` returned.

    Selection, not computation. The Overview's summary strip prints the same
    figures the bars are drawn from, so there is no second definition of a
    week's wasteful, worthwhile or tool minutes. A week outside the diagnostic
    window has no split, and the strip then prints nothing rather than zero.
    """
    for s in splits:
        if s.week_start == week_start:
            return s
    return None


def week_imports(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                 totals: list[report.SourceTotal]) -> list[report.ImportSummary]:
    """The imports the week's rows report on, so "partial" links to *its* warnings.

    The latest import of a kind is often a later week's; linking a week's
    partial status there would show the wrong warnings.
    """
    ids = sorted({t.import_id for t in totals if t.import_id is not None})
    return [d for d in (report.import_detail(conn, clk, settings, i) for i in ids) if d is not None]


def reflex_total(gated: list[report.GatedRow]) -> float:
    """Sum of the per-group wasteful ungated minutes `report` computed. Addition only."""
    return sum(r.wasteful_ungated_min for r in gated)


def label_toggleable(source: labels.Source) -> bool:
    return not (source.kind == T.KIND_YOUTUBE_CHANNEL and source.source_key == T.UNKNOWN_YOUTUBE_CHANNEL_KEY)


# --------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------

def fmt_min(value: float | None) -> str:
    if value is None:
        return "not measured"
    if value == 0:
        return "0 min"
    if value < 10:
        return f"{value:.1f} min"
    return f"{value:,.0f} min"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


#: Quantities inside generated prose: a number with a unit. Bolding these
#: gives the paragraph a spine you can skim without the model having to emit
#: markup, which it must never do — see `emphasise`.
_QUANTITY = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*-?\s*(?:minutes?|mins?\b|percent|hours?|hrs?)|\b\d[\d,]*(?:\.\d+)?\s*%",
    re.IGNORECASE,
)


def emphasise(text: str) -> Markup:
    """Bold the figures in a generated paragraph.

    The text is model output, so it is **escaped first** and the `<strong>`
    tags are added afterwards: nothing the model writes can become markup.
    Only quantities are emphasised — they are the part a reader skims for, and
    they are the one thing in the sentence this app can recognise reliably
    without guessing at the model's phrasing.
    """
    escaped = str(escape(text or ""))
    return Markup(_QUANTITY.sub(lambda m: f"<strong>{m.group(0)}</strong>", escaped))


def _parse_day(raw: str | None, name: str, errors: list[str]) -> str | None:
    if not raw:
        return None
    try:
        return T.iso_date(date.fromisoformat(raw))
    except ValueError:
        errors.append(f"{name} must be a date YYYY-MM-DD; ignored {raw!r}.")
        return None


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

def create_app(settings: Settings, clk: Clock, get_conn: Callable[[], sqlite3.Connection]) -> Flask:
    """Build the dashboard. `get_conn()` must return a *new* connection each call."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    def conn() -> sqlite3.Connection:
        if "drbr_conn" not in g:
            g.drbr_conn = get_conn()
        return g.drbr_conn

    @app.teardown_appcontext
    def _close_conn(_exc):
        c = g.pop("drbr_conn", None)
        if c is not None:
            c.close()

    app.register_blueprint(ingest_api.make_blueprint(
        get_conn=conn, clk=clk, settings=settings, api_factory=lambda: DataApiClient(settings),
    ))

    app.jinja_env.filters["min"] = fmt_min
    app.jinja_env.filters["emphasise"] = emphasise
    app.jinja_env.filters["pct"] = fmt_pct
    app.jinja_env.filters["local_dt"] = lambda iso: clock.fmt_local(iso, settings.tz) if iso else ""
    app.jinja_env.globals.update(
        GROUP_NAMES=GROUP_NAMES, KIND_NAMES=KIND_NAMES, IMPORT_KIND_NAMES=IMPORT_KIND_NAMES,
        PROVENANCE_BADGES=PROVENANCE_BADGES, PROVENANCE_TEXT=T.PROVENANCE_TEXT, BASIS_TEXT=BASIS_TEXT,
        PROV_SCREENSHOT=T.PROV_SCREENSHOT, PROV_BROWSER_SYNC=T.PROV_BROWSER_SYNC, HTMX_URL=HTMX_URL, HTMX_SRI=HTMX_SRI,
        UNKNOWN_CHANNEL_REASON=UNKNOWN_CHANNEL_REASON, WASTEFUL=T.WASTEFUL, WORTHWHILE=T.WORTHWHILE,
        TOOL=T.TOOL, TOOL_TEXT=TOOL_TEXT,
    )

    @app.context_processor
    def _header():
        return {"countdown": countdown(conn(), clk, settings)}

    @app.before_request
    def _refuse_cross_site_writes():
        if request.method == "POST" and not request.path.startswith("/api/"):
            if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                # `same-site` is refused as well as `cross-site`: another page on
                # another localhost port is same-site, and this app has no CSRF
                # token. A form served by this dashboard always sends
                # `same-origin`, so the guard only fires on a write that came
                # from somewhere else.
                abort(403, description=WRITE_REFUSED)

    @app.errorhandler(403)
    def _explain_refusal(exc):
        """Say why, rather than showing a bare 403 the user cannot act on.

        An htmx request gets a bare fragment: the label toggle swaps the
        response into one `<tr>`, and a full document there renders as
        garbage instead of telling the user anything.
        """
        detail = getattr(exc, "description", "") or WRITE_REFUSED
        if request.headers.get("HX-Request") == "true":
            return render_template("_refused_row.html", detail=detail), 403
        return render_template("refused.html", detail=detail), 403

    @app.get("/")
    def overview():
        c = conn()
        floor = report.window_start(c, clk, settings)
        week = choose_week(clk, settings, request.args.get("week"), floor)
        status = report.allowance_status(c, clk, settings)
        splits = report.label_split(c, clk, settings)
        totals = report.weekly_totals(c, clk, settings, week.week_start)
        gated = report.gated_vs_ungated(c, clk, settings, week.week_start)
        return render_template(
            "overview.html",
            week=week,
            totals=totals,
            chart=split_bars(splits, status.week_start, settings.tz),
            week_split=week_summary(splits, week.week_start),
            allowance=status,
            gated=gated,
            reflex_min=reflex_total(gated),
            keepers=report.cost_per_keeper(c, clk, settings),
            funnels=report.funnels(c, clk, settings),
            blind_spots=report.blind_spots(c, clk, settings),
            imports=report.latest_imports(c, clk, settings),
            week_imports=week_imports(c, clk, settings, totals),
            everything_in=report.everything_in(totals),
            top_sources=report.top_sources(c, clk, settings, week.week_start),
            progress=progress.progress(c, clk, settings),
            all_sources_count=len(report.top_sources(c, clk, settings, week.week_start, limit=None)),
            window_start=floor,
            needs=report.needs_attention(c, clk, settings),
            provenance_rows=report.provenance_map(c, clk, settings),
            needs_your_eye=report.needs_your_eye(c, clk, settings),
            unlabelled=labels.unlabelled_summary(c, clk, settings),
            flash=flash_from(request.args),
            # Appended, never reordered: the design pass reads this call's
            # argument order. `insight` is an insights.Insight or None.
            insight=(latest_insight := insights.latest(c, clk, settings)),
            insight_stale_reason=insight_staleness(c, latest_insight, week.last_complete_sunday,
                                                   settings.tz),
        )

    def _label_filters(args) -> tuple[str | None, str | None]:
        kind = args.get("kind") or None
        label = args.get("label") or None
        if kind is not None and kind not in T.SOURCE_KINDS:
            abort(400, description=f"unknown kind {kind!r}")
        if label is not None and label not in (*T.SOURCE_LABELS, labels.UNLABELLED):
            abort(400, description=f"unknown label filter {label!r}")
        return kind, label

    @app.get("/labels")
    def labels_page():
        kind, label = _label_filters(request.args)
        return render_template(
            "labels.html",
            sources=labels.list_sources(conn(), kind=kind, label=label),
            f_kind=kind, f_label=label, kinds=T.SOURCE_KINDS, UNLABELLED=labels.UNLABELLED,
            toggleable=label_toggleable, covered=labels.is_covered_elsewhere,
            unlabelled=labels.unlabelled_summary(conn(), clk, settings),
            flash=flash_from(request.args),
        )

    @app.post("/labels/<int:source_id>")
    def set_label(source_id: int):
        c = conn()
        source = labels.get_source(c, source_id)
        if source is None:
            abort(404, description="no such source")
        if not label_toggleable(source):
            abort(400, description=UNKNOWN_CHANNEL_REASON)
        new_label = request.form.get("label", "")
        if new_label not in T.SOURCE_LABELS:
            abort(400, description=f"label must be one of {', '.join(T.SOURCE_LABELS)}")
        source = labels.set_source_label(c, clk, source_id, new_label)
        # DECISIONS DR2.7/DR5.2: a relabel re-records each week's overage now, not at the next import.
        allowance.on_measurements_changed(c, clk, settings)
        log.info("source %d labelled %s from the dashboard", source_id, new_label)
        if request.headers.get("HX-Request") == "true":
            return render_template("_label_row.html", s=source, toggleable=label_toggleable,
                                   covered=labels.is_covered_elsewhere,
                                   f_kind=request.form.get("f_kind") or None,
                                   f_label=request.form.get("f_label") or None)
        params = {k: v for k, v in (("kind", request.form.get("f_kind")),
                                    ("label", request.form.get("f_label"))) if v}
        return redirect(url_for("labels_page", **params) + f"#source-{source_id}", code=303)

    @app.get("/labels/queue")
    def labels_queue():
        """The decisions that move the numbers most, biggest first (DR5.6).

        Its own URL and its own template: `/labels` keeps its table as the
        page's first `<tbody>`, which the dashboard tests read positionally.
        """
        c = conn()
        return render_template(
            "labels_queue.html",
            rows=labels.label_queue(c, clk, settings),
            unlabelled=labels.unlabelled_summary(c, clk, settings),
            seen_text=labels.PROVENANCE_SEEN,
            flash=flash_from(request.args),
        )

    @app.post("/labels/bulk")
    def labels_bulk():
        """Label a whole selection at once — one transaction, one recompute."""
        new_label = request.form.get("label", "")
        if new_label not in T.SOURCE_LABELS:
            abort(400, description=f"label must be one of {', '.join(T.SOURCE_LABELS)}")
        ids = [int(v) for v in request.form.getlist("source_id") if v.isdigit()]
        if not ids:
            abort(400, description="select at least one source")
        changed = labels.bulk_set_label(conn(), clk, ids, new_label, settings=settings)
        log.info("%d source(s) labelled %s in bulk from the dashboard", changed, new_label)
        return redirect(url_for("labels_queue", msg="labelled"), code=303)

    @app.post("/measurements/<int:measurement_id>/fix")
    def fix_measurement(measurement_id: int):
        """A reading OCR could not settle, settled by the person who was there."""
        raw = (request.form.get("minutes") or "").strip()
        try:
            minutes = float(raw)
        except ValueError:
            abort(400, description=f"minutes must be a number, got {raw!r}")
        try:
            screentime.correct_measurement(conn(), clk, settings, measurement_id, minutes)
        except screentime.CorrectionError as exc:
            abort(400, description=str(exc))
        log.info("measurement %d corrected by hand from the dashboard", measurement_id)
        return redirect(url_for("overview", msg="fixed"), code=303)

    @app.post("/sync")
    def sync_now():
        """Run every source DRBR can fetch by itself, now, instead of at 09:30."""
        results = jobs.run_sync(conn(), clk, settings)
        failed = [r.name for r in results.values() if not r.ok]
        for r in results.values():
            log.info("sync %s: %s", r.name, r.summary if r.ok else r.error)
        code = "sync-failed" if failed else "synced"
        return redirect(url_for("overview", msg=code), code=303)

    @app.post("/insights/refresh")
    def refresh_insight():
        """Rewrite this week's summary now, instead of waiting for tomorrow's job.

        `force=True` because `write_summary` writes at most one row a week; this
        is the one way to ask for another. It never raises: a failure stores
        itself and comes back as DRBR's own computed sentences, which is what
        the warn flash says.
        """
        result = insights.write_summary(conn(), clk, settings, force=True)
        log.info("insight refreshed from the dashboard: %s",
                 result.fallback_reason or f"written by {result.model}")
        code = "insight-written" if result.model else "insight-fallback"
        return redirect(url_for("overview", msg=code), code=303)

    @app.get("/records")
    def records_page():
        errors: list[str] = []
        group = request.args.get("group") or None
        label = request.args.get("label") or None
        if group is not None and group not in T.REPORT_GROUPS:
            errors.append(f"Unknown source group {group!r}; ignored.")
            group = None
        if label is not None and label not in T.SOURCE_LABELS:
            errors.append(f"Unknown label {label!r}; ignored.")
            label = None
        start = _parse_day(request.args.get("start"), "Start", errors)
        end = _parse_day(request.args.get("end"), "End", errors)
        c = conn()
        rows = report.records(c, clk, settings, group=group, label=label, start=start, end=end,
                              limit=RECORDS_LIMIT)
        # Records is the full detail page (DR5.12): the surfaces the Overview
        # sheds render here from exactly the values `overview()` computes, so a
        # figure cannot say one thing on one page and another on the other.
        floor = report.window_start(c, clk, settings)
        week = choose_week(clk, settings, request.args.get("week"), floor)
        totals = report.weekly_totals(c, clk, settings, week.week_start)
        status = report.allowance_status(c, clk, settings)
        splits = report.label_split(c, clk, settings)
        gated = report.gated_vs_ungated(c, clk, settings, week.week_start)
        return render_template(
            "records.html", rows=rows, errors=errors, f_group=group, f_label=label, f_start=start,
            f_end=end, groups=T.REPORT_GROUPS, limit=RECORDS_LIMIT,
            window_start=floor,
            week=week,
            totals=totals,
            allowance=status,
            chart=split_bars(splits, status.week_start, settings.tz),
            reflex_min=reflex_total(gated),
            gated=gated,
            keepers=report.cost_per_keeper(c, clk, settings),
            funnels=report.funnels(c, clk, settings),
            imports=report.latest_imports(c, clk, settings),
            week_imports=week_imports(c, clk, settings, totals),
            everything_in=report.everything_in(totals),
        ), (400 if errors else 200)

    return app


def main() -> None:  # production entry point; tests patch Flask.run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO. The YouTube key travels in a header,
    # but keep the policy the bot and jobs follow.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = config.load()
    setup = db.connect(settings)
    try:
        db.init_db(setup)
        db.migrate(setup)
    finally:
        setup.close()
    if not settings.ingest_token:
        log.warning("DRBR_INGEST_TOKEN is not set: the extension ingest API will refuse every request")
    app = create_app(settings, clock.RealClock(), lambda: db.connect(settings))
    app.run(host="127.0.0.1", port=settings.dashboard_port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
