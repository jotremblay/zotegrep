"""Command line interface: configure libraries, sync, search, serve the web UI."""
import os
import re
import sys
import json
import shutil
import getpass
import logging
import argparse
import threading

from zotegrep import __version__, config, search, store
from zotegrep.config import ConfigError

DEFAULT_PORT = 21824
ESC = chr(27)
# grep-like exit codes: 0 found, 1 nothing found, 2 error
EXIT_OK, EXIT_NO_MATCH, EXIT_ERROR = 0, 1, 2


class CliError(Exception):
    pass


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"{ESC}[{code}m{text}{ESC}[0m" if self.enabled and text else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def mark(self, text: str) -> str:
        return self._wrap("30;103", text)


def _color_enabled(choice: str) -> bool:
    if choice == "always":
        return True
    if choice == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # virtual terminal processing
    except Exception:
        pass


def _highlight(snippet: str, match: str, style: Style) -> str:
    if not style.enabled or not match:
        return snippet
    return re.sub(re.escape(match), lambda m: style.mark(m.group()), snippet, flags=re.IGNORECASE)


def _locator(m: dict) -> str:
    if m["kind"] == "page":
        return f"p. {m['page']}"
    if m["kind"] == "annotation":
        return f"annot. p. {m['page']}" if m["page"] else "annotation"
    return m["kind"]


class ProgressPrinter:
    """Sync messages on stderr; on a terminal, the per-file lines overwrite
    each other instead of scrolling."""
    _COUNTER_RE = re.compile(r"^(\[[^\]]*\] )?\[\d+/\d+\] ")

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.tty = sys.stderr.isatty()
        self._pending = False

    def __call__(self, msg: str):
        if self.quiet:
            return
        if self.tty and self._COUNTER_RE.match(msg):
            width = shutil.get_terminal_size((80, 20)).columns - 1
            sys.stderr.write("\r" + msg[:width].ljust(width))
            self._pending = True
        else:
            self.finish()
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    def finish(self):
        if self._pending:
            sys.stderr.write("\n")
            self._pending = False


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_library_add(args) -> int:
    cfg = config.load()
    api_key = args.key or getpass.getpass("Zotero API key (hidden): ")
    try:
        lib = cfg.add_library("group" if args.group else "user", args.zotero_id, api_key, args.name or "")
    except ConfigError as e:
        raise CliError(str(e))
    config.save(cfg)
    print(f"Added library #{lib.id}: {lib.label}")
    print("Run `zotegrep sync` to download and index its PDFs.")
    return EXIT_OK


def cmd_library_list(args) -> int:
    cfg = config.load()
    if not cfg.libraries:
        print("No library yet. Add one with: zotegrep library add <user ID> --key <API key>")
        return EXIT_OK
    for lib in cfg.libraries:
        name = f"  {lib.name}" if lib.name else ""
        print(f"#{lib.id}  {lib.type:<5}  {lib.zotero_id}{name}")
    return EXIT_OK


def cmd_library_edit(args) -> int:
    cfg = config.load()
    api_key = getpass.getpass("New Zotero API key (hidden): ") if args.key else None
    try:
        lib = cfg.update_library(args.id, name=args.name, api_key=api_key)
    except ConfigError as e:
        raise CliError(str(e))
    config.save(cfg)
    print(f"Library #{lib.id} saved: {lib.label}")
    return EXIT_OK


def cmd_library_remove(args) -> int:
    cfg = config.load()
    lib = cfg.library(args.id)
    if not lib:
        raise CliError(f"No library #{args.id} (see `zotegrep library list`)")
    if not args.yes:
        answer = input(f"Remove {lib.label} and delete its PDFs and index from this computer? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing removed.")
            return EXIT_OK
    cfg.remove_library(lib.id)
    config.save(cfg)
    store.init_db()
    store.delete_library_data(lib.id)
    print(f"Removed {lib.label} (nothing changes in Zotero).")
    return EXIT_OK


def cmd_sync(args) -> int:
    from zotegrep.sync import SyncRunner

    cfg = config.load()
    if not cfg.libraries:
        raise CliError("No Zotero library configured. Add one with: zotegrep library add <ID> --key <API key>")
    store.init_db()
    runner = SyncRunner()
    printer = ProgressPrinter(quiet=args.quiet)
    worker = threading.Thread(target=runner.run, args=(cfg.libraries, printer), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        printer.finish()
        print("Cancelling after the current file...", file=sys.stderr)
        runner.cancel()
        worker.join()
    printer.finish()
    return EXIT_ERROR if any("Error:" in msg for msg in runner.state["log"]) else EXIT_OK


def _resolve_collection(name: str | None) -> str | None:
    if not name:
        return None
    collections = store.get_facets()["collections"]
    if any(c["key"] == name for c in collections):
        return name
    wanted = name.casefold()
    exact = [c for c in collections if c["name"].casefold() == wanted]
    found = exact or [c for c in collections if wanted in c["name"].casefold()]
    if len(found) == 1:
        return found[0]["key"]
    if not found:
        raise CliError(f"No collection named {name!r}")
    names = ", ".join(sorted({c["name"] for c in found}))
    raise CliError(f"Several collections match {name!r}: {names}")


def _sort_groups(groups: list, order: str) -> list:
    if order == "matches":
        return sorted(groups, key=lambda g: -g.match_count)
    if order == "title":
        return sorted(groups, key=lambda g: (g.title or "").lower())
    if order == "date":
        return sorted(groups, key=lambda g: (g.date[:4] if g.date[:4].isdigit() else "0000"), reverse=True)
    return groups  # relevance: already sorted


def cmd_search(args) -> int:
    store.init_db()
    query = " ".join(args.query)
    filters = search.Filters(
        library_id=args.library, collection=_resolve_collection(args.collection), tags=args.tag or [],
        year_from=args.since, year_to=args.until, author=args.author or "",
    )
    if not query.strip() and not filters.active():
        raise CliError("Type a query, or pick a filter to list papers")
    try:
        outcome = search.search(query, case_sensitive=args.case, use_regex=args.regex,
                                scope=args.scope, filters=filters)
    except search.SearchError as e:
        raise CliError(str(e))

    groups = _sort_groups(outcome.groups, args.sort)
    shown = groups if args.limit == 0 else groups[:args.limit]
    libraries = {lib.id: lib for lib in config.load().libraries}

    if args.json:
        for g in shown:
            path = store.get_pdf_path(g.zotero_key)
            print(json.dumps({
                "key": g.zotero_key, "title": g.title, "authors": g.authors, "date": g.date,
                "publication": g.publication, "doi": g.doi, "url": g.url, "tags": g.tags,
                "score": g.score, "match_count": g.match_count,
                "pdf": str(path) if path else None,
                "zotero": _select_link(g, libraries),
                "matches": g.matches,
            }, ensure_ascii=False))
        return EXIT_OK if groups else EXIT_NO_MATCH

    style = Style(_color_enabled(args.color))
    if args.files_with_matches:
        for g in shown:
            print(f"{g.zotero_key}  {g.title}")
        return EXIT_OK if groups else EXIT_NO_MATCH

    if not groups:
        if not store.get_stats()["pdf_count"]:
            print("The index is empty: run `zotegrep sync` first.", file=sys.stderr)
        else:
            print("No match.", file=sys.stderr)
        return EXIT_NO_MATCH

    for g in shown:
        meta = " · ".join(p for p in (g.date, g.authors, g.publication) if p)
        count = f"{g.match_count} match{'es' if g.match_count != 1 else ''}" if query.strip() else ""
        # A match in the title is highlighted in the title rather than repeated as a snippet.
        title_hits = [m for m in g.matches if m["kind"] == "title"]
        title = g.title
        for m in title_hits:
            title = _highlight(title, m["match"], style)
        print(style.bold(title) + (f"  {style.red(count)}" if count else ""))
        if meta:
            print("  " + style.dim(meta))
        others = [m for m in g.matches if m["kind"] != "title"]
        snippets = others if args.snippets == 0 else others[:args.snippets]
        for m in snippets:
            print(f"  {style.dim(_locator(m).rjust(8))}  {_highlight(m['snippet'], m['match'], style)}")
        hidden = g.match_count - len(snippets) - len(title_hits)
        footer = [f"key {g.zotero_key}"]
        if hidden > 0:
            footer.append(f"{hidden} more")
        link = _select_link(g, libraries)
        if link:
            footer.append(link)
        print("  " + style.dim(" · ".join(footer)))
        print()

    papers = f"{len(groups)} paper{'s' if len(groups) != 1 else ''}"
    summary = f"{outcome.count} match{'es' if outcome.count != 1 else ''} in {papers}" if query.strip() else papers
    if len(shown) < len(groups):
        summary += f", first {len(shown)} shown (--limit 0 for all)"
    print(style.dim(summary))
    return EXIT_OK


def _select_link(g, libraries: dict) -> str:
    lib = libraries.get(g.library_id)
    if not lib:
        return ""
    key = g.parent_key or g.zotero_key
    if lib.type == "group":
        return f"zotero://select/groups/{lib.zotero_id}/items/{key}"
    return f"zotero://select/library/items/{key}"


def cmd_serve(args) -> int:
    import webbrowser
    import uvicorn
    from zotegrep.web import LOOPBACK_HOSTS, create_app

    loopback = args.host in LOOPBACK_HOSTS
    if not loopback:
        print(f"Warning: listening on {args.host}. ZoteGrep has no accounts: anyone who can reach "
              "this address can search your library and change its settings.", file=sys.stderr)
    app = create_app(allowed_hosts=LOOPBACK_HOSTS if loopback else None, auto_sync=not args.no_sync)
    url = f"http://{'localhost' if loopback else args.host}:{args.port}/"
    print(f"ZoteGrep {__version__} running on {url} (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def cmd_status(args) -> int:
    from zotegrep.text import OCR_LANGUAGES, TESSDATA

    cfg = config.load()
    store.init_db()
    stats = store.get_stats()
    print(f"ZoteGrep {__version__}")
    print(f"Config file    {config.paths.config_file}")
    print(f"Data directory {config.paths.data_dir}")
    print(f"Libraries      {', '.join(f'#{lib.id} {lib.label}' for lib in cfg.libraries) or 'none'}")
    print(f"PDFs indexed   {stats['pdf_count']} ({stats['page_count']} pages"
          + (f", {stats['no_text_count']} without text" if stats["no_text_count"] else "") + ")")
    print(f"Last sync      {stats['last_sync'] or 'never'}")
    interval = f"every {cfg.sync_interval} min while `zotegrep serve` runs" if cfg.sync_interval else "on demand only"
    print(f"Auto sync      {interval}")
    print(f"OCR            {('Tesseract, ' + OCR_LANGUAGES) if TESSDATA and OCR_LANGUAGES else 'off (Tesseract not found)'}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zotegrep",
        description="Full-text search inside every PDF of your Zotero library.",
        epilog="Query syntax: a, b (AND)  a OR b  -a (exclude)  \"exact phrase\"  a NEAR/5 b.",
    )
    parser.add_argument("--version", action="version", version=f"zotegrep {__version__}")
    parser.add_argument("--home", metavar="DIR",
                        help="keep the config and data under DIR (default: the platform's folders, or $ZOTEGREP_HOME)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log what happens in detail")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    lib = sub.add_parser("library", help="add, list, edit or remove Zotero libraries")
    lib_sub = lib.add_subparsers(dest="action", required=True, metavar="action")
    add = lib_sub.add_parser("add", help="add a Zotero library")
    add.add_argument("zotero_id", help="your Zotero user ID, or a group ID with --group")
    add.add_argument("--group", action="store_true", help="the ID is a group library")
    add.add_argument("--key", help="Zotero API key (asked for if omitted)")
    add.add_argument("--name", help="a name to tell libraries apart")
    add.set_defaults(func=cmd_library_add)
    lst = lib_sub.add_parser("list", help="list configured libraries")
    lst.set_defaults(func=cmd_library_list)
    edit = lib_sub.add_parser("edit", help="rename a library or change its API key")
    edit.add_argument("id", type=int, help="library number, from `zotegrep library list`")
    edit.add_argument("--name", help="new name")
    edit.add_argument("--key", action="store_true", help="ask for a new API key")
    edit.set_defaults(func=cmd_library_edit)
    rm = lib_sub.add_parser("remove", help="remove a library and its local data")
    rm.add_argument("id", type=int, help="library number, from `zotegrep library list`")
    rm.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    rm.set_defaults(func=cmd_library_remove)

    sync = sub.add_parser("sync", help="download new PDFs from Zotero and index them")
    sync.add_argument("-q", "--quiet", action="store_true", help="print nothing but errors")
    sync.set_defaults(func=cmd_sync)

    s = sub.add_parser("search", help="search inside the PDFs",
                       description="Search inside the PDFs, titles, abstracts, notes and annotations.")
    s.add_argument("query", nargs="*", help="the query (quote it in your shell)")
    s.add_argument("-c", "--case", action="store_true", help="case sensitive")
    s.add_argument("-E", "--regex", action="store_true", help="the query is one regular expression")
    s.add_argument("--scope", choices=search.SCOPES, default="doc",
                   help="where all terms must appear together: doc (default), page or paragraph")
    s.add_argument("--collection", help="collection name or key (subcollections included)")
    s.add_argument("--tag", action="append", help="Zotero tag (repeat for several)")
    s.add_argument("--since", type=int, metavar="YEAR", help="published this year or later")
    s.add_argument("--until", type=int, metavar="YEAR", help="published this year or earlier")
    s.add_argument("--author", help="part of an author's name")
    s.add_argument("--library", type=int, metavar="ID", help="only this library (number from `library list`)")
    s.add_argument("--sort", choices=("relevance", "matches", "date", "title"), default="relevance")
    s.add_argument("-n", "--limit", type=int, default=20, help="papers to show (0 for all, default 20)")
    s.add_argument("-s", "--snippets", type=int, default=3, help="snippets per paper (0 for all, default 3)")
    s.add_argument("-l", "--files-with-matches", action="store_true", help="only list the matching papers")
    s.add_argument("--json", action="store_true", help="one JSON object per paper")
    s.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    s.set_defaults(func=cmd_search)

    serve = sub.add_parser("serve", help="open the web interface")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    serve.add_argument("--no-sync", action="store_true", help="never sync on its own")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="show where things are and what is indexed")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # Redirected output on Windows would otherwise use the legacy code page.
            if os.name == "nt" and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    _enable_windows_ansi()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.home:
        config.set_home(args.home)
    try:
        return args.func(args)
    except (CliError, ConfigError) as e:
        print(f"zotegrep: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
