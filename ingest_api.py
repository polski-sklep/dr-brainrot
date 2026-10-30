"""Local HTTP ingest for the browser extension: `/api/youtube/*`.

Phase 2, Agent A. Implements the `ingest_api.py` part of CONTRACTS.md Part II
§6.1. Mounted by the dashboard app (Phase 3) on `127.0.0.1:dashboard_port`
(DECISIONS DR1.19); this module creates no app and binds no socket.

Why a token on a loopback-only port: any web page the user has open can make
the browser POST to `http://127.0.0.1:<port>`. Without a shared secret such a
page could feed invented watch history into the measurements. The token is
compared in constant time and an unset token refuses everything rather than
accepting everything.

Every processed POST answers with the Outcome, including failures: the
extension shows it verbatim, so a broken page structure is seen as a failure,
never as "0 videos".
"""
from __future__ import annotations

import dataclasses
import hmac
import logging
import sqlite3
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from clock import Clock
from config import Settings
from sources import youtube

log = logging.getLogger("dr-brainrot.ingest_api")

#: CONTRACTS §6.1. A week of history is typically one or two ~2 MB pages.
MAX_BODY_BYTES = 25 * 1024 * 1024

TOKEN_HEADER = "X-Dr-Brainrot-Token"


def _authorised(settings: Settings) -> bool:
    expected = settings.ingest_token or ""
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _unauthorised():
    return jsonify({"error": f"missing or wrong {TOKEN_HEADER} header"}), 401


def make_blueprint(*, get_conn: Callable[[], sqlite3.Connection], clk: Clock, settings: Settings,
                   api_factory: Callable[[], Any]) -> Blueprint:
    """The `/api/youtube` routes.

    `get_conn()` returns a connection the caller owns (the blueprint never
    closes it). `api_factory()` builds a `DataApiClient`-shaped object per
    POST; it is closed afterwards if it has `close()`, and it is only built
    once the request is authorised, so an unauthorised caller costs no quota.
    """
    bp = Blueprint("dr_brainrot_ingest", __name__, url_prefix="/api/youtube")

    @bp.get("/cursor")
    def cursor():
        if not _authorised(settings):
            return _unauthorised()
        conn = get_conn()
        return jsonify({"cursor": youtube.current_cursor(conn),
                        "cursor_date": youtube.current_cursor_date(conn)})

    @bp.post("/history")
    def history():
        if not _authorised(settings):
            return _unauthorised()
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return jsonify({"error": f"payload larger than {MAX_BODY_BYTES // (1024 * 1024)} MB; "
                                     "lower the extension's page cap"}), 413
        body = request.stream.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            return jsonify({"error": f"payload larger than {MAX_BODY_BYTES // (1024 * 1024)} MB; "
                                     "lower the extension's page cap"}), 413

        conn = get_conn()
        api = api_factory()
        try:
            outcome = youtube.ingest(conn, clk, settings, body, api)
        finally:
            close = getattr(api, "close", None)
            if callable(close):
                close()
        result = dataclasses.asdict(outcome)
        result["warnings"] = list(outcome.warnings)
        log.info("youtube history import %s: %s, %d written, %d warnings", outcome.import_id,
                 outcome.status, outcome.measurements_written, len(outcome.warnings))
        return jsonify(result), (422 if outcome.status == "failed" else 200)

    return bp
