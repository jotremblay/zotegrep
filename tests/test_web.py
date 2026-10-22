import json
import time

import pytest
from fastapi.testclient import TestClient

from zotegrep import config, store
from zotegrep.sync import SyncRunner
from zotegrep.web import LOOPBACK_HOSTS, create_app

from conftest import add_paper


@pytest.fixture
def client(home, monkeypatch):
    started = []
    monkeypatch.setattr(SyncRunner, "run", lambda self, libraries, *a, **k: started.append(libraries))
    app = create_app(allowed_hosts=LOOPBACK_HOSTS | {"testserver"}, auto_sync=False)
    with TestClient(app) as c:
        c.sync_started = started
        yield c


def test_state_and_library_settings(client):
    assert client.get("/api/state").json()["has_zotero"] is False

    r = client.post("/api/libraries", json={"zotero_library_type": "user", "zotero_library_id": "abc",
                                             "zotero_api_key": "KEY"})
    assert r.status_code == 400 and "number" in r.json()["error"]

    r = client.post("/api/libraries", json={"zotero_library_type": "group", "zotero_library_id": "42",
                                             "zotero_api_key": "SECRETKEY", "name": "Lab"})
    assert r.status_code == 200
    for _ in range(100):   # the sync starts in a background thread
        if client.sync_started:
            break
        time.sleep(0.01)
    assert client.sync_started, "adding a library starts a sync"
    state = client.get("/api/state").json()
    assert state["libraries"] == [{"id": 1, "name": "Lab", "label": "Lab",
                                   "zotero_library_id": "42", "zotero_library_type": "group"}]
    assert "SECRETKEY" not in json.dumps(state)                     # the key never reaches the browser
    assert config.load().libraries[0].api_key == "SECRETKEY"

    assert client.put("/api/libraries/1", json={"name": "Team"}).status_code == 200
    assert config.load().libraries[0].name == "Team"
    assert client.post("/api/settings", json={"sync_interval": 3}).status_code == 200
    assert config.load().sync_interval == 10                        # clamped to the minimum
    assert client.delete("/api/libraries/1").status_code == 200
    assert config.load().libraries == []


def test_streamed_search(client):
    add_paper("AAAA0001", ["the needle in the haystack"], title="Haystacks")
    r = client.get("/api/search/stream", params={"q": "needle"})
    events = [json.loads(line) for line in r.text.splitlines()]
    assert events[0] == {"type": "start", "total": 1}
    doc = next(e["doc"] for e in events if e["type"] == "doc")
    assert doc["zotero_key"] == "AAAA0001" and doc["matches"][0]["page"] == 1
    assert events[-1]["type"] == "done" and events[-1]["count"] == 1

    full = client.get("/api/search", params={"q": "needle", "key": "AAAA0001"}).json()
    assert full["grouped"][0]["match_count"] == 1
    assert client.get("/api/search/stream", params={"q": ""}).status_code == 400


def test_pdf_and_item_endpoints(client):
    add_paper("AAAA0001", ["text"], title="A paper")
    assert client.get("/api/pdf/AAAA0001").status_code == 404     # indexed but no file
    path = store.pdf_path(1, "AAAA0001")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 fake")
    r = client.get("/api/pdf/AAAA0001")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert client.get("/api/pdf/..%2Fconfig").status_code in (400, 404)
    assert client.get("/api/item/AAAA0001").json()["title"] == "A paper"


def test_requests_from_elsewhere_are_refused(client):
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 403
    r = client.post("/api/settings", json={"sync_interval": 60}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/settings", json={"sync_interval": 60}, headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_static_pages(client):
    assert "ZoteGrep" in client.get("/").text
    assert client.get("/viewer").status_code == 200
    assert client.get("/static/pdfjs/pdf.min.mjs").headers["content-type"].startswith("text/javascript")
