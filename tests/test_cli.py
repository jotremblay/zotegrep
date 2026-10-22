import json

from zotegrep import config
from zotegrep.cli import main

from conftest import add_paper


def zg(home, *args) -> int:
    return main(["--home", str(home), *args])


def test_library_commands(home, capsys):
    assert zg(home, "library", "add", "1234567", "--key", "ABCDEF", "--name", "Mine") == 0
    assert zg(home, "library", "add", "99", "--group", "--key", "ABCDEF") == 0
    assert zg(home, "library", "add", "1234567", "--key", "ABCDEF") == 2    # already there
    assert zg(home, "library", "add", "12x", "--key", "ABCDEF") == 2        # not a number
    capsys.readouterr()

    assert zg(home, "library", "list") == 0
    out = capsys.readouterr().out
    assert "#1  user   1234567  Mine" in out and "#2  group  99" in out
    assert "ABCDEF" not in out

    assert zg(home, "library", "edit", "2", "--name", "Lab") == 0
    assert config.load().library(2).name == "Lab"
    assert zg(home, "library", "remove", "2", "--yes") == 0
    assert [lib.id for lib in config.load().libraries] == [1]


def test_search_output(home, capsys):
    add_paper("AAAA0001", ["intro", "the needle sits on page two"], title="Haystacks", date="2024",
              authors="Doe, J.")
    add_paper("BBBB0002", ["nothing here"], title="Needles and pins")

    assert zg(home, "search", "needle", "--color", "never") == 0
    out = capsys.readouterr().out
    assert "Haystacks  1 match\n  2024 · Doe, J.\n      p. 2  the needle sits on page two\n" in out
    assert "Needles and pins  1 match\n  key BBBB0002" in out      # title match: no repeated snippet
    assert "2 matches in 2 papers" in out

    assert zg(home, "search", "needle", "--json") == 0
    docs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {d["key"] for d in docs} == {"AAAA0001", "BBBB0002"}
    assert next(d for d in docs if d["key"] == "AAAA0001")["matches"][0]["page"] == 2

    assert zg(home, "search", "needle", "-l", "--sort", "title") == 0
    assert capsys.readouterr().out.splitlines() == ["AAAA0001  Haystacks", "BBBB0002  Needles and pins"]

    assert zg(home, "search", "absent") == 1
    assert zg(home, "search", "(bad", "-E") == 2
    assert zg(home, "search", "needle", "--collection", "missing") == 2


def test_sync_without_library_is_an_error(home, capsys):
    assert zg(home, "sync") == 2
    assert "library add" in capsys.readouterr().err


def test_status(home, capsys):
    assert zg(home, "status") == 0
    out = capsys.readouterr().out
    assert str(home) in out and "PDFs indexed   0" in out
