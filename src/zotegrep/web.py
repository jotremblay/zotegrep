"""Local web interface: the search page, the PDF viewer and their JSON API.

There are no accounts: the server is meant to listen on this machine only,
and it refuses requests that do not come from it (see LocalOnlyMiddleware).
"""
import re
import json
import time
import asyncio
import logging
import mimetypes
import threading
from pathlib import Path
from dataclasses import asdict
from urllib.parse import urlsplit
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from starlette.datastructures import Headers, MutableHeaders

from zotegrep import __version__, config, search, store
from zotegrep.config import ConfigError, Library
from zotegrep.sync import SyncRunner
from zotegrep.text import strip_html

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_QUERY_LEN = 500
ZOTERO_KEY_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
PERIODIC_SYNC_TICK = 60
MAX_CONCURRENT_SEARCHES = 4
# Snippets per paper in the result stream; the rest loads on demand
# ("Show all", /api/search?key=...).
STREAM_SNIPPETS_PER_DOC = 30
ZOTERO_API = "https://api.zotero.org"
CITATION_STYLE = "apa"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# PDF.js ES modules (static/pdfjs/*.mjs) only load with a JavaScript type,
# which `mimetypes` does not know on every system.
mimetypes.add_type("text/javascript", ".mjs")


class SecurityHeadersMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware, which sits in the middle of response
    bodies): result streaming and client disconnect detection, which the
    cancellation of a search relies on, stay intact."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LocalOnlyMiddleware:
    """Without accounts, the server trusts whoever reaches it, so requests
    must come from a browser on this machine: the Host header has to be a
    loopback name (defeats DNS rebinding) and writes must come from the page
    itself (defeats other websites posting to localhost)."""

    def __init__(self, app, allowed_hosts: frozenset[str] | None):
        self.app = app
        self.allowed_hosts = allowed_hosts   # None: any host (--host 0.0.0.0)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            host = headers.get("host", "")
            if self.allowed_hosts is not None and _hostname(host) not in self.allowed_hosts:
                return await JSONResponse({"error": "Host not allowed"}, 403)(scope, receive, send)
            origin = headers.get("origin")
            if scope["method"] not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != host:
                return await JSONResponse({"error": "Cross-origin request refused"}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


def _hostname(host_header: str) -> str:
    try:
        return (urlsplit("//" + host_header).hostname or "").lower()
    except ValueError:
        return ""


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _seconds_since_last_sync(runner: SyncRunner) -> float:
    """Time since the last attempt in this process or, right after startup,
    since the last complete sync recorded in the database."""
    if runner.last_attempt is not None:
        return time.monotonic() - runner.last_attempt
    last = store.get_stats()["last_sync"]
    if not last:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return float("inf")


def _zotero_links(item: dict, libraries: dict[int, Library]) -> dict:
    """zotero:// (desktop app) and zotero.org links to the parent item."""
    lib = libraries.get(item.get("library_id"))
    if not lib:
        return {"zotero_select": "", "zotero_web": ""}
    key = item.get("parent_key") or item["zotero_key"]
    if lib.type == "group":
        return {
            "zotero_select": f"zotero://select/groups/{lib.zotero_id}/items/{key}",
            "zotero_web": f"https://www.zotero.org/groups/{lib.zotero_id}/items/{key}",
        }
    return {"zotero_select": f"zotero://select/library/items/{key}", "zotero_web": ""}


def _libraries_by_id() -> dict[int, Library]:
    return {lib.id: lib for lib in config.load().libraries}


def _public_doc(g: search.DocMatches, libraries: dict[int, Library]) -> dict:
    doc = asdict(g)
    del doc["clause_tf"], doc["title_hit"]
    doc.update(_zotero_links(doc, libraries))
    return doc


class SearchRequest:
    """Parameters shared by /api/search and /api/search/stream."""

    def __init__(
        self,
        q: str = Query("", max_length=MAX_QUERY_LEN),
        case: bool = Query(False),
        regex: bool = Query(False),
        scope: str = Query("doc"),
        library: int | None = Query(None),
        collection: str | None = Query(None),
        tag: list[str] = Query([]),
        year_from: int | None = Query(None),
        year_to: int | None = Query(None),
        author: str = Query("", max_length=100),
    ):
        self.q, self.case, self.regex, self.scope = q, case, regex, scope
        self.collection = collection
        self.filters = search.Filters(
            library_id=library, collection=collection, tags=tag[:20],
            year_from=year_from, year_to=year_to, author=author,
        )

    def error(self) -> str | None:
        if self.collection is not None and not ZOTERO_KEY_RE.match(self.collection):
            return "Invalid collection"
        if not self.q.strip() and not self.filters.active():
            return "Type a query or pick a filter"
        return None

    def kwargs(self) -> dict:
        return {"case_sensitive": self.case, "use_regex": self.regex,
                "scope": self.scope, "filters": self.filters}


def create_app(allowed_hosts: frozenset[str] | None = LOOPBACK_HOSTS, auto_sync: bool = True) -> FastAPI:
    runner = SyncRunner()
    search_slots = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    def start_sync() -> bool:
        if runner.running:
            return False
        libraries = config.load().libraries
        if not libraries:
            return False
        threading.Thread(target=runner.run, args=(libraries,), daemon=True).start()
        return True

    async def periodic_sync():
        while True:
            try:
                interval = config.load().sync_interval
                if interval > 0 and not runner.running and _seconds_since_last_sync(runner) >= interval * 60:
                    logger.info("Periodic sync")
                    start_sync()
            except Exception:
                logger.exception("Periodic sync check failed")
            await asyncio.sleep(PERIODIC_SYNC_TICK)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.init_db()
        task = asyncio.create_task(periodic_sync()) if auto_sync else None
        yield
        if task:
            task.cancel()
        runner.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(LocalOnlyMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.sync_runner = runner

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/viewer")
    async def viewer():
        return FileResponse(STATIC_DIR / "viewer.html")

    # ------------------------------------------------------------ configuration

    def state_payload() -> dict:
        cfg = config.load()
        return {
            "version": __version__,
            "has_zotero": bool(cfg.libraries),
            "libraries": [lib.public() for lib in cfg.libraries],
            "settings": {"sync_interval": cfg.sync_interval},
        }

    @app.get("/api/state")
    async def get_state():
        return state_payload()

    @app.post("/api/settings")
    async def save_settings(request: Request):
        body = await _json_body(request)
        cfg = config.load()
        try:
            cfg.set_sync_interval(int(body.get("sync_interval", config.DEFAULT_SYNC_INTERVAL)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid sync interval"}, 400)
        config.save(cfg)
        return {"ok": True}

    @app.post("/api/libraries")
    async def add_library(request: Request):
        body = await _json_body(request)
        cfg = config.load()
        try:
            lib = cfg.add_library(
                str(body.get("zotero_library_type", "user")), str(body.get("zotero_library_id", "")),
                str(body.get("zotero_api_key", "")), str(body.get("name", "")),
            )
        except ConfigError as e:
            return JSONResponse({"error": str(e)}, 400)
        config.save(cfg)
        start_sync()
        return {"ok": True, "id": lib.id}

    @app.put("/api/libraries/{library_id}")
    async def update_library(library_id: int, request: Request):
        body = await _json_body(request)
        cfg = config.load()
        try:
            cfg.update_library(library_id, name=str(body.get("name", "")),
                               api_key=str(body.get("zotero_api_key", "")) or None)
        except ConfigError as e:
            return JSONResponse({"error": str(e)}, 404 if "not found" in str(e) else 400)
        config.save(cfg)
        return {"ok": True}

    @app.delete("/api/libraries/{library_id}")
    async def delete_library(library_id: int):
        if runner.running:
            return JSONResponse({"error": "A sync is running, cancel it first"}, 409)
        cfg = config.load()
        try:
            cfg.remove_library(library_id)
        except ConfigError as e:
            return JSONResponse({"error": str(e)}, 404)
        config.save(cfg)
        await asyncio.to_thread(store.delete_library_data, library_id)
        return {"ok": True}

    # ------------------------------------------------------------ search

    @app.get("/api/search")
    async def api_search(params: SearchRequest = Depends(), key: str | None = Query(None)):
        if key is not None and not ZOTERO_KEY_RE.match(key):
            return JSONResponse({"error": "Invalid key"}, 400)
        err = params.error()
        if err:
            return {"error": err, "count": 0, "grouped": []}
        try:
            # In a thread: the search is synchronous and would otherwise block
            # the event loop while it runs.
            async with search_slots:
                outcome = await asyncio.to_thread(search.search, params.q, zotero_key=key, **params.kwargs())
        except search.SearchError as e:
            return {"error": str(e), "count": 0, "grouped": []}
        except Exception:
            logger.exception("Search failed")
            return {"error": "Search failed", "count": 0, "grouped": []}
        libraries = _libraries_by_id()
        return {
            "count": outcome.count, "truncated": outcome.truncated,
            "grouped": [_public_doc(g, libraries) for g in outcome.groups],
        }

    @app.get("/api/search/stream")
    async def api_search_stream(params: SearchRequest = Depends()):
        """Same search as NDJSON, each paper sent as soon as it is found:

            {"type": "start", "total": N}             papers to examine
            {"type": "doc", "doc": {...}}             a paper (provisional score)
            {"type": "progress", "done": k}           papers examined so far
            {"type": "done", "count", "papers", "truncated", "scores": {key: score}}
            {"type": "error", "error": "..."}

        If the client drops the connection (a new search started), the search
        stops on the server at the next paper."""
        err = params.error()
        if err:
            return JSONResponse({"error": err}, 400)

        libraries = _libraries_by_id()
        cancel = threading.Event()

        def line(event: dict) -> bytes:
            return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

        def produce():
            start, groups = None, []
            try:
                for event in search.iter_search(
                    params.q, doc_cap=STREAM_SNIPPETS_PER_DOC, cancel=cancel, **params.kwargs()
                ):
                    if isinstance(event, search.SearchStart):
                        start = event
                        yield line({"type": "start", "total": event.total_docs})
                    elif isinstance(event, search.DocMatches):
                        groups.append(event)
                        yield line({"type": "doc", "doc": _public_doc(event, libraries)})
                    else:
                        yield line({"type": "progress", "done": event})
                if cancel.is_set():
                    return
                if start and start.clauses:
                    search.finalize_scores(groups, start)
                yield line({
                    "type": "done",
                    "count": sum(g.match_count for g in groups),
                    "papers": len(groups),
                    "truncated": any(len(g.matches) < g.match_count for g in groups),
                    "scores": {g.zotero_key: g.score for g in groups},
                })
            except search.SearchError as e:
                yield line({"type": "error", "error": str(e)})
            except Exception:
                logger.exception("Search failed")
                yield line({"type": "error", "error": "Search failed"})

        producer = produce()

        def close_producer():
            # Close the abandoned generator to release its SQLite connection
            # right away. If it is busy with a paper in the threadpool, it
            # returns right after (it sees `cancel`): retry briefly.
            for _ in range(100):
                try:
                    producer.close()
                    return
                except ValueError:
                    time.sleep(0.05)

        async def stream():
            try:
                async with search_slots:
                    async for chunk in iterate_in_threadpool(producer):
                        yield chunk
            finally:
                # Client gone (new search started) or normal end.
                cancel.set()
                threading.Thread(target=close_producer, daemon=True).start()

        return StreamingResponse(stream(), media_type="application/x-ndjson", headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",  # nginx: do not hold lines in its buffer
        })

    @app.get("/api/facets")
    async def api_facets():
        return await asyncio.to_thread(store.get_facets)

    @app.get("/api/no-text")
    async def api_no_text():
        return {"items": await asyncio.to_thread(store.get_no_text_items)}

    @app.get("/api/item/{zotero_key}")
    async def api_item(zotero_key: str):
        if not ZOTERO_KEY_RE.match(zotero_key):
            return JSONResponse({"error": "Invalid key"}, 400)
        item = store.get_item(zotero_key)
        if not item:
            return JSONResponse({"error": "Not found"}, 404)
        return {
            "zotero_key": zotero_key,
            "title": item["title"], "authors": item["authors"], "date": item["date"],
            "filename": item["filename"],
            "authors_full": item.get("authors_full") or "",
            "abstract": item.get("abstract") or "",
            **_zotero_links(item, _libraries_by_id()),
        }

    @app.get("/api/cite/{zotero_key}")
    async def api_cite(zotero_key: str, format: str = Query("bibtex")):
        """Citation (APA) or BibTeX of the parent item, generated by the Zotero API."""
        if not ZOTERO_KEY_RE.match(zotero_key) or format not in ("bibtex", "citation"):
            return JSONResponse({"error": "Invalid request"}, 400)
        item = store.get_item(zotero_key)
        lib = _libraries_by_id().get(item["library_id"]) if item else None
        if not item or not lib:
            return JSONResponse({"error": "Not found"}, 404)
        if not item["parent_key"]:
            return JSONResponse({"error": "This PDF has no parent item in Zotero, nothing to cite"}, 404)

        params = {"format": "bibtex"} if format == "bibtex" else \
            {"format": "bib", "style": CITATION_STYLE, "linkwrap": "0"}
        url = f"{ZOTERO_API}/{lib.type}s/{lib.zotero_id}/items/{item['parent_key']}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, params=params, headers={
                    "Zotero-API-Key": lib.api_key, "Zotero-API-Version": "3",
                })
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Citation fetch failed for %s: %s", zotero_key, e)
            return JSONResponse({"error": "Zotero API request failed"}, 502)
        text = r.text.strip() if format == "bibtex" else strip_html(r.text)
        return {"text": text}

    @app.get("/api/pdf/{zotero_key}")
    async def serve_pdf(zotero_key: str):
        if not ZOTERO_KEY_RE.match(zotero_key):
            return JSONResponse({"error": "Invalid key"}, 400)
        path = store.get_pdf_path(zotero_key)
        if not path:
            return JSONResponse({"error": "PDF not found"}, 404)
        return FileResponse(path, media_type="application/pdf")

    # ------------------------------------------------------------ sync

    @app.post("/api/sync")
    async def trigger_sync():
        if runner.running:
            return {"status": "already_running"}
        if not start_sync():
            return JSONResponse({"error": "No Zotero library configured"}, 400)
        return {"status": "started"}

    @app.post("/api/sync/cancel")
    async def cancel_sync():
        return {"status": "cancelling" if runner.cancel() else "not_running"}

    @app.get("/api/sync/status")
    async def get_sync_status():
        return {**runner.state, **await asyncio.to_thread(store.get_stats)}

    return app
