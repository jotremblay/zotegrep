import pytest

from zotegrep import search, store
from zotegrep.search import Filters, SearchError

from conftest import add_paper

RSQUO, EN_DASH, LDQUO, RDQUO = chr(0x2019), chr(0x2013), chr(0x201C), chr(0x201D)


def keys(query: str, **kwargs) -> list[str]:
    return [g.zotero_key for g in search.search(query, **kwargs).groups]


def matches(query: str, key: str, **kwargs) -> list[str]:
    return [m["match"] for g in search.search(query, **kwargs).groups if g.zotero_key == key for m in g.matches]


def test_fts_index_is_available(home):
    assert store.FTS_ENABLED


def test_flexible_phrase_matches_spellings(home):
    add_paper("A", ["We model time-series data.", "Timeseries again, and Time Series."])
    assert matches("time series", "A") == ["time-series", "Timeseries", "Time Series"]


def test_and_is_per_paper_or_per_page_depending_on_scope(home):
    add_paper("SPLIT", ["anomaly here", "transformer there"])
    add_paper("SAME", ["anomaly and transformer on one page"])
    assert sorted(keys("anomaly, transformer")) == ["SAME", "SPLIT"]
    assert keys("anomaly AND transformer", scope="page") == ["SAME"]


def test_paragraph_scope_and_reading_order(home):
    add_paper("P", ["filler filler filler beacon first\n\nbeacon second", "beacon alone\n\nother"])
    assert keys("beacon, second", scope="paragraph") == ["P"]
    snippets = [m["snippet"] for m in search.search("beacon", scope="paragraph").groups[0].matches]
    assert snippets[0].startswith("filler") and snippets[1].startswith("beacon second")


def test_or_and_exclusion(home):
    add_paper("MAMBA", ["state space models like mamba"])
    add_paper("S4", ["the s4 layer, used for forecasting"])
    assert sorted(keys("mamba OR s4")) == ["MAMBA", "S4"]
    assert keys("mamba OR s4, -forecasting") == ["MAMBA"]
    assert keys("mamba OR s4, NOT forecasting") == ["MAMBA"]


def test_exact_phrase_needs_whole_words(home):
    add_paper("RAG", ["retrieval augmented generation (RAG) pipeline"])
    add_paper("FRAG", ["a fragment of text"])
    assert keys('"RAG"') == ["RAG"]
    assert sorted(keys("rag")) == ["FRAG", "RAG"]


def test_near_finds_words_close_together_in_any_order(home):
    add_paper("CLOSE", ["the F1 score with point adjust is misleading"])
    add_paper("FAR", ["point adjust " + "word " * 20 + "F1"])
    assert keys("point adjust NEAR/5 F1") == ["CLOSE"]
    assert keys("F1 NEAR/5 point adjust") == ["CLOSE"]
    assert sorted(keys("point adjust NEAR/50 F1")) == ["CLOSE", "FAR"]


def test_typographic_punctuation_is_folded(home):
    add_paper("TYPO", [f"the model{RSQUO}s output and the Kullback{EN_DASH}Leibler divergence"])
    assert matches("model's", "TYPO") == [f"model{RSQUO}s"]
    assert matches("Kullback-Leibler", "TYPO") == [f"Kullback{EN_DASH}Leibler"]
    assert keys(f"{LDQUO}Leibler divergence{RDQUO}") == ["TYPO"]


def test_accents_and_case(home):
    add_paper("FR", ["une approche na" + chr(0xEF) + "ve et Robuste"])
    assert keys("naive") == ["FR"]
    assert keys("robuste") == ["FR"]
    assert keys("robuste", case_sensitive=True) == []


def test_regex_mode(home):
    add_paper("RX", ["transformers and transformation"])
    assert matches(r"transform\w+", "RX", use_regex=True) == ["transformers", "transformation"]
    with pytest.raises(SearchError, match="Invalid regex"):
        search.search("(unclosed", use_regex=True)


def test_query_errors(home):
    with pytest.raises(SearchError, match="needs at least one term"):
        search.search("-alone")


def test_metadata_is_searched_and_title_matches_rank_first(home):
    add_paper("BODY", ["isolation forest " * 3], title="Some other title")
    add_paper("TITLE", ["isolation forest once"], title="Isolation Forest")
    add_paper("ABSTRACT", ["nothing relevant"], abstract="We revisit the isolation forest.")
    assert keys("isolation forest")[0] == "TITLE"
    assert "ABSTRACT" in keys("isolation forest")


def test_filters(home):
    conn = store.get_db()
    conn.executemany("INSERT INTO collections VALUES (?, ?, ?, ?)",
                     [(1, "ROOT0001", "Root", None), (1, "CHILD001", "Child", "ROOT0001")])
    conn.commit()
    conn.close()
    add_paper("OLD", ["drift"], date="2015", tags=["classic"], authors_full="Ada Lovelace",
              collections=["CHILD001"])
    add_paper("NEW", ["drift"], date="2024", tags=["recent", "classic"], authors_full="Alan Turing")
    assert keys("drift", filters=Filters(year_from=2020)) == ["NEW"]
    assert keys("drift", filters=Filters(year_to=2020)) == ["OLD"]
    assert keys("drift", filters=Filters(tags=["classic", "recent"])) == ["NEW"]
    assert keys("drift", filters=Filters(author="lovelace")) == ["OLD"]
    assert keys("drift", filters=Filters(collection="ROOT0001")) == ["OLD"]   # subcollections included
    # no query: the filters alone list papers
    assert keys("", filters=Filters(tags=["classic"])) == ["NEW", "OLD"]


def test_snippets_carry_page_numbers(home):
    add_paper("PAGES", ["intro", "the needle is here", "more", "needle again"])
    found = search.search("needle").groups[0].matches
    assert [m["page"] for m in found] == [2, 4]
    assert all("needle" in m["snippet"] for m in found)
