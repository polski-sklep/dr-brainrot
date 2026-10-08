"""The launchd agents: each one is installed by ./drbr and listed in the README.

And the Qbserve keeper (DR5.27) does exactly one thing: reopen Qbserve in the
background when no process of that name is running. It never touches
Qbserve's data and runs no code from this repository (DR5.14). Its command is
run here with stand-ins for pgrep and open, so no real app is ever opened.
"""
from __future__ import annotations

import pathlib
import plistlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLISTS = sorted((ROOT / "launchd").glob("*.plist"))
KEEPER = ROOT / "launchd" / "com.jacob.drbr.qbserve.plist"


def load(path: pathlib.Path) -> dict:
    with path.open("rb") as f:
        return plistlib.load(f)


def drbr_agents() -> list[str]:
    m = re.search(r"^AGENTS=\(([^)]*)\)", (ROOT / "drbr").read_text(), re.M)
    assert m, "./drbr has no AGENTS=( ... ) list"
    return m.group(1).split()


def readme_agent_table() -> str:
    text = (ROOT / "README.md").read_text()
    return text.split("## Start everything", 1)[1].split("\n## ", 1)[0]


def test_every_agent_parses_is_named_after_its_file_and_is_installed_and_documented():
    assert PLISTS
    table = readme_agent_table()
    for path in PLISTS:
        assert load(path)["Label"] == path.stem, path.name
        assert re.search(rf"^\| `{re.escape(path.stem)}` \|", table, re.M), f"{path.stem} missing from README"
    assert sorted(drbr_agents()) == sorted(p.stem for p in PLISTS)


def test_the_qbserve_keeper_checks_every_ten_minutes_and_at_login():
    plist = load(KEEPER)
    shell, flag, command = plist["ProgramArguments"]
    assert (shell, flag) == ("/bin/sh", "-c")
    assert command.startswith("/usr/bin/pgrep -xq Qbserve || ")
    assert "/usr/bin/open -g -j -a Qbserve" in command
    assert plist["StartInterval"] == 600 and plist["RunAtLoad"] is True
    assert "KeepAlive" not in plist and "StartCalendarInterval" not in plist
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == "/Users/Jacob/Projects/drbr/logs/qbserve.log"


def test_the_qbserve_keeper_never_touches_qbserves_data_or_runs_repository_code():
    plist = load(KEEPER)
    values = " ".join(str(v) for v in [*plist["ProgramArguments"], *plist.values()])
    assert ".venv" not in values and ".py" not in values and "WorkingDirectory" not in plist
    assert "Application Support" not in KEEPER.read_text()


@pytest.fixture
def stand_ins(tmp_path):
    """pgrep and open replaced by scripts that record their arguments."""
    calls = tmp_path / "calls"

    def write(name: str, body: str) -> pathlib.Path:
        path = tmp_path / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n{body}\n')
        path.chmod(0o755)
        return path

    def run(qbserve_running: bool) -> tuple[str, list[str]]:
        pgrep = write("pgrep", f"exit {0 if qbserve_running else 1}")
        opener = write("open", "exit 0")
        command = load(KEEPER)["ProgramArguments"][2]
        command = command.replace("/usr/bin/pgrep", str(pgrep)).replace("/usr/bin/open", str(opener))
        out = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=10, check=True)
        lines = calls.read_text().splitlines() if calls.exists() else []
        calls.unlink(missing_ok=True)
        return out.stdout, lines

    return run


def test_the_keeper_does_nothing_while_qbserve_runs(stand_ins):
    stdout, calls = stand_ins(qbserve_running=True)
    assert (stdout, calls) == ("", ["pgrep -xq Qbserve"])


def test_the_keeper_reopens_a_stopped_qbserve_hidden_and_logs_one_line(stand_ins):
    stdout, calls = stand_ins(qbserve_running=False)
    assert calls == ["pgrep -xq Qbserve", "open -g -j -a Qbserve"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d reopening Qbserve\n", stdout)
