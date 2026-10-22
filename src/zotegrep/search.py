"""Search engine: query language, FTS5 preselection, pattern matching on the
text of pages, metadata, notes and annotations, and relevance."""
import re
import json
import math
import time
import logging
from itertools import groupby
from dataclasses import dataclass, field

import regex

from zotegrep import store
from zotegrep.text import fold

logger = logging.getLogger(__name__)

CONTEXT_CHARS = 120
MAX_REGEX_LEN = 200
REGEX_TIMEOUT = 5.0
MAX_SNIPPETS_PER_DOC = 100
MIN_SNIPPETS_PER_DOC = 5
MAX_SNIPPETS_SINGLE_DOC = 2000
MAX_SNIPPETS_TOTAL = 5000
PROGRESS_EVERY = 20
MAX_TERMS = 20
NEAR_DEFAULT = 10
NEAR_MAX = 50
TITLE_BOOST = 2.0
TF_SATURATION = 1.5

SCOPES = ("doc", "page", "paragraph")
# Display order of matches found on the same page.
KIND_RANK = {"title": 0, "authors": 1, "abstract": 2, "note": 3, "page": 4, "annotation": 5}


class SearchError(Exception):
    pass


# --------------------------------------------------------------------------
# Query language
#
#   a, b            AND (also: a AND b)
#   a OR b          OR
#   -a / NOT a      exclusion
#   "a b"           exact phrase (whole words)
#   a NEAR/5 b      a and b at most 5 words apart (NEAR alone = 10)
#   time series     flexible phrase: also matches time-series, timeseries
# --------------------------------------------------------------------------

@dataclass
class Term:
    pattern: object
    # Substrings every matching page necessarily contains (FTS5 query).
    # None = no preselection possible for this term.
    fts_tokens: list[str] | None


@dataclass
class ParsedQuery:
    positives: list[list[Term]] = field(default_factory=list)  # AND of ORs
    negatives: list[Term] = field(default_factory=list)
    timed: bool = False

    @property
    def terms(self) -> list[Term]:
        return [t for clause in self.positives for t in clause]


_QUOTE_RE = re.compile(r'"([^"]*)"')
_PLACEHOLDER_RE = re.compile(r'\x00(\d+)\x00')
_NEAR_RE = re.compile(r'^(.+?)\s+NEAR(?:/(\d+))?\s+(.+)$')
_SEPARATORS = r'[\s.\-_/]+'


def _fts_tokens(text: str) -> list[str] | None:
    tokens = [t.lower() for t in re.findall(r'[A-Za-z0-9]{3,}', text)]
    return sorted(set(tokens), key=len, reverse=True)[:6] or None


def _flexible_source(text: str) -> str:
    tokens = [t for t in re.split(_SEPARATORS, text) if t]
    if not tokens:
        return re.escape(text)
    return r'[\s.\-_/]?'.join(re.escape(t) for t in tokens)


def _exact_source(text: str) -> str:
    parts = []
    for chunk in re.split(r'(\s+|-)', text.strip()):
        if not chunk:
            continue
        if chunk.isspace():
            parts.append(r'\s+')
        elif chunk == '-':
            parts.append(r'[-\s]*')  # the indexed text joins hyphenated words back
        else:
            parts.append(re.escape(chunk))
    source = ''.join(parts)
    if text[:1].isalnum():
        source = r'(?<!\w)' + source
    if text[-1:].isalnum():
        source += r'(?!\w)'
    return source


def _atom_source(atom: str, quoted: list[str]) -> tuple[str, str]:
    """(regex source, raw text) of a simple term, exact phrase or not."""
    m = _PLACEHOLDER_RE.fullmatch(atom)
    if m:
        raw = quoted[int(m.group(1))]
        return _exact_source(raw), raw
    raw = _PLACEHOLDER_RE.sub(lambda p: quoted[int(p.group(1))], atom)
    return _flexible_source(raw), raw


def _build_term(atom: str, quoted: list[str], flags: int) -> Term:
    near = _NEAR_RE.match(atom)
    if near:
        left, left_raw = _atom_source(near.group(1).strip(), quoted)
        right, right_raw = _atom_source(near.group(3).strip(), quoted)
        n = min(int(near.group(2) or NEAR_DEFAULT), NEAR_MAX)
        gap = r'\w*(?:\W+\w+){0,%d}?\W+\w*' % n
        source = f'(?:{left}){gap}(?:{right})|(?:{right}){gap}(?:{left})'
        tokens_l, tokens_r = _fts_tokens(left_raw), _fts_tokens(right_raw)
        tokens = (tokens_l or []) + (tokens_r or []) or None
        return Term(re.compile(source, flags), tokens)
    source, raw = _atom_source(atom, quoted)
    return Term(re.compile(source, flags), _fts_tokens(raw))


def parse_query(query: str, case_sensitive: bool, use_regex: bool) -> ParsedQuery:
    if use_regex:
        if len(query) > MAX_REGEX_LEN:
            raise SearchError(f"Regex too long (max {MAX_REGEX_LEN} chars)")
        # The `regex` module, not `re`: only it accepts a timeout, the guard
        # against patterns with catastrophic backtracking.
        try:
            pattern = regex.compile(query, 0 if case_sensitive else regex.IGNORECASE)
        except regex.error as e:
            raise SearchError(f"Invalid regex: {e}")
        return ParsedQuery(positives=[[Term(pattern, None)]], timed=True)

    flags = 0 if case_sensitive else re.IGNORECASE
    quoted: list[str] = []

    def stash(m):
        quoted.append(fold(m.group(1)))
        return f'\x00{len(quoted) - 1}\x00'

    text = _QUOTE_RE.sub(stash, fold(query)).replace('"', ' ')
    parsed = ParsedQuery()
    for clause in re.split(r',|\s+AND\s+', text):
        clause = clause.strip()
        if not clause:
            continue
        negative = False
        if clause.startswith('-') and len(clause) > 1:
            negative, clause = True, clause[1:].strip()
        elif clause.startswith('NOT '):
            negative, clause = True, clause[4:].strip()
        alternatives = [a.strip() for a in re.split(r'\s+OR\s+', clause) if a.strip()]
        terms = [_build_term(a, quoted, flags) for a in alternatives]
        if not terms:
            continue
        if negative:
            parsed.negatives.extend(terms)
        else:
            parsed.positives.append(terms)
    if len(parsed.terms) + len(parsed.negatives) > MAX_TERMS:
        raise SearchError(f"Too many terms (max {MAX_TERMS})")
    if parsed.negatives and not parsed.positives:
        raise SearchError("An exclusion (-term) needs at least one term to search for")
    return parsed


# --------------------------------------------------------------------------
# Filters (library, collection, tags, years, author)
# --------------------------------------------------------------------------

@dataclass
class Filters:
    library_id: int | None = None
    collection: str | None = None
    tags: list[str] = field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    author: str = ""

    def active(self) -> bool:
        return bool(self.library_id or self.collection or self.tags
                    or self.year_from or self.year_to or self.author.strip())


def _collection_with_descendants(conn, root: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for row in conn.execute("SELECT collection_key, parent_key FROM collections").fetchall():
        children.setdefault(row["parent_key"] or "", []).append(row["collection_key"])
    keys, stack = set(), [root]
    while stack:
        key = stack.pop()
        if key not in keys:
            keys.add(key)
            stack.extend(children.get(key, []))
    return keys


def _items_in_scope(conn, filters: Filters, zotero_key: str | None) -> dict[str, dict]:
    rows = conn.execute("""
        SELECT zotero_key, library_id, parent_key, title, authors, authors_full, date, filename,
               abstract, tags, collections, publication, doi, url
        FROM items WHERE (? IS NULL OR zotero_key=?)
    """, (zotero_key, zotero_key)).fetchall()

    wanted_collections = (
        _collection_with_descendants(conn, filters.collection) if filters.collection else None
    )
    author = fold(filters.author.strip()).lower()
    items = {}
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item["tags"] or "[]")
        if filters.library_id and item["library_id"] != filters.library_id:
            continue
        if wanted_collections is not None and not wanted_collections.intersection(
                json.loads(item["collections"] or "[]")):
            continue
        if filters.tags and not set(filters.tags).issubset(item["tags"]):
            continue
        if filters.year_from or filters.year_to:
            year = int(item["date"][:4]) if (item["date"] or "")[:4].isdigit() else None
            if year is None or (filters.year_from and year < filters.year_from) \
                    or (filters.year_to and year > filters.year_to):
                continue
        if author and author not in fold(f"{item['authors_full'] or ''} {item['authors'] or ''}").lower():
            continue
        items[item["zotero_key"]] = item
    return items


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

@dataclass
class DocMatches:
    zotero_key: str
    library_id: int | None
    parent_key: str
    title: str
    authors: str
    date: str
    filename: str
    publication: str
    doi: str
    url: str
    tags: list
    match_count: int = 0
    score: float = 0.0
    matches: list = field(default_factory=list)
    # per clause: number of occurrences in each unit (page, abstract...) that matches
    clause_tf: list = field(default_factory=list, repr=False)
    title_hit: bool = False


@dataclass
class SearchOutcome:
    count: int
    groups: list
    truncated: bool


@dataclass
class SearchStart:
    """First event of iter_search: enough to follow progress and compute the
    final scores once every paper is known."""
    total_docs: int
    clauses: int


@dataclass
class _Unit:
    kind: str
    page: int
    body: str   # text searched (without accents, except in regex mode)
    text: str   # text displayed, same offsets as body
    page_id: int | None = None
    offset: int = 0  # position in the page (paragraphs), to order the snippets


class _Matcher:
    """Applies the patterns, with an overall time budget in regex mode."""

    def __init__(self, timed: bool):
        self._deadline = time.monotonic() + REGEX_TIMEOUT if timed else None

    def finditer(self, pattern, text: str):
        if self._deadline is None:
            return pattern.finditer(text)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return pattern.finditer(text, timeout=remaining)


def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - CONTEXT_CHARS)
    hi = min(len(text), end + CONTEXT_CHARS)
    snippet = re.sub(r'\s+', ' ', text[lo:hi]).strip()
    if lo > 0:
        snippet = "..." + snippet
    if hi < len(text):
        snippet += "..."
    return snippet


def _fts_query(tokens: list[str]) -> str:
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _candidate_pages(conn, parsed: ParsedQuery, scope: str):
    """Pages worth reading according to the FTS5 index (trigrams). None when
    a term allows no preselection (regex, words under 3 letters): every page
    must then be read."""
    if not store.FTS_ENABLED:
        return None

    def pages_for(term: Term) -> set[int] | None:
        if not term.fts_tokens:
            return None
        # No JOIN on `pages` here: it makes SQLite pick a plan that probes the
        # FTS index page by page (seconds instead of a millisecond).
        rows = conn.execute(
            "SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?", (_fts_query(term.fts_tokens),)
        ).fetchall()
        return {r[0] for r in rows}

    clause_sets = []
    for clause in parsed.positives:
        pages: set[int] = set()
        for term in clause:
            found = pages_for(term)
            if found is None:
                return None
            pages |= found
        clause_sets.append(pages)

    if scope == "doc":
        candidates = set().union(*clause_sets) if clause_sets else set()
        # An exclusion applies to the whole paper: its candidate pages must be
        # read too.
        for term in parsed.negatives:
            found = pages_for(term)
            if found is None:
                return None
            candidates |= found
        return candidates
    return set.intersection(*clause_sets) if clause_sets else set()


def _load_pages(conn, keys: list[str], page_ids: set[int] | None, use_regex: bool):
    body = "p.text" if use_regex else "COALESCE(p.folded, p.text)"
    sql = f"""
        SELECT p.id, p.zotero_key, p.page_num, p.text, {body} AS body
        FROM pages p
        WHERE p.zotero_key IN (SELECT value FROM json_each(?))
    """
    params: list = [json.dumps(keys)]
    if page_ids is not None:
        sql += " AND p.id IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(sorted(page_ids)))
    return conn.execute(sql + " ORDER BY p.zotero_key, p.page_num", params)


def _paragraphs(unit: _Unit):
    for m in re.finditer(r'(?:[^\n]|\n(?!\n))+', unit.body):
        yield _Unit(unit.kind, unit.page, m.group(), unit.text[m.start():m.end()], unit.page_id, m.start())


def _doc_units(item: dict, extras: list, page_rows: list, use_regex: bool, scope: str) -> list[_Unit]:
    prep = (lambda t: t) if use_regex else fold
    units = [
        _Unit(kind, 0, prep(text), text)
        for kind, text in (
            ("title", item["title"]), ("authors", item["authors_full"]), ("abstract", item["abstract"]),
        ) if text
    ]
    units += [_Unit(e["kind"], e["page_num"], prep(e["text"]), e["text"]) for e in extras]
    pages = [_Unit("page", r["page_num"], r["body"] or "", r["text"] or "", r["id"]) for r in page_rows]
    if scope == "paragraph":
        pages = [para for unit in pages for para in _paragraphs(unit)]
    return units + pages


def iter_search(query: str, case_sensitive: bool = False, use_regex: bool = False,
                scope: str = "doc", filters: Filters | None = None,
                zotero_key: str | None = None, doc_cap: int = MAX_SNIPPETS_PER_DOC,
                cancel=None):
    """Incremental search. Yields a SearchStart, then each DocMatches as soon
    as it is complete (count and snippets), interleaved with integers giving
    the number of papers examined so far.

    Papers are examined from the most to the least promising (number of
    candidate pages according to the index), so the best results come first.
    The score each DocMatches carries is provisional (no idf); finalize_scores
    gives the final ranking once everything is known. `cancel` (a
    threading.Event) stops the search between two papers. Without a query
    (filters only), lists the matching papers."""
    filters = filters or Filters()
    if scope not in SCOPES:
        scope = "doc"
    parsed = parse_query(query, case_sensitive, use_regex) if query.strip() else ParsedQuery()

    conn = store.get_db()
    try:
        items = _items_in_scope(conn, filters, zotero_key)
        yield SearchStart(len(items), len(parsed.positives))
        if not items:
            return
        if not parsed.positives:
            yield from sorted((_new_doc(item) for item in items.values()), key=lambda g: (g.title or "").lower())
            return

        page_ids = _candidate_pages(conn, parsed, scope)
        keys = list(items)
        extras_by_key: dict[str, list] = {}
        for row in conn.execute(
            "SELECT zotero_key, kind, page_num, text FROM extras "
            "WHERE zotero_key IN (SELECT value FROM json_each(?))",
            (json.dumps(keys),),
        ).fetchall():
            extras_by_key.setdefault(row["zotero_key"], []).append(row)

        pages_by_key = {
            key: list(rows)
            for key, rows in groupby(_load_pages(conn, keys, page_ids, use_regex), key=lambda r: r["zotero_key"])
        }
        if page_ids is not None:
            keys.sort(key=lambda k: len(pages_by_key.get(k, ())), reverse=True)

        matcher = _Matcher(parsed.timed)
        unit_idf = [1.0] * len(parsed.positives)
        for done, key in enumerate(keys, 1):
            if cancel is not None and cancel.is_set():
                return
            item = items[key]
            units = _doc_units(item, extras_by_key.get(key, []), pages_by_key.get(key, []), use_regex, scope)
            doc = _match_doc(item, units, parsed, matcher, scope, doc_cap)
            if doc:
                doc.score = _doc_score(doc, unit_idf)
                yield doc
            if done % PROGRESS_EVERY == 0:
                yield done
    except TimeoutError:
        raise SearchError(f"Regex too slow (over {REGEX_TIMEOUT}s), simplify it")
    finally:
        conn.close()


def search(query: str, case_sensitive: bool = False, use_regex: bool = False,
           scope: str = "doc", filters: Filters | None = None,
           zotero_key: str | None = None, doc_cap: int | None = None) -> SearchOutcome:
    """Results grouped by paper, sorted by relevance. Every occurrence is
    counted, but only the first snippets are returned (MAX_SNIPPETS_PER_DOC /
    MAX_SNIPPETS_TOTAL) to bound the response. With `zotero_key`, the search
    is limited to that paper, with a higher cap."""
    if doc_cap is None:
        doc_cap = MAX_SNIPPETS_SINGLE_DOC if zotero_key else MAX_SNIPPETS_PER_DOC
    start, groups = None, []
    for event in iter_search(query, case_sensitive, use_regex, scope, filters, zotero_key, doc_cap):
        if isinstance(event, SearchStart):
            start = event
        elif isinstance(event, DocMatches):
            groups.append(event)
    if not start.clauses:
        return SearchOutcome(0, groups, False)

    finalize_scores(groups, start)
    groups.sort(key=lambda g: (g.score, g.match_count), reverse=True)
    # Overall budget shared fairly: every paper keeps some snippets.
    per_doc = doc_cap if zotero_key else max(MIN_SNIPPETS_PER_DOC, MAX_SNIPPETS_TOTAL // max(1, len(groups)))
    truncated = False
    for g in groups:
        del g.matches[per_doc:]
        if len(g.matches) < g.match_count:
            truncated = True
    return SearchOutcome(sum(g.match_count for g in groups), groups, truncated)


def _doc_score(doc: DocMatches, idf: list[float]) -> float:
    score = 0.0
    for c, tfs in enumerate(doc.clause_tf):
        if tfs:
            # / sqrt(n): a 200-page thesis must not crush a 10-page paper
            # entirely about the topic.
            score += idf[c] * sum(tf / (tf + TF_SATURATION) for tf in tfs) / math.sqrt(len(tfs))
    if doc.title_hit:
        score *= TITLE_BOOST
    return round(score, 3)


def finalize_scores(groups: list, start: SearchStart):
    """BM25-like relevance: for each clause, a saturated frequency per unit
    (50 occurrences on a reference page weigh barely more than 5), summed over
    the paper and weighted by the rarity of the clause (idf); doubled when the
    title itself matches."""
    idf = [
        math.log(1 + start.total_docs / (1 + sum(1 for g in groups if g.clause_tf[c])))
        for c in range(start.clauses)
    ]
    for g in groups:
        g.score = _doc_score(g, idf)


def _new_doc(item: dict) -> DocMatches:
    return DocMatches(
        zotero_key=item["zotero_key"],
        library_id=item["library_id"],
        parent_key=item["parent_key"] or "",
        title=item["title"] or item["filename"] or "Unknown",
        authors=item["authors"] or "",
        date=item["date"] or "",
        filename=item["filename"] or "",
        publication=item["publication"] or "",
        doi=item["doi"] or "",
        url=item["url"] or "",
        tags=item["tags"],
    )


def _match_doc(item: dict, units: list[_Unit], parsed: ParsedQuery, matcher: _Matcher,
               scope: str, doc_cap: int) -> DocMatches | None:
    # hits[u][c]: matches of clause c (all its ORs) in unit u
    hits = [
        [
            [m for term in clause for m in matcher.finditer(term.pattern, unit.body) if m.end() > m.start()]
            for clause in parsed.positives
        ]
        for unit in units
    ]
    excluded = [
        any(next(matcher.finditer(term.pattern, unit.body), None) is not None for term in parsed.negatives)
        for unit in units
    ] if parsed.negatives else [False] * len(units)

    if scope == "doc":
        # AND at the paper level: each clause somewhere, no exclusion anywhere.
        if any(excluded):
            return None
        if not all(any(h[c] for h in hits) for c in range(len(parsed.positives))):
            return None
        qualifying = range(len(units))
    else:
        # AND at the unit level (page or paragraph): everything in the same one.
        qualifying = [u for u, h in enumerate(hits) if all(h) and not excluded[u]]
        if not qualifying:
            return None

    doc = _new_doc(item)
    found = []
    doc.clause_tf = [[] for _ in parsed.positives]
    for u in qualifying:
        unit = units[u]
        for c, clause_hits in enumerate(hits[u]):
            if clause_hits:
                doc.clause_tf[c].append(len(clause_hits))
                doc.title_hit = doc.title_hit or unit.kind == "title"
            for m in clause_hits:
                found.append((unit.page, KIND_RANK.get(unit.kind, 9), unit.offset + m.start(), unit, m))
    if not found:
        return None
    found.sort(key=lambda f: f[:3])
    doc.match_count = len(found)
    for page, _, _, unit, m in found[:doc_cap]:
        doc.matches.append({
            "page": page,
            "kind": unit.kind,
            "match": unit.text[m.start():m.end()],
            "snippet": _snippet(unit.text, m.start(), m.end()),
        })
    return doc
