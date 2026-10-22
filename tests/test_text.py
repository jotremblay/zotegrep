from zotegrep.text import fold, normalize_text, strip_html

RSQUO, EN_DASH, LDQUO, RDQUO, EM_DASH = chr(0x2019), chr(0x2013), chr(0x201C), chr(0x201D), chr(0x2014)


def test_normalize_joins_words_hyphenated_at_line_end():
    assert normalize_text("an optimi-\nzation problem") == "an optimization problem"
    # a capital after the break is a real compound or a new sentence
    assert normalize_text("Kullback-\nLeibler") == "Kullback-\nLeibler"


def test_normalize_splits_ligatures_and_drops_soft_hyphens():
    assert normalize_text(chr(0xFB01) + "nite") == "finite"
    assert normalize_text("detec" + chr(0xAD) + "tion") == "detection"


def test_normalize_turns_control_characters_into_spaces():
    raw = "alpha" + chr(0) + "beta" + chr(1) + "gamma\tdelta\nepsilon" + chr(0xC) + "zeta"
    assert normalize_text(raw) == "alpha beta gamma\tdelta\nepsilon zeta"


def test_fold_keeps_length_and_maps_accents_and_punctuation():
    text = f"na{chr(0xEF)}ve model{RSQUO}s Kullback{EN_DASH}Leibler {LDQUO}RAG{RDQUO} a{EM_DASH}b"
    folded = fold(text)
    assert len(folded) == len(text)
    assert folded == "naive model's Kullback-Leibler \"RAG\" a-b"


def test_fold_leaves_ascii_untouched():
    text = "plain ASCII text, with -dashes- and 'quotes'"
    assert fold(text) is text


def test_strip_html_keeps_line_structure():
    assert strip_html("<p>First</p><p>Second &amp; third<br>fourth</p>") == "First\nSecond & third\nfourth"
