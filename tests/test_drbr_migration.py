"""Migrations 0002 (DRBR) and 0003 (no debt, DR5.2): upgrade a representative pre-DRBR POP database.

The fixture database is built from the untouched version-1 `schema.sql` and
populated with rows in every POP table, in every queue state, so the upgrade
is tested against data shaped like the real installation rather than against
an empty file.
"""
from __future__ import annotations

import sqlite3

import pytest

import clock
import config
import db

V1_ROWS = {
    "episodes": [
        ("4rOoJ6Egrf8K2IrywzwOMk", "Episode A", "Show One", "desc", "2026-08-01", 3_600_000),
        ("5sPpK7Fhsg9L3JszxAxPNl", "Episode B", "Show Two", None, "2026", 1_800_000),
    ],
    "queue": [
        (1, "4rOoJ6Egrf8K2IrywzwOMk", "looked good", "2026-08-30T08:00:00+00:00", None, "locked", 0, None, None, None),
        (2, "4rOoJ6Egrf8K2IrywzwOMk", "for the trip", "2026-08-20T08:00:00+00:00",
         "2026-09-01T16:00:00+00:00", "promoted", 1, 1, "2026-08-23T17:00:00+00:00", None),
        (3, "5sPpK7Fhsg9L3JszxAxPNl", "curious", "2026-08-10T08:00:00+00:00", None, "dropped", 2, 0, None,
         "2026-08-12T08:00:00+00:00"),
        (4, "5sPpK7Fhsg9L3JszxAxPNl", "again", "2026-08-11T08:00:00+00:00", None, "played", 1, None,
         "2026-08-16T17:00:00+00:00", "2026-08-18T08:00:00+00:00"),
    ],
    "listening": [
        (1, "4rOoJ6Egrf8K2IrywzwOMk", "2026-08-24T07:17:00+00:00", 600_000, 600_000, 0),
        (2, "5sPpK7Fhsg9L3JszxAxPNl", "2026-08-17T07:17:00+00:00", 1_790_000, 1_790_000, 1),
    ],
    "weeks": [("2026-08-23T16:00:00+00:00", 180, 12, 60, 40)],
    "alerts_sent": [("2026-08-23T16:00:00+00:00", "approaching", "2026-08-27T07:17:00+00:00")],
    "deadline_notices": [(2, "2026-08-31T07:17:00+00:00")],
}


def _v1_database(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_PATH.read_text())
    for table, rows in V1_ROWS.items():
        marks = ",".join("?" * len(rows[0]))
        conn.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_triage_week', '2026-08-23T16:00:00+00:00')")
    conn.commit()
    return conn


def _dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    out = {}
    for table in V1_ROWS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        added = {"episodes": ("platform", "source_key"), "weeks": ("overage_min",)}.get(table, ())
        v1_cols = [c for c in cols if c not in added]
        out[table] = [tuple(r) for r in conn.execute(f"SELECT {', '.join(v1_cols)} FROM {table} ORDER BY 1")]
    return out


@pytest.fixture
def v1(tmp_path):
    conn = _v1_database(tmp_path / "pop.db")
    yield conn
    conn.close()


def test_fixture_really_is_version_1(v1):
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    assert v1.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone() is None


def test_upgrade_preserves_every_pop_row_exactly(v1):
    before = _dump(v1)
    db.init_db(v1)        # production order: init_db then migrate
    db.migrate(v1)
    after = _dump(v1)
    # The one deliberate data change: migration 0003 zeroes carried debt (DR5.2).
    assert before["weeks"] == [("2026-08-23T16:00:00+00:00", 180, 12, 60, 40)]
    assert after["weeks"] == [("2026-08-23T16:00:00+00:00", 180, 0, 60, 40)]
    assert {t: r for t, r in after.items() if t != "weeks"} == {t: r for t, r in before.items() if t != "weeks"}
    # ...and adds overage_min, uncomputed until the allowance code records it.
    assert v1.execute("SELECT overage_min FROM weeks").fetchall()[0][0] is None
    assert v1.execute("SELECT value FROM meta WHERE key='last_triage_week'").fetchone()[0] == \
        "2026-08-23T16:00:00+00:00"


def test_upgrade_sets_version_and_creates_drbr_tables(v1):
    db.init_db(v1)
    db.migrate(v1)
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert db.SCHEMA_VERSION == 4
    assert "overage_min" in [r[1] for r in v1.execute("PRAGMA table_info(weeks)")]
    names = {r[0] for r in v1.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sources", "item_labels", "imports", "measurements", "category_measurements",
            "vault_captures", "vault_observations", "prompts_sent"} <= names


def test_existing_episodes_default_to_spotify(v1):
    db.migrate(v1)
    assert {r[0] for r in v1.execute("SELECT platform FROM episodes")} == {"spotify"}


def test_x_is_seeded_wasteful_and_nothing_else_is_labelled(v1):
    db.migrate(v1)
    rows = [tuple(r) for r in v1.execute("SELECT kind, source_key, label FROM sources")]
    assert rows == [("x", "x", "wasteful")]


def test_migrate_twice_is_a_noop(v1):
    db.migrate(v1)
    before = [tuple(r) for r in v1.execute("SELECT * FROM sources")]
    db.migrate(v1)
    db.init_db(v1)
    assert [tuple(r) for r in v1.execute("SELECT * FROM sources")] == before


def test_pop_behaviour_survives_upgrade(v1, tmp_path):
    """The migrated database still runs POP's gate and state machine."""
    db.migrate(v1)
    settings = config.test_settings(db_path=tmp_path / "pop.db")
    clk = clock.FrozenClock("2026-08-30T09:00:00+00:00")
    item = db.get_item(v1, 2)
    assert item.state == "promoted"
    db.roll_over_week(v1, clk, settings)
    assert db.get_item(v1, 2).state == "queued"
    with pytest.raises(db.IllegalTransition):
        db.set_state(v1, clk, 3, "queued")          # dropped stays terminal
    assert db.funnel_stats(v1, clk, settings).captured == 3   # item 1 still inside its lock


def test_fresh_database_reaches_same_schema_as_upgraded(tmp_path):
    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    fresh.row_factory = sqlite3.Row
    db.init_db(fresh)
    db.migrate(fresh)
    upgraded = _v1_database(tmp_path / "old.db")
    db.init_db(upgraded)
    db.migrate(upgraded)

    def schema(c):
        return sorted(tuple(r) for r in c.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"))

    assert schema(fresh) == schema(upgraded)


def test_failed_migration_rolls_back_whole_step(v1, monkeypatch, tmp_path):
    broken = tmp_path / "migrations"
    broken.mkdir()
    good = (db.MIGRATIONS_DIR / "0002_drbr.sql").read_text()
    (broken / "0002_drbr.sql").write_text(good + "\nCREATE TABLE sources (x);\n")  # duplicate -> error
    monkeypatch.setattr(db, "MIGRATIONS_DIR", broken)
    with pytest.raises(sqlite3.Error):
        db.migrate(v1)
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    assert v1.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone() is None
    assert "platform" not in [r[1] for r in v1.execute("PRAGMA table_info(episodes)")]
    monkeypatch.setattr(db, "MIGRATIONS_DIR", db.ROOT / "migrations")
    db.migrate(v1)                                   # retried whole, succeeds
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)


def test_failed_0003_rolls_back_whole_step_including_the_debt_reset(v1, monkeypatch, tmp_path):
    broken = tmp_path / "migrations"
    broken.mkdir()
    (broken / "0002_drbr.sql").write_text((db.MIGRATIONS_DIR / "0002_drbr.sql").read_text())
    good = (db.MIGRATIONS_DIR / "0003_no_debt.sql").read_text()
    (broken / "0003_no_debt.sql").write_text(good + "\nCREATE TABLE sources (x);\n")  # duplicate -> error
    monkeypatch.setattr(db, "MIGRATIONS_DIR", broken)
    with pytest.raises(sqlite3.Error):
        db.migrate(v1)
    # 0002 committed on its own; 0003 left nothing behind.
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    assert "overage_min" not in [r[1] for r in v1.execute("PRAGMA table_info(weeks)")]
    assert v1.execute("SELECT debt_min FROM weeks").fetchone()[0] == 12       # the UPDATE rolled back too
    monkeypatch.setattr(db, "MIGRATIONS_DIR", db.ROOT / "migrations")
    db.migrate(v1)                                   # retried whole, succeeds
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert tuple(v1.execute("SELECT debt_min, overage_min FROM weeks").fetchone()) == (0, None)


def test_migrating_a_v2_database_zeroes_debt_and_adds_overage(v1, monkeypatch):
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    db.init_db(v1)
    db.migrate(v1)                                   # stop at version 2, as the live database was
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    v1.execute("INSERT INTO weeks (week_start, allowance_min, debt_min, promoted_min, listened_min) "
               "VALUES ('2026-08-30T16:00:00+00:00', 180, 180, 0, 0)")
    v1.commit()
    assert [r[0] for r in v1.execute("SELECT debt_min FROM weeks ORDER BY 1")] == [12, 180]
    assert "overage_min" not in [r[1] for r in v1.execute("PRAGMA table_info(weeks)")]

    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    db.migrate(v1)
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    rows = [tuple(r) for r in v1.execute(
        "SELECT week_start, allowance_min, debt_min, promoted_min, listened_min, overage_min FROM weeks ORDER BY 1")]
    assert rows == [("2026-08-23T16:00:00+00:00", 180, 0, 60, 40, None),
                    ("2026-08-30T16:00:00+00:00", 180, 0, 0, 0, None)]
    with pytest.raises(sqlite3.IntegrityError):      # overage is never negative
        v1.execute("UPDATE weeks SET overage_min = -1")
    week = db._to_week(v1.execute("SELECT * FROM weeks ORDER BY 1 DESC").fetchone())
    assert week.effective_allowance_min == 180       # the full allowance, debt gone


def test_measurement_check_rejects_zero_standin(v1):
    db.migrate(v1)
    v1.execute("INSERT INTO imports (kind, received_at, parser_version, status) VALUES "
               "('coldturkey','2026-09-16T10:00:00+00:00','t','ok')")
    v1.execute("INSERT INTO sources (kind, source_key, display_name, first_seen_at) VALUES "
               "('web_domain','google.com','google.com','2026-09-16T10:00:00+00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        v1.execute("INSERT INTO measurements (import_id, source_id, device, provenance, period_start, "
                   "period_end, minutes, status) VALUES (1, 2, 'desktop', 'manual_export', "
                   "'2026-09-13', '2026-09-13', 0, 'unreadable')")


# --- 0004: desktop apps (DR5.3), tables rebuilt to widen CHECKs ----------------------

DRBR_TABLES = ("sources", "item_labels", "imports", "measurements", "category_measurements",
               "vault_captures", "vault_observations", "prompts_sent")
T0 = "2026-09-16T10:00:00+00:00"


def _schema(c):
    return sorted(tuple(r) for r in c.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"))


def _v3_with_drbr_rows(v1, monkeypatch):
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    db.init_db(v1)
    db.migrate(v1)
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    v1.executescript(f"""
        INSERT INTO sources (id, kind, source_key, display_name, label, labelled_at, first_seen_at, prompted_at)
        VALUES (2, 'web_domain', 'google.com', 'google.com', NULL, NULL, '{T0}', '{T0}'),
               (3, 'youtube_channel', 'UCabc', 'Chan', 'worthwhile', '{T0}', '{T0}', NULL);
        INSERT INTO item_labels VALUES ('youtube:vid1', 'worthwhile', '{T0}', 'capture');
        INSERT INTO imports (id, kind, received_at, raw_path, raw_sha256, parser_version, status, period_start,
                             period_end, warnings, error, meta)
        VALUES (1, 'coldturkey', '{T0}', 'coldturkey/a.json', 'aa', 'ct-1', 'ok', '2026-09-13', '2026-09-15',
                '[]', NULL, '{{"origin": "local_db"}}'),
               (2, 'vault_scan', '{T0}', NULL, NULL, 'v-1', 'partial', NULL, NULL, '["w"]', NULL, '{{}}'),
               (3, 'screentime', '{T0}', 'screentime/b.png', 'bb', 's-1', 'failed', NULL, NULL, '[]', 'bad', '{{}}');
        INSERT INTO measurements (id, import_id, source_id, device, provenance, item_key, item_title, period_start,
                                  period_end, minutes, status, detail)
        VALUES (7, 1, 2, 'desktop', 'manual_export', 'google.com', 'google.com', '2026-09-14', '2026-09-14', 1.5,
                'ok', '{{}}'),
               (8, 1, 3, 'account', 'page_extraction', 'vid1', 'A video', '2026-09-14', '2026-09-14', NULL,
                'duration_missing', '{{}}');
        INSERT INTO category_measurements (import_id, category, period_start, period_end, minutes)
        VALUES (1, 'Social', '2026-09-13', '2026-09-19', 12.0);
        INSERT INTO vault_captures VALUES ('cap-1', '{T0}', 'x', 'x', 1, 'https://x.com/a', 'T', '01-Inbox/a.md',
                                           'sha', '{T0}');
        INSERT INTO vault_observations (import_id, capture_id, scanned_at, outcome, rel_path, edited, copies)
        VALUES (2, 'cap-1', '{T0}', 'kept', 'notes/a.md', 1, 1);
        INSERT INTO prompts_sent VALUES ('coldturkey', '2026-09-13', '2026-09-21', '{T0}'),
                                        ('diagnostic_end', '2026-09-13', '2026-11-07', '{T0}');
    """)
    v1.commit()
    monkeypatch.setattr(db, "SCHEMA_VERSION", 4)


def _all_rows(c):
    return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in (*DRBR_TABLES, *V1_ROWS, "meta") if t != "meta"} | {
        "meta": [tuple(r) for r in c.execute("SELECT * FROM meta WHERE key != 'schema_version' ORDER BY 1")]}


def test_v3_to_v4_keeps_every_row_and_matches_a_fresh_schema(v1, monkeypatch, tmp_path):
    _v3_with_drbr_rows(v1, monkeypatch)
    before = _all_rows(v1)
    assert all(before[t] for t in DRBR_TABLES)                 # rows in every DRBR table
    db.migrate(v1)
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    assert _all_rows(v1) == before
    assert v1.execute("PRAGMA foreign_key_check").fetchall() == []
    assert v1.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert v1.execute("SELECT name FROM sqlite_master WHERE name LIKE '%\\_new' ESCAPE '\\'").fetchall() == []

    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    fresh.row_factory = sqlite3.Row
    db.init_db(fresh)
    db.migrate(fresh)
    assert _schema(fresh) == _schema(v1)
    indexes = {r[0] for r in v1.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_imports_kind", "idx_imports_sha", "uq_measurements_fact", "idx_measurements_period"} <= indexes
    # The natural key still holds after the rebuild.
    with pytest.raises(sqlite3.IntegrityError):
        v1.execute("INSERT INTO measurements (import_id, source_id, device, provenance, item_key, period_start, "
                   "period_end, minutes) VALUES (1, 2, 'desktop', 'manual_export', 'google.com', '2026-09-14', "
                   "'2026-09-14', 2.0)")
    # ...and so do the foreign keys, enforced again.
    with pytest.raises(sqlite3.IntegrityError):
        v1.execute("INSERT INTO measurements (import_id, source_id, device, provenance, period_start, period_end, "
                   "minutes) VALUES (999, 2, 'desktop', 'app_tracker', '2026-09-14', '2026-09-14', 1.0)")


def test_v1_to_v4_reaches_the_same_schema_as_v3_to_v4(v1, monkeypatch, tmp_path):
    straight = _v1_database(tmp_path / "straight.db")
    db.init_db(straight)
    db.migrate(straight)
    _v3_with_drbr_rows(v1, monkeypatch)
    db.migrate(v1)
    assert _schema(straight) == _schema(v1)


def test_v4_accepts_the_new_values_and_still_rejects_garbage(v1):
    db.init_db(v1)
    db.migrate(v1)
    good = [
        f"INSERT INTO sources (id, kind, source_key, display_name, first_seen_at) VALUES "
        f"(50, 'desktop_app', 'com.tinyspeck.slackmacgap', 'Slack', '{T0}')",
        f"INSERT INTO imports (id, kind, received_at, parser_version, status) VALUES (50, 'qbserve', '{T0}', 'q', 'ok')",
        "INSERT INTO measurements (import_id, source_id, device, provenance, period_start, period_end, minutes) "
        "VALUES (50, 50, 'desktop', 'app_tracker', '2026-09-14', '2026-09-14', 2.5)",
        f"INSERT INTO prompts_sent VALUES ('qbserve_stale', '2026-09-18', '2026-09-18', '{T0}')",
    ]
    for sql in good:
        v1.execute(sql)
    bad = [
        f"INSERT INTO sources (kind, source_key, display_name, first_seen_at) VALUES ('garbage', 'k', 'k', '{T0}')",
        f"INSERT INTO imports (kind, received_at, parser_version, status) VALUES ('garbage', '{T0}', 'q', 'ok')",
        "INSERT INTO measurements (import_id, source_id, device, provenance, period_start, period_end, minutes) "
        "VALUES (50, 50, 'desktop', 'garbage', '2026-09-15', '2026-09-15', 2.5)",
        f"INSERT INTO prompts_sent VALUES ('garbage', '2026-09-18', '2026-09-18', '{T0}')",
    ]
    for sql in bad:
        with pytest.raises(sqlite3.IntegrityError):
            v1.execute(sql)


def _broken_0004(tmp_path, tail: str):
    broken = tmp_path / "migrations"
    broken.mkdir()
    for name in ("0002_drbr.sql", "0003_no_debt.sql"):
        (broken / name).write_text((db.MIGRATIONS_DIR / name).read_text())
    good = (db.MIGRATIONS_DIR / "0004_desktop_apps.sql").read_text()
    (broken / "0004_desktop_apps.sql").write_text(good + "\n" + tail + "\n")
    return broken


def _assert_still_v3(v1, before):
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    assert _all_rows(v1) == before
    assert v1.execute("SELECT name FROM sqlite_master WHERE name LIKE '%\\_new' ESCAPE '\\'").fetchall() == []
    assert v1.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):              # the old, narrower CHECK is still in place
        v1.execute(f"INSERT INTO sources (kind, source_key, display_name, first_seen_at) "
                   f"VALUES ('desktop_app', 'a', 'a', '{T0}')")


def test_failed_0004_rolls_back_whole(v1, monkeypatch, tmp_path):
    _v3_with_drbr_rows(v1, monkeypatch)
    before = _all_rows(v1)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", _broken_0004(tmp_path, "CREATE TABLE sources (x);"))
    with pytest.raises(sqlite3.Error):
        db.migrate(v1)
    _assert_still_v3(v1, before)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", db.ROOT / "migrations")
    db.migrate(v1)                                   # retried whole, succeeds
    assert v1.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    assert _all_rows(v1) == before


def test_0004_leaving_a_foreign_key_violation_rolls_back_whole(v1, monkeypatch, tmp_path):
    _v3_with_drbr_rows(v1, monkeypatch)
    before = _all_rows(v1)
    orphan = ("INSERT INTO measurements (import_id, source_id, device, provenance, period_start, period_end, "
              "minutes) VALUES (999, 2, 'desktop', 'app_tracker', '2026-09-14', '2026-09-14', 1.0);")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", _broken_0004(tmp_path, orphan))
    with pytest.raises(db.MigrationError, match="foreign key"):
        db.migrate(v1)
    _assert_still_v3(v1, before)
