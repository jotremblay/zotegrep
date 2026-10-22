import json

from zotegrep import config, search, store
from zotegrep.config import Library
from zotegrep.sync import SyncRunner, ZoteroSync

from conftest import FakeZotero, make_pdf

LIB = Library(id=1, type="user", zotero_id="123", api_key="KEY")
RSQUO = chr(0x2019)


def run(zot: FakeZotero) -> list[str]:
    messages: list[str] = []
    zot.calls.clear()
    ZoteroSync(LIB, client=zot).sync(progress_cb=messages.append)
    return messages


def meta(name: str) -> str | None:
    conn = store.get_db()
    try:
        return store.get_meta(conn, f"{name}_L{LIB.id}")
    finally:
        conn.close()


def indexed() -> dict[str, int]:
    conn = store.get_db()
    try:
        return {r["zotero_key"]: r["indexed"] for r in conn.execute("SELECT zotero_key, indexed FROM items")}
    finally:
        conn.close()


def test_full_sync_indexes_pdfs_metadata_notes_and_annotations(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["Intro"], [f"The model{RSQUO}s output on page two."]),
                  title="Paper A", date="2021-05-01", abstractNote="An abstract.",
                  creators=[{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
                  collections=["COLL0001"], tags=[{"tag": "toread"}])
    zot.collections_data = [{"key": "COLL0001", "name": "Reading list"}]
    zot.tag_colors = [{"name": "toread", "color": "#FF0000"}]
    zot.notes = [
        {"key": "NOTE0001", "itemType": "note", "parentItem": "PAAAA0001", "note": "<p>my own remark</p>"},
        {"key": "ANNO0001", "itemType": "annotation", "parentItem": "AAAA0001",
         "annotationText": "highlighted sentence", "annotationPosition": json.dumps({"pageIndex": 1})},
    ]
    assert run(zot)[-1] == "Sync complete"
    assert indexed() == {"AAAA0001": store.INDEX_VERSION}
    assert meta("lib_version") == "user:123:1"

    item = store.get_item("AAAA0001")
    assert (item["title"], item["date"], item["authors"]) == ("Paper A", "2021", "Lovelace, A.")
    hits = {m["kind"]: m["page"] for m in search.search("model's").groups[0].matches}
    assert hits == {"page": 2}
    assert search.search("remark").groups[0].matches[0]["kind"] == "note"
    assert search.search("highlighted").groups[0].matches[0] == {
        "page": 2, "kind": "annotation", "match": "highlighted", "snippet": "highlighted sentence"}
    facets = store.get_facets()
    assert facets["collections"][0]["name"] == "Reading list"
    assert facets["tags"] == [{"name": "toread", "count": 1, "color": "#FF0000"}]


def test_unchanged_library_is_not_listed_again(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["text"]))
    run(zot)
    assert run(zot)[-1] == "Everything up to date"
    assert zot.calls == []


def test_file_not_uploaded_yet_waits_without_failing(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["ready"]))
    zot.add_paper("BBBB0002", None)            # Zotero has the item, not the file yet
    assert run(zot)[-1] == "Sync complete (1 PDF not uploaded to Zotero yet)"
    assert meta("lib_version") == "user:123:1"   # still recorded: no full relisting next time

    assert run(zot)[-1] == "Everything up to date (1 PDF not uploaded to Zotero yet)"
    assert zot.calls == ["file:BBBB0002"]       # only the missing file is retried

    zot.files["BBBB0002"] = make_pdf(["arrived"])
    assert run(zot)[-1] == "Everything up to date"
    assert set(indexed()) == {"AAAA0001", "BBBB0002"}
    assert json.loads(meta("waiting_files")) == []


def test_real_failure_forces_a_full_sync_next_time(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["text"]))
    zot.broken = {"AAAA0001"}
    assert run(zot)[-1] == "Sync complete (1 failed)"
    assert meta("lib_version") is None
    zot.broken = set()
    assert run(zot)[-1] == "Sync complete"
    assert "top" in zot.calls


def test_linked_files_are_skipped(home):
    zot = FakeZotero()
    zot.add_paper("LINK0001", None, link_mode="linked_file")
    assert run(zot)[-1] == "Sync complete"
    assert "file:LINK0001" not in zot.calls


def test_deleted_items_are_removed_with_their_file(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["text"]))
    run(zot)
    assert store.get_pdf_path("AAAA0001").exists()
    path = store.get_pdf_path("AAAA0001")
    zot.version = 2
    zot.attachments.clear()
    run(zot)
    assert indexed() == {}
    assert not path.exists()


def test_duplicate_files_are_indexed_once(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["same file"]), md5="abc")
    zot.add_paper("BBBB0002", make_pdf(["same file"]), md5="abc")
    zot.notes = [{"key": "ANNO0001", "itemType": "annotation", "parentItem": "BBBB0002",
                  "annotationText": "on the duplicate"}]
    run(zot)
    assert list(indexed()) == ["AAAA0001"]
    assert search.search("duplicate").groups[0].zotero_key == "AAAA0001"


def test_old_index_format_is_rebuilt_from_disk(home):
    zot = FakeZotero()
    zot.add_paper("AAAA0001", make_pdf(["text"]))
    run(zot)
    conn = store.get_db()
    conn.execute("UPDATE items SET indexed=0")
    conn.commit()
    conn.close()
    run(zot)
    assert indexed() == {"AAAA0001": store.INDEX_VERSION}
    assert not any(c.startswith("file:") for c in zot.calls)


def test_runner_stamps_last_sync_only_when_everything_worked(home):
    class Offline(FakeZotero):
        def last_modified_version(self):
            raise RuntimeError("\nCode: 503\nResponse: Zotero is offline")

    runner = SyncRunner()
    assert runner.run([LIB], client_factory=lambda lib: Offline())
    assert runner.state["message"] == "Error: Code: 503 - Response: Zotero is offline"
    assert store.get_stats()["last_sync"] is None

    assert runner.run([LIB], client_factory=lambda lib: FakeZotero())
    assert store.get_stats()["last_sync"] is not None
    assert not runner.run([], client_factory=lambda lib: FakeZotero())
