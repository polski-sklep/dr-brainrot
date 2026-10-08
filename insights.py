"""insights.py — the weekly written summary at the top of the Overview (DR5.8).

CONTRACTS.md Part II §20. The user asked for it in these words:

> "I'd like 'overview' to be a summary based on LLM findings parsed findings
> (Opus 5) on my activity, e.g. 'brainrot activity is decreasing but reflex
> content consumption remains an issue' or whatever"

Five rules this module exists to enforce.

1. **Aggregates only leave this Mac.** `build_payload` assembles report group
   names, minutes by label, week-on-week changes, allowance and overage,
   reflex share, cost per keeper, funnel counts and coverage gaps — and
   nothing else. No podcast show, YouTube channel, iPhone app, domain, Mac
   app, video id, item title, note title or path is ever put in the payload,
   and `tests/test_insights.py` seeds a database with distinctive names and
   asserts none of them appears. The payload is stored verbatim beside the
   summary so what was sent can always be checked.

2. **It describes; it does not judge.** dr-brainrot's rule is that the system counts
   and the person labels (DR5.5, DR5.6). The prompt says so explicitly: name
   the measured trend, do not say whether any of it was worth the time, do
   not suggest relabelling anything, give no advice and no praise. Every
   number must come from the payload — `SYSTEM_PROMPT` says that too.

3. **It writes one row and changes nothing else.** The only table this module
   writes is `insights`. No figure, no label, no week, no measurement is
   touched; `build_payload` and `latest` issue SELECTs only.

4. **A failure is never the caller's problem.** `write_summary` catches
   everything: no key, a network error, a refusal, a truncated or empty
   answer. It stores `status='failed'` with the reason and returns the
   locally computed `fallback_summary`, which is honest about having too
   little data rather than inventing a trend. It never raises.

5. **The key never leaves `Settings`.** It is not logged, not stored, not put
   in the payload, and `_scrub` removes it from any error text before that
   text reaches the database or the log. Tests never construct a real client:
   `conftest.py` blocks the network and every test injects a fake or runs the
   no-key path.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

import allowance
import clock
import dr_brainrot_types as T
import evidence
import report
from clock import Clock
from config import Settings

log = logging.getLogger("dr-brainrot.insights")

#: The model the user chose for this. Opus 5.
MODEL = "claude-opus-5"

#: Three to five short sentences is ~150 tokens; 400 leaves room without
#: leaving room for an essay. Thinking is off (see `_call`), so the whole
#: budget is the answer.
MAX_TOKENS = 400

#: Bumped whenever the payload's shape changes, so a stored payload can always
#: be read back against the code that wrote it.
PAYLOAD_SCHEMA = 2      # 2: `comparison`, the like-for-like week-on-week figures (DR5.17)

#: How many complete weeks of figures the payload carries.
WEEKS = 8

#: Minutes are rounded to this many places everywhere in the payload, so two
#: runs over unchanged data produce byte-identical JSON.
ROUND = 1

#: Below this, a week-on-week change is "held steady" rather than a direction.
FLAT_PCT = 5.0

#: The `insights.status` column. Storage and querying only — the view layer
#: branches on `Insight.model` / `Insight.fallback_reason`, never on this.
STATUS_OK = "ok"
STATUS_FAILED = "failed"

#: Why a summary is dr-brainrot's own sentences rather than a model's, in the words
#: the dashboard prints. A failed import shows its real error everywhere else
#: on this dashboard; this panel is not the one place that shows a code.
REASON_NO_KEY = ("No ANTHROPIC_API_KEY is set, so these sentences were written from the stored "
                 "figures.")
REASON_TOO_LITTLE = ("Too little data: no complete week of figures yet, so there was nothing "
                     "to summarise.")
REASON_CALL_FAILED = "API call failed: {error}"

#: Failures a person can act on, recognised from the provider's own message.
#: The panel prints the reason verbatim, so a raw 400 body with a request id
#: reads as noise where one sentence would tell the user what to do. The full
#: error is still stored in `insights.error` either way.
KNOWN_FAILURES = (
    ("credit balance is too low",
     "The summary needs Anthropic credit and the balance is too low, so these sentences were written "
     "sentences from its own figures."),
    ("rate_limit", "The API was rate-limited, so these sentences were written from the stored figures. "
                   "The next weekly run will try again."),
    ("authentication_error", "The Anthropic API key was rejected, so these sentences were written "
                             "from its own figures."),
)


def explain_failure(error: str) -> str:
    """A sentence a person can act on, or the raw error when it is not one we know."""
    low = error.lower()
    for needle, sentence in KNOWN_FAILURES:
        if needle in low:
            return sentence
    return REASON_CALL_FAILED.format(error=error if len(error) <= 200 else error[:197] + "…")
REASON_UNKNOWN = "The summary was not written by a model; the reason was not recorded."

SYSTEM_PROMPT = """\
You write one short summary panel for a personal diagnostic that measures how much \
brain-rot content one person consumes each week.

Every number you use must come from the JSON payload in the user's message. Do not invent a \
figure, do not estimate one, and do not state any number that is not in the payload. You may \
compare two numbers that are both in the payload; you may not derive anything else.

The standing rule is that the system counts and the person labels. Do not say whether any of \
this content was worth the time, and do not suggest that anything should be labelled \
differently. Give no advice and no recommendations, and do not tell the person what to do next: \
concrete suggestions, with names, are computed separately on their own machine and shown \
beside this paragraph, so any advice here would only repeat them less precisely.

When the like-for-like comparison shows a fall, say so plainly and first — it is the thing the \
person most wants to know. State it as a fact, not a compliment: no cheerleading, no \
exclamation, no criticism and no moralising.

The payload's comparison block is the only trustworthy week-on-week figure. The weeks' own \
totals are not comparable when comparison.like_for_like is false: a channel only one week had, \
a reading that could not be read, or apps below a cut-off Screen Time list make one week look \
bigger or smaller for reasons that are not behaviour. When like_for_like is false, describe the \
change only through comparison.before_min and comparison.after_min, say it is like for like, \
and say briefly what was left out — by channel name, and by how many sources. Never present the \
difference between the weeks' raw totals as a change in what the person did. The same holds for \
every per-group figure: when like_for_like is false, the groups carry the same distortion as the \
totals, so do not set one week's group figures beside the other's, and do not call any \
difference between them a shift, a rise or a fall. Describe the latest week's groups on their \
own if you describe them at all.

Where the payload's coverage_gaps say a week's evidence never arrived, say that the week is \
incomplete rather than treating the gap as zero.

wasteful_min counts only sources the person has actually labelled wasteful. unclassified_min is \
time on sources they have not labelled at all: it is not a judgement that the time was wasted, \
and it must never be described as wasteful, brain rot or a problem. charged_min is the two \
together, which is what the allowance counted. Where unclassified_min is a large share of \
charged_min, say plainly that much of the week is undecided and that the wasteful figure \
therefore describes only the part that has been labelled — state it as a limit on what can be \
read from the figures, not as something the person should go and fix.

The payload names report groups only: podcasts, YouTube, X, iPhone apps, desktop web, desktop \
apps, audiobooks. You are not told the name of any show, channel, app, site, video or note, and must not \
guess at one or refer to one.

Write 3 to 5 short sentences of plain English, as one paragraph. Name the trend across the weeks, \
then the single biggest pattern in the figures. No headings, no bullet points, no lists, no \
markdown, no bold. Do not include internal or system XML tags in your response. Reply with the \
summary itself and nothing else."""

USER_PROMPT_HEADER = (
    "These are dr-brainrot's own figures for the complete weeks so far. Write the summary.\n\n"
)


def user_prompt(payload: dict) -> str:
    """The user turn: the header, then the payload as sorted, indented JSON."""
    return USER_PROMPT_HEADER + json.dumps(payload, indent=2, sort_keys=True)


@dataclass(frozen=True)
class Insight:
    """One stored summary, as the dashboard renders it.

    One fact, one field. **`model is None` is the fallback**: a model run
    always names its model, dr-brainrot's own sentences never do, so there is no
    second flag that could disagree with it.

    `fallback_reason` is the reason in the words to print — "No
    ANTHROPIC_API_KEY is set…", "API call failed: …", "Too little data: …" —
    and is None exactly when a model wrote `summary`. The stored row keeps a
    `status` column for querying; nothing renders it.

    `created_at` is a UTC instant (`clock.iso`): when its figures were read,
    just before the request, so data landing while the request is in flight
    counts as after it (DR5.26). `week_start` is the allowance week the
    summary was generated in, UTC ISO.

    `based_on_through` is a **local date, `YYYY-MM-DD` in `settings.tz`**: the
    last local day the figures cover, which is the Saturday of the last
    complete measurement week. It is deliberately a local date and not an
    instant, and is never derived from `week_start`, so it compares directly
    with `report.window_start`, with `T.MeasurementWeek.end` and with
    `T.local_date(clk.now(), tz)` — that is what lets the panel say "written
    before Thursday's data arrived". None when no week was complete.
    """

    summary: str
    created_at: str
    model: str | None
    week_start: str
    fallback_reason: str | None
    based_on_through: str | None


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------

def _r(value: float | None) -> float | None:
    """Round a rate or a ratio. Minutes go through `_min` instead."""
    return None if value is None else round(float(value), ROUND)


def _round_min(value: float | None) -> float | None:
    """Round minutes the way the dashboard prints them (`dashboard.app.fmt_min`).

    Whole minutes at 10 and above, one decimal below. The summary quotes these
    figures in prose beside the page's own tables, so "494.7 minutes" next to
    "495 min" would read as two different measurements of the same thing, and
    a trailing "177.0" reads as precision that isn't there.
    """
    if value is None:
        return None
    value = float(value)
    return round(value) if abs(value) >= 10 else round(value, 1)


def _pct_change(current: float, previous: float) -> float | None:
    """Percent change, or None when the previous week was zero (no ratio exists)."""
    if previous == 0:
        return None
    return round((current - previous) / previous * 100.0, ROUND)


def _complete_week_starts(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[str]:
    """The last `WEEKS` allowance weeks that are over, oldest first.

    `report.label_split` already clamps to the diagnostic window and ends with
    the current week; the current week is dropped because it is still running
    and a half week read as a trend would be a lie every Monday.
    """
    current = clock.iso(clock.week_start_for(clk.now(), settings.tz))
    splits = report.label_split(conn, clk, settings, weeks=WEEKS + 1)
    return [s.week_start for s in splits if s.week_start != current]


def _coverage_gaps(conn: sqlite3.Connection, settings: Settings, week_start: str) -> list[str]:
    """One line per weekly import that never arrived for this week.

    The words are the group's, not the source's: "iPhone apps (Screen Time)",
    never an app name. Qbserve is only expected while it is configured and
    Audible only while he is signed in (`report.expects`): without them those
    groups are deliberately unmeasured rather than an import the user owes,
    exactly as `report.weekly_totals` treats them.
    """
    mweek = T.measurement_week_for_allowance_week(week_start, settings.tz)
    out: list[str] = []
    for kind, group, _prov, _device, name in report.EXPECTED_WEEKLY:
        if not report.expects(kind, settings):
            continue
        if report._covering_import(conn, settings, kind, mweek) is None:
            out.append(f"{report.GROUP_TEXT.get(group, group)} ({name}) was not received "
                       f"for the week {mweek.start} to {mweek.end}.")
    return out


def _week_facts(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                week_start: str, split: report.WeekSplit | None) -> dict:
    """One week of the payload: totals, allowance, reflex, per-group rows, gaps."""
    pooled = allowance.pooled_minutes(conn, settings, week_start)
    gated = {g.group: g for g in report.gated_vs_ungated(conn, clk, settings, week_start)}
    mweek = T.measurement_week_for_allowance_week(week_start, settings.tz)

    wasteful = pooled.wasteful_min
    reflex = sum(g.wasteful_ungated_min for g in gated.values())

    groups = []
    for group in T.REPORT_GROUPS:
        gm = pooled.by_group.get(group, allowance.GroupMinutes())
        gr = gated.get(group)
        groups.append({
            "group": group,
            "wasteful_min": _round_min(gm.wasteful_min),
            "worthwhile_min": _round_min(gm.worthwhile_min),
            "unclassified_min": _round_min(gm.unclassified_min),
            "tool_min": _round_min(gm.tool_min),
            "gated_min": _round_min(gr.gated_min) if gr else 0.0,
            "ungated_min": _round_min(gr.ungated_min) if gr else 0.0,
            "share_ungated": _r(gr.share_ungated) if gr else None,
            "wasteful_ungated_min": _round_min(gr.wasteful_ungated_min) if gr else 0.0,
            "unmeasured_items": gm.unmeasured_items,
            "covered_excluded_min": _round_min(gm.covered_excluded_min),
        })

    return {
        "week_start": week_start,
        "days": {"from": mweek.start, "to": mweek.end},
        "wasteful_min": _round_min(wasteful),
        "worthwhile_min": _round_min(pooled.worthwhile_min),
        "unclassified_min": _round_min(pooled.unclassified_min),
        "charged_min": _round_min(pooled.charged_min),
        "tool_min": _round_min(pooled.tool_min),
        "screenshot_ocr_min": _round_min(pooled.screenshot_ocr_min),
        "allowance_min": (split.allowance_min if split else settings.weekly_allowance_min),
        "overage_min": (split.overage_min if split else 0),
        "reflex_wasteful_min": _round_min(reflex),
        "reflex_share_of_wasteful": _r(reflex / wasteful) if wasteful > 0 else None,
        "unmeasured_items": pooled.unmeasured_items,
        "by_group": groups,
        "coverage_gaps": _coverage_gaps(conn, settings, week_start),
    }


def _comparison(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> dict | None:
    """The like-for-like week-on-week figures, without a single name (DR5.17).

    `progress.Progress` holds the same comparison for the page, with sources
    named. Only its name-free half crosses here: channel names are that
    module's fixed vocabulary, and sources set aside are counted, not listed.
    Wins and tips never leave the Mac — they are nothing but names.
    """
    import progress  # noqa: PLC0415 — progress imports report, which this module also uses

    p = progress.progress(conn, clk, settings)
    if p is None or p.previous_start is None:
        return None
    prev = T.measurement_week_for_allowance_week(p.previous_start, settings.tz)
    return {
        "week": {"from": p.week_sunday, "to": p.week_saturday},
        "previous_week": {"from": prev.start, "to": prev.end},
        "direction": p.direction,
        "like_for_like": p.like_for_like,
        "before_min": _round_min(p.before_min),
        "after_min": _round_min(p.after_min),
        "pct": _pct_change(p.after_min, p.before_min),
        "channels_set_aside": list(p.channels_set_aside),
        "unreadable_sources": p.unreadable_sources,
        "below_cut_sources": p.below_cut_sources,
    }


def build_payload(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> dict:
    """The aggregate-only facts the summary is written from. Read-only.

    Deterministic and JSON-serialisable: every float is rounded, every list is
    in a fixed order (weeks oldest first, groups in `T.REPORT_GROUPS` order,
    cost per keeper in `report.cost_per_keeper`'s ranking), and nothing is
    keyed on iteration order. Two runs over unchanged data produce the same
    dict.

    What it contains, and nothing else: report **group** names, minutes by
    label per group per week, week-on-week changes, allowance and overage,
    reflex (ungated wasteful) minutes and share, gated vs ungated per group,
    cost per keeper per group, the queue and vault funnel counts and rates, and
    per-week coverage gaps. No source name, item title, note title, path,
    video id or anything else from the vault ever reaches it.
    """
    starts = _complete_week_starts(conn, clk, settings)
    splits = {s.week_start: s for s in report.label_split(conn, clk, settings, weeks=WEEKS + 1)}

    weeks = [_week_facts(conn, clk, settings, start, splits.get(start)) for start in starts]
    for i, week in enumerate(weeks):
        if i == 0:
            week["change_from_previous_week"] = None
            continue
        prev = weeks[i - 1]
        week["change_from_previous_week"] = {
            "wasteful_min": _round_min(week["wasteful_min"] - prev["wasteful_min"]),
            "wasteful_pct": _pct_change(week["wasteful_min"], prev["wasteful_min"]),
            "worthwhile_min": _round_min(week["worthwhile_min"] - prev["worthwhile_min"]),
            "worthwhile_pct": _pct_change(week["worthwhile_min"], prev["worthwhile_min"]),
            "tool_min": _round_min(week["tool_min"] - prev["tool_min"]),
        }

    diag = evidence.diagnostic(conn, clk, settings)
    funnels = report.funnels(conn, clk, settings)
    comparison = _comparison(conn, clk, settings)

    return {
        "schema": PAYLOAD_SCHEMA,
        "generated_for_week_start": clock.iso(clock.week_start_for(clk.now(), settings.tz)),
        "timezone": settings.tz,
        # A local date (YYYY-MM-DD) in settings.tz: the Saturday of the last
        # complete measurement week. Never an instant, never from week_start.
        "based_on_through": weeks[-1]["days"]["to"] if weeks else None,
        "complete_weeks": len(weeks),
        "weekly_allowance_min": settings.weekly_allowance_min,
        "diagnostic": {
            "started": diag.started,
            "ended": diag.ended,
            "start": diag.start,
            "end": diag.end,
            "week_number": diag.week_number,
            "total_weeks": settings.diagnostic_weeks,
        },
        "weeks": weeks,
        "comparison": comparison,
        "cost_per_keeper": [
            {"group": k.group, "minutes": _round_min(k.minutes), "keepers": k.keepers,
             "minutes_per_keeper": _round_min(k.minutes_per_keeper)}
            for k in report.cost_per_keeper(conn, clk, settings)
        ],
        "funnels": {
            "queue": {
                "captured": funnels.queue.captured,
                "survived_lock": funnels.queue.survived_lock,
                "promoted": funnels.queue.promoted,
                "played": funnels.queue.played,
                "fully_played": funnels.queue.fully_played,
                "survived_rate": _r(funnels.queue.survived_rate),
                "promoted_rate": _r(funnels.queue.promoted_rate),
                "played_rate": _r(funnels.queue.played_rate),
                "fully_played_rate": _r(funnels.queue.fully_played_rate),
            },
            "vault": {
                "captured": funnels.vault.captured,
                "unprocessed": funnels.vault.unprocessed,
                "kept": funnels.vault.kept,
                "excluded": funnels.vault.excluded,
                "edited_kept": funnels.vault.edited_kept,
            },
        },
    }


# --------------------------------------------------------------------------
# The fallback — computed here, never guessed
# --------------------------------------------------------------------------

def _min(value: float | None) -> str:
    if value is None:
        return "not measured"
    return f"{value:,.0f} min" if value >= 10 or value == 0 else f"{value:.1f} min"


def fallback_summary(payload: dict) -> str:
    """dr-brainrot's own sentences, computed from the payload with no model involved.

    Used when there is no API key, when the call fails, and whenever the
    stored row is `failed`. It says outright when there is too little data to
    speak of a trend rather than manufacturing one from a single week.
    """
    weeks = payload.get("weeks") or []
    if not weeks:
        return ("No week of figures is complete yet, so there is nothing to compare. "
                "The summary starts once the first week is over.")

    last = weeks[-1]
    sentences: list[str] = []

    if len(weeks) < 2:
        sentences.append(
            f"One complete week so far ({last['days']['from']} to {last['days']['to']}), "
            f"so there is no week-on-week trend yet: {_min(last['wasteful_min'])} wasteful content, "
            f"{_min(last['worthwhile_min'])} worthwhile.")
    elif (cmp := payload.get("comparison")) and not cmp.get("like_for_like"):
        # The weeks were not measured the same way, so their raw totals must
        # not be compared (DR5.17). Say what the like-for-like figures show,
        # and what was left out, by channel and by count — never by name.
        before, after = cmp["before_min"], cmp["after_min"]
        verb = {"down": "fell", "up": "rose"}.get(cmp.get("direction"), "held level")
        pct = f" {abs(cmp['pct']):.0f}%" if cmp.get("pct") is not None and verb != "held level" else ""
        sentences.append(f"Like for like, counted brain-rot time {verb}{pct} week on week, from "
                         f"{_min(before)} to {_min(after)}.")
        left: list[str] = list(cmp.get("channels_set_aside") or [])
        if cmp.get("unreadable_sources"):
            n = cmp["unreadable_sources"]
            left.append(f"{n} reading{'s' if n != 1 else ''} that could not be read")
        if cmp.get("below_cut_sources"):
            n = cmp["below_cut_sources"]
            left.append(f"{n} app{'s' if n != 1 else ''} below a cut-off Screen Time list")
        if left:
            sentences.append("Left out because the two weeks did not measure them the same way: "
                             + "; ".join(left) + ".")
    else:
        prev = weeks[-2]
        now_w, prev_w = last["wasteful_min"], prev["wasteful_min"]
        pct = _pct_change(now_w, prev_w)
        if pct is None:
            sentences.append(
                f"Wasteful content went from nothing to {_min(now_w)} week on week."
                if now_w > 0 else
                "No wasteful content was measured in either of the last two weeks.")
        elif abs(pct) < FLAT_PCT:
            sentences.append(f"Wasteful content held steady week on week, {_min(prev_w)} to "
                             f"{_min(now_w)}.")
        else:
            direction = "fell" if pct < 0 else "rose"
            sentences.append(f"Wasteful content {direction} {abs(pct):.0f}% week on week, from "
                             f"{_min(prev_w)} to {_min(now_w)}.")
        sentences.append(f"Across {len(weeks)} complete weeks the wasteful weekly average is "
                         f"{_min(sum(w['wasteful_min'] for w in weeks) / len(weeks))}.")

    undecided = last.get("unclassified_min") or 0
    charged = last.get("charged_min") or 0
    if undecided > 0 and charged > 0:
        sentences.append(
            f"{_min(undecided)} of that week's {_min(charged)} counted minutes are on sources "
            f"with no label yet, so the wasteful figure covers only the labelled part.")

    share = last.get("reflex_share_of_wasteful")
    if share is not None:
        sentences.append(f"{share * 100:.0f}% of wasteful content bypassed the queue.")

    over = last.get("overage_min") or 0
    if over > 0:
        sentences.append(f"That week went {over:,} min past the "
                         f"{last.get('allowance_min', 0):,} min allowance.")

    gaps = last.get("coverage_gaps") or []
    if gaps:
        sentences.append(f"That week's evidence is incomplete: {len(gaps)} weekly import "
                         f"{'was' if len(gaps) == 1 else 'were'} not received, so the figures are "
                         "a floor, not a total.")
    return " ".join(sentences)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _scrub(text: str, settings: Settings) -> str:
    """Remove the API key from any text on its way to the database or the log."""
    key = getattr(settings, "anthropic_api_key", "") or ""
    return text.replace(key, "***") if key else text


def _row_to_insight(row: sqlite3.Row) -> Insight:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    is_fallback = row["status"] != STATUS_OK
    summary = row["summary"] or ""
    if is_fallback and payload:
        # A fallback's text is dr-brainrot's own; recompute it so it always matches
        # the payload stored beside it.
        summary = fallback_summary(payload)
    return Insight(
        summary=summary,
        created_at=row["created_at"],
        # `model is None` IS the fallback. A failed row never keeps a model.
        model=None if is_fallback else row["model"],
        week_start=row["week_start"],
        fallback_reason=(row["error"] or REASON_UNKNOWN) if is_fallback else None,
        based_on_through=payload.get("based_on_through"),
    )


def _existing(conn: sqlite3.Connection, week_start: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM insights WHERE week_start = ? ORDER BY id DESC LIMIT 1", (week_start,)
    ).fetchone()


def latest(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Insight | None:
    """The newest stored summary, whatever week it was written in, or None.

    A fallback row still returns an `Insight`: its text is dr-brainrot's own computed
    sentences, `model` is None and `fallback_reason` says why, so the panel
    shows honest sentences and the reason rather than nothing at all.
    """
    row = conn.execute("SELECT * FROM insights ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_insight(row) if row is not None else None


def _store(conn: sqlite3.Connection, clk: Clock, settings: Settings, *, week_start: str, created_at: str,
           payload: dict, summary: str, status: str, model: str | None, error: str | None,
           input_tokens: int | None, output_tokens: int | None) -> Insight:
    """Write one row. `error` is the printable reason a fallback was used.

    `created_at` is when the payload's figures were read, not now: the API
    call takes seconds, and an import that lands during it is in neither the
    summary nor, if stamped after the answer, the staleness check.
    """
    cur = conn.execute(
        "INSERT INTO insights (created_at, model, week_start, summary, payload, status, error, "
        "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (created_at, model, week_start, summary,
         json.dumps(payload, indent=2, sort_keys=True), status,
         _scrub(error, settings) if error else None, input_tokens, output_tokens),
    )
    conn.commit()
    return _row_to_insight(
        conn.execute("SELECT * FROM insights WHERE id = ?", (cur.lastrowid,)).fetchone())


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------

class SummaryUnusable(RuntimeError):
    """The API answered, but with nothing that can be shown."""


def _call(client, payload: dict) -> tuple[str, int | None, int | None]:
    """One Messages request. -> (text, input_tokens, output_tokens).

    Thinking is switched off and effort is `low`: the answer is three to five
    sentences read straight off a JSON document, so reasoning tokens would eat
    the 400-token budget the summary itself needs. There are no tools here, so
    the tool-call failure mode of disabled thinking cannot apply, and the
    system prompt carries the "no internal or system XML tags" instruction
    that covers the other one.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user_prompt(payload)}],
    )
    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        raise SummaryUnusable("the model declined to answer")
    if stop == "max_tokens":
        raise SummaryUnusable(f"the answer was cut off at max_tokens ({MAX_TOKENS})")
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise SummaryUnusable("the answer contained no text")
    usage = getattr(response, "usage", None)
    return text, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def write_summary(conn: sqlite3.Connection, clk: Clock, settings: Settings, *,
                  client=None, force: bool = False) -> Insight:
    """Build the payload, ask Claude for the summary, store the row, return it.

    Once a week, once written: a *successful* row for the current allowance
    week is returned untouched unless `force` (the Overview's refresh button).
    A fallback row is retried by the next daily run, because the failure that
    produced it is usually a passing one — a network blip, a rate limit, a
    balance that is topped up an hour later — and without a retry a single bad
    morning leaves the whole week showing dr-brainrot's own sentences until someone
    notices the panel and presses refresh.

    That retry cannot become a daily API call for the reasons that would
    deserve the worry. No key and too little data both return above, before any
    client is built, so they cost a SELECT. What remains is a provider that is
    genuinely down or an account that is genuinely empty: one rejected call a
    day, which bills nothing and stops the day it is fixed.

    Never raises. Anything at all going wrong — no key, too little data, a
    network error, a refusal, a truncated or empty answer — stores
    `status='failed'` with `model` NULL and the printable, key-scrubbed reason,
    and returns the locally computed fallback.
    """
    week_start = clock.iso(clock.week_start_for(clk.now(), settings.tz))
    if not force:
        row = _existing(conn, week_start)
        if row is not None and row["status"] == STATUS_OK:
            return _row_to_insight(row)

    as_of = clock.iso(clk.now())            # before the figures are read: see `_store`
    payload = build_payload(conn, clk, settings)
    fallback = fallback_summary(payload)

    def store_fallback(reason: str) -> Insight:
        # A fallback never keeps a model: `model is None` is what the view
        # branches on, and `error` holds the reason it prints. The provider's
        # raw message is logged rather than stored: the panel shows this text
        # to a person, and a 400 body with a request id is not a sentence.
        return _store(conn, clk, settings, week_start=week_start, created_at=as_of, payload=payload,
                      summary=fallback, status=STATUS_FAILED, model=None, error=reason,
                      input_tokens=None, output_tokens=None)

    if not payload.get("complete_weeks"):
        # Nothing to summarise yet, so nothing is sent and nothing is spent.
        log.info("insights: no complete week yet; storing the computed sentences")
        return store_fallback(REASON_TOO_LITTLE)

    if client is None:
        key = getattr(settings, "anthropic_api_key", "") or ""
        if not key:
            log.info("insights: no ANTHROPIC_API_KEY; storing the computed sentences")
            return store_fallback(REASON_NO_KEY)
        try:
            import anthropic  # noqa: PLC0415 — optional dependency, only needed here
            client = anthropic.Anthropic(api_key=key)
        except Exception as exc:  # noqa: BLE001 — a missing package must not break the run
            detail = _scrub(f"{type(exc).__name__}: {exc}", settings)
            log.warning("insights: %s", detail)
            return store_fallback(explain_failure(detail))

    try:
        text, input_tokens, output_tokens = _call(client, payload)
    except Exception as exc:  # noqa: BLE001 — a failed summary must never fail the caller
        detail = _scrub(f"{type(exc).__name__}: {exc}", settings)
        log.warning("insights: %s", detail)
        return store_fallback(explain_failure(detail))

    return _store(conn, clk, settings, week_start=week_start, created_at=as_of, payload=payload, summary=text,
                  status=STATUS_OK, model=MODEL, error=None, input_tokens=input_tokens,
                  output_tokens=output_tokens)
