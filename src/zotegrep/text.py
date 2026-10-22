"""PDF text extraction (with OCR of scanned pages) and text normalization."""
import os
import re
import glob
import html
import logging
import unicodedata

import pymupdf

logger = logging.getLogger(__name__)

# Below this, a page is considered to have no text layer (scan) and goes to OCR.
OCR_MIN_CHARS = 20
OCR_MAX_PAGES = 80
OCR_DPI = 200
# A whole PDF with less text than this is listed as "without searchable text".
NO_TEXT_MAX_CHARS = 100

_SOFT_HYPHEN = chr(0xAD)
_HYPHEN_BREAK_RE = re.compile(
    # a letter, then a hyphen (ASCII, soft, U+2010, U+2011) at the end of a
    # line, then a lowercase letter (ASCII or accented Latin-1)
    r'(?<=[^\W\d_])[-' + _SOFT_HYPHEN + chr(0x2010) + chr(0x2011) + r']' + '\n'
    + '(?=[a-z' + chr(0xE0) + '-' + chr(0xF6) + chr(0xF8) + '-' + chr(0xFF) + '])'
)
# Control characters (except tab and newline) become spaces: math fonts emit
# them, and SQLite's FTS5 (at least up to 3.40) stops indexing a page at its
# first NUL character, which made the rest of the page unsearchable.
_CONTROL_CHARS = dict.fromkeys([c for c in range(0x20) if c not in (0x09, 0x0A)] + [0x7F], " ")
# Typographic punctuation folded to its keyboard equivalent, one character for
# one like accents: typing model's finds the curly apostrophe, Kullback-Leibler
# finds the en dash, and the curly quotes a phone keyboard inserts still
# delimit an exact phrase.
_PUNCT_FOLD = {
    **dict.fromkeys((0x2018, 0x2019, 0x201A, 0x201B, 0x2032, 0x02BC, 0xB4), "'"),
    **dict.fromkeys((0x201C, 0x201D, 0x201E, 0x201F, 0x2033), '"'),
    **dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D), "-"),
}
_fold_cache: dict[str, str] = {}


def normalize_text(text: str) -> str:
    """Text as displayed and indexed: ligatures split, accents composed,
    words hyphenated at the end of a line joined back (optimi- / zation
    becomes optimization), control characters removed."""
    text = unicodedata.normalize("NFKC", text).translate(_CONTROL_CHARS)
    text = _HYPHEN_BREAK_RE.sub("", text)
    return text.replace(_SOFT_HYPHEN, "")


def _fold_char(ch: str) -> str:
    folded = _fold_cache.get(ch)
    if folded is None:
        folded = _PUNCT_FOLD.get(ord(ch))
        if folded is None:
            base = "".join(
                c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
            )
            folded = base if len(base) == 1 else ch
        _fold_cache[ch] = folded
    return folded


def fold(text: str) -> str:
    """Strip accents and turn typographic punctuation into ASCII, character
    by character, keeping the length: an offset in the folded text points to
    the same place in the original."""
    if text.isascii():
        return text
    return "".join(ch if ch < "\x80" else _fold_char(ch) for ch in text)


def strip_html(raw: str) -> str:
    text = re.sub(r'<(br|/p|/div|/li|/h\d)\s*/?>', '\n', raw or '', flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(text)).strip()


def _find_tessdata() -> str | None:
    candidates = [os.getenv("TESSDATA_PREFIX", "")]
    candidates += sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata"), reverse=True)
    candidates += ["/usr/share/tessdata", "/usr/local/share/tessdata",
                   "/opt/homebrew/share/tessdata", r"C:\Program Files\Tesseract-OCR\tessdata"]
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "eng.traineddata")):
            return c
    return None


TESSDATA = _find_tessdata()
# Tesseract languages, e.g. ZOTEGREP_OCR_LANGUAGES=eng+deu. Each extra
# language makes OCR slower; by default English and French when installed.
OCR_LANGUAGES = os.getenv("ZOTEGREP_OCR_LANGUAGES") or "+".join(
    lang for lang in ("eng", "fra")
    if TESSDATA and os.path.exists(os.path.join(TESSDATA, f"{lang}.traineddata"))
)


def _blocks_to_text(blocks: list) -> str:
    # A PyMuPDF block is roughly a paragraph: the blank line between blocks is
    # what the "paragraph" search scope splits on.
    return "\n\n".join(b[4].strip() for b in blocks if b[6] == 0 and b[4].strip())


def extract_pages(filepath: str) -> tuple[list[str], int]:
    """Normalized text of each page, and how many pages went through OCR."""
    pages, ocr_count = [], 0
    with pymupdf.open(filepath) as doc:
        for page in doc:
            text = _blocks_to_text(page.get_text("blocks"))
            if (len(text) < OCR_MIN_CHARS and TESSDATA and ocr_count < OCR_MAX_PAGES
                    and page.get_images()):
                try:
                    tp = page.get_textpage_ocr(
                        flags=0, language=OCR_LANGUAGES, dpi=OCR_DPI, full=True, tessdata=TESSDATA
                    )
                    text = _blocks_to_text(page.get_text("blocks", textpage=tp))
                    ocr_count += 1
                except Exception as e:
                    logger.warning("OCR failed on %s p.%s: %s", filepath, page.number + 1, e)
            pages.append(normalize_text(text))
    return pages, ocr_count
