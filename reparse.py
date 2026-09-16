"""reparse.py — re-run a parser over evidence DRBR already stored.

    .venv/bin/python reparse.py list [kind]      # recent imports: id, kind, status, warnings
    .venv/bin/python reparse.py <import_id>      # re-parse that import's raw evidence

Every import keeps its original bytes under DRBR_RAW_DIR (evidence.store_raw),
so a parser fix never needs the user to send a screenshot, export or history
capture again. A re-parse writes a NEW import row whose facts replace the old
ones on their natural key; the old row and its raw file stay as history.

Vault scans have no raw evidence (the vault itself is the evidence): run
`jobs.py daily` again instead.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable

import clock
import db
import evidence

USAGE = "usage: reparse.py list [kind] | reparse.py <import_id>"


def _dispatch(kind: str, settings) -> Callable:
    """-> fn(conn, clk, settings, import_id) -> Outcome, for this import kind."""
    if kind == "coldturkey":
        from sources import coldturkey

        return coldturkey.reparse
    if kind == "screentime":
        from sources import screentime

        return screentime.reparse
    if kind == "youtube_history":
        from sources import youtube

        def run(conn, clk, s, import_id):
            api = youtube.DataApiClient(s)
            try:
                return youtube.reparse(conn, clk, s, import_id, api)
            finally:
                api.close()

        return run
    raise ValueError(f"imports of kind {kind!r} have no raw evidence to re-parse")


def list_imports(conn, kind: str | None = None, limit: int = 20) -> list[str]:
    sql = "SELECT id, kind, received_at, status, warnings, error FROM imports"
    params: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    out = []
    for r in rows:
        warnings = json.loads(r["warnings"] or "[]")
        line = f"#{r['id']:<5} {r['kind']:<16} {r['received_at']}  {r['status']:<8} warnings={len(warnings)}"
        if r["error"]:
            line += f"  error={r['error'][:120]}"
        out.append(line)
    return out


def reparse_import(conn, clk, settings, import_id: int, dispatch=_dispatch) -> evidence.Outcome:
    row = conn.execute("SELECT kind FROM imports WHERE id = ?", (import_id,)).fetchone()
    if row is None:
        raise ValueError(f"no import #{import_id}")
    return dispatch(row["kind"], settings)(conn, clk, settings, import_id)


def main(argv: list[str]) -> int:  # pragma: no cover — thin CLI over tested functions
    import config

    args = argv[1:]
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    settings = config.load()
    conn = db.connect(settings)
    db.init_db(conn)
    db.migrate(conn)
    try:
        if args[0] == "list":
            print("\n".join(list_imports(conn, args[1] if len(args) > 1 else None)) or "no imports yet")
            return 0
        if not args[0].isdigit():
            print(USAGE, file=sys.stderr)
            return 2
        outcome = reparse_import(conn, clock.RealClock(), settings, int(args[0]))
        print(f"new import #{outcome.import_id}: {outcome.status}, "
              f"{outcome.measurements_written} measurement(s)")
        for w in outcome.warnings:
            print(f"  warning: {w}")
        if outcome.error:
            print(f"  error: {outcome.error}")
        return 0 if outcome.status != "failed" else 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
