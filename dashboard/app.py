"""dashboard/app.py — the DRBR dashboard. Phase 3, integration.

Implements CONTRACTS.md Part II §10 and the brief's "Dashboard" section: Flask
plus HTMX, server-rendered, bound to 127.0.0.1 only. Run it with

    .venv/bin/python -m dashboard.app

Why it is shaped like this.

1. **It renders; it does not compute.** Every figure comes from `report.py`,
   the label manager writes through `labels.set_source_label`, and the
   countdown is `evidence.diagnostic`. The helpers below only format, group
   rows for display, or read stored rows back verbatim (the latest import per
   kind). A number the dashboard derived on its own would be a second,
   untested definition of the same fact.

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

6. **Loopback is not a trust boundary on its own.** Any page open in the
   browser can POST to 127.0.0.1. The ingest API has its token; the label
   toggle refuses requests the browser marks as cross-site
   (`Sec-Fetch-Site`), so a web page cannot relabel sources behind the user's
   back.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from flask import Flask, abort, g, redirect, render_template, request, url_for

import allowance
import clock
import config
import db
import drbr_types as T
import ingest_api
import labels
import report
from clock import Clock
from config import Settings
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
    T.GROUP_WEB: "Web articles",
    T.GROUP_OTHER: "Other captures",
}

KIND_NAMES = {
    T.KIND_PODCAST_SHOW: "podcast show",
    T.KIND_YOUTUBE_CHANNEL: "YouTube channel",
    T.KIND_IPHONE_APP: "iPhone app",
    T.KIND_WEB_DOMAIN: "web domain",
    T.KIND_X: "X",
}

IMPORT_KIND_NAMES = {
    "youtube_history": "YouTube history",
    "screentime": "Screen Time",
    "coldturkey": "Cold Turkey",
    "vault_scan": "Vault scan",
}

#: (short text, css tier) per provenance. The long text is T.PROVENANCE_TEXT.
PROVENANCE_BADGES = {
    T.PROV_API: ("API", "api"),
    T.PROV_PAGE: ("page data", "page"),
    T.PROV_SCREENSHOT: ("parsed screenshot", "screenshot"),
    T.PROV_EXPORT: ("manual export", "export"),
    T.PROV_VAULT: ("vault scan · count", "vault"),
    T.PROV_LABEL: ("your label", "label"),
}

BASIS_TEXT = {
    T.BASIS_ITEM_OVERRIDE: "item override",
    T.BASIS_SOURCE_LABEL: "source label",
    T.BASIS_DEFAULT: "unlabelled → wasteful",
}

UNKNOWN_CHANNEL_REASON = (
    "Not toggleable: this is a bucket for videos with no identifiable channel. "
    "A label here would apply to unrelated channels at once, so its minutes "
    "count as unlabelled (wasteful)."
)

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
    prev_sunday: str
    next_sunday: str | None    # None when the next week is in the future
    current_sunday: str
    last_complete_sunday: str


def choose_week(clk: Clock, settings: Settings, raw: str | None) -> WeekChoice:
    """`?week=YYYY-MM-DD` (any local day) -> that allowance week; default the last complete one."""
    tz = settings.tz
    current = clock.iso(clock.week_start_for(clk.now(), tz))
    current_m = T.measurement_week_for_allowance_week(current, tz)
    last_m = T.measurement_week(current_m.sunday - timedelta(days=7))
    if raw:
        try:
            chosen_m = T.measurement_week(T.parse_date(raw))
        except ValueError:
            abort(400, description=f"week must be a date YYYY-MM-DD, got {raw!r}")
    else:
        chosen_m = last_m
    week_start = T.allowance_week_start_for_day(chosen_m.sunday, tz)
    next_sunday = chosen_m.sunday + timedelta(days=7)
    return WeekChoice(
        week_start=week_start,
        sunday=chosen_m.start,
        saturday=chosen_m.end,
        is_current=chosen_m.sunday == current_m.sunday,
        prev_sunday=T.iso_date(chosen_m.sunday - timedelta(days=7)),
        next_sunday=T.iso_date(next_sunday) if next_sunday <= current_m.sunday else None,
        current_sunday=current_m.start,
        last_complete_sunday=last_m.start,
    )


def covered_reasons(conn, clk, settings, week: WeekChoice) -> dict[tuple, list[tuple[str, float]]]:
    """(group, provenance, device) -> [(reason, excluded minutes)], from `report.records`.

    `SourceTotal` carries the excluded minutes but not why; the record browser
    rows carry the reason, so the reasons are read from there.
    """
    out: dict[tuple, dict[str, float]] = {}
    for r in report.records(conn, clk, settings, start=week.sunday, end=week.saturday, limit=10**9):
        if r.covered_elsewhere is None or r.minutes is None or r.record_type != "measurement":
            continue
        bucket = out.setdefault((r.group, r.provenance, r.device), {})
        bucket[r.covered_elsewhere] = bucket.get(r.covered_elsewhere, 0.0) + r.minutes
    return {k: sorted(v.items()) for k, v in out.items()}


def split_bars(splits: list[report.WeekSplit], current_week_start: str, tz: str) -> dict:
    """Geometry for the stacked wasteful/worthwhile bars. Scale only; no judgement."""
    height, bar_w, gap, top, label_h = 160, 56, 36, 28, 40
    peak = max((s.wasteful_min + s.worthwhile_min for s in splits), default=0.0)
    scale = (height / peak) if peak > 0 else 0.0
    bars = []
    for i, s in enumerate(splits):
        x = gap // 2 + i * (bar_w + gap)
        waste_h = s.wasteful_min * scale
        worth_h = s.worthwhile_min * scale
        base = top + height
        bars.append({
            "x": x,
            "cx": x + bar_w / 2,
            "waste_y": base - waste_h,
            "waste_h": waste_h,
            "worth_y": base - waste_h - worth_h,
            "worth_h": worth_h,
            "total_y": base - waste_h - worth_h - 6,
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


def latest_imports(conn: sqlite3.Connection) -> list[dict]:
    """The latest stored import per kind, read back verbatim (status, error, warnings).

    `report.py` has no function for this; it is a plain read of stored rows,
    not a derived figure.
    """
    out = []
    for kind in T.IMPORT_KINDS:
        row = conn.execute("SELECT * FROM imports WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        if row is None:
            out.append({"kind": kind, "row": None, "warnings": []})
            continue
        try:
            warnings = list(json.loads(row["warnings"] or "[]"))
        except ValueError:
            warnings = ["(stored warnings could not be read)"]
        out.append({"kind": kind, "row": row, "warnings": warnings})
    return out


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
    app.jinja_env.filters["pct"] = fmt_pct
    app.jinja_env.filters["local_dt"] = lambda iso: clock.fmt_local(iso, settings.tz) if iso else ""
    app.jinja_env.globals.update(
        GROUP_NAMES=GROUP_NAMES, KIND_NAMES=KIND_NAMES, IMPORT_KIND_NAMES=IMPORT_KIND_NAMES,
        PROVENANCE_BADGES=PROVENANCE_BADGES, PROVENANCE_TEXT=T.PROVENANCE_TEXT, BASIS_TEXT=BASIS_TEXT,
        PROV_SCREENSHOT=T.PROV_SCREENSHOT, HTMX_URL=HTMX_URL, HTMX_SRI=HTMX_SRI,
        UNKNOWN_CHANNEL_REASON=UNKNOWN_CHANNEL_REASON, WASTEFUL=T.WASTEFUL, WORTHWHILE=T.WORTHWHILE,
    )

    @app.context_processor
    def _header():
        return {"countdown": countdown(conn(), clk, settings)}

    @app.before_request
    def _refuse_cross_site_writes():
        if request.method == "POST" and not request.path.startswith("/api/"):
            if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                abort(403, description="cross-site form posts are refused")

    @app.get("/")
    def overview():
        c = conn()
        week = choose_week(clk, settings, request.args.get("week"))
        status = report.allowance_status(c, clk, settings)
        splits = report.label_split(c, clk, settings)
        return render_template(
            "overview.html",
            week=week,
            totals=report.weekly_totals(c, clk, settings, week.week_start),
            covered=covered_reasons(c, clk, settings, week),
            chart=split_bars(splits, status.week_start, settings.tz),
            allowance=status,
            gated=report.gated_vs_ungated(c, clk, settings, week.week_start),
            keepers=report.cost_per_keeper(c, clk, settings),
            funnels=report.funnels(c, clk, settings),
            blind_spots=report.blind_spots(c, clk, settings),
            imports=latest_imports(c),
            window_start=report.window_start(c, clk, settings),
        )

    def _label_filters(args) -> tuple[str | None, str | None]:
        kind = args.get("kind") or None
        label = args.get("label") or None
        if kind is not None and kind not in T.SOURCE_KINDS:
            abort(400, description=f"unknown kind {kind!r}")
        if label is not None and label not in (*T.LABELS, labels.UNLABELLED):
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
        if new_label not in T.LABELS:
            abort(400, description=f"label must be one of {', '.join(T.LABELS)}")
        source = labels.set_source_label(c, clk, source_id, new_label)
        # DECISIONS DR2.7: a relabel changes carried debt now, not at the next import.
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

    @app.get("/records")
    def records_page():
        errors: list[str] = []
        group = request.args.get("group") or None
        label = request.args.get("label") or None
        if group is not None and group not in T.REPORT_GROUPS:
            errors.append(f"Unknown source group {group!r}; ignored.")
            group = None
        if label is not None and label not in T.LABELS:
            errors.append(f"Unknown label {label!r}; ignored.")
            label = None
        start = _parse_day(request.args.get("start"), "Start", errors)
        end = _parse_day(request.args.get("end"), "End", errors)
        c = conn()
        rows = report.records(c, clk, settings, group=group, label=label, start=start, end=end,
                              limit=RECORDS_LIMIT)
        return render_template(
            "records.html", rows=rows, errors=errors, f_group=group, f_label=label, f_start=start,
            f_end=end, groups=T.REPORT_GROUPS, limit=RECORDS_LIMIT,
            window_start=report.window_start(c, clk, settings),
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
