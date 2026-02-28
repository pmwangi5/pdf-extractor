"""
PDF Data Extractor Module

Column-aware extraction for multi-column technical manuals (car manuals, service docs, etc.).
Handles:
  - 2- and 3-column page layouts (auto-detected per page)
  - Printed page numbers in chapter-page format (e.g. "7-5", "1-21") from page headers
  - CID artifact substitution (bullet points, section markers, special chars)
  - Hyphenation repair across line breaks
  - ManualsLib / watermark footer stripping
"""

import re
import pdfplumber
import PyPDF2
from typing import Dict, List, Any, Optional, Tuple


# ---------------------------------------------------------------------------
# CID character substitution map
# These are the common CID codes found in Subaru / ManualsLib PDFs.
# Add more as encountered.
# ---------------------------------------------------------------------------
CID_MAP = {
    "(cid:121)": "•",    # bullet
    "(cid:132)": "■",    # section/filled square
    "(cid:84)":  "™",    # trademark
    "(cid:146)": "'",    # right single quote / apostrophe
    "(cid:147)": "\u201c",  # left double quote
    "(cid:148)": "\u201d",  # right double quote
    "(cid:150)": "–",    # en dash
    "(cid:151)": "—",    # em dash
    "(cid:160)": " ",    # non-breaking space
    "(cid:183)": "·",    # middle dot
}

# Regex that matches any (cid:NNN) token
_CID_RE = re.compile(r"\(cid:\d+\)")

# Footer lines injected by ManualsLib on every page
_FOOTER_LINES = {
    "downloaded from www.manualslib.com manuals search engine",
    "– continued –",
}


def _substitute_cid(text: str) -> str:
    """Replace known (cid:NNN) tokens with their Unicode equivalents."""
    for token, replacement in CID_MAP.items():
        text = text.replace(token, replacement)
    # Remove any remaining unknown CID tokens
    text = _CID_RE.sub("", text)
    return text


def _repair_hyphenation(text: str) -> str:
    """Join words broken across lines with a hyphen (e.g. 'assem-\nblies' → 'assemblies')."""
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)


def _strip_footer_lines(lines: List[str]) -> List[str]:
    """Remove ManualsLib watermark and '– CONTINUED –' lines."""
    return [l for l in lines if l.strip().lower() not in _FOOTER_LINES]


def _detect_columns(words: List[dict], page_width: float) -> List[Tuple[float, float]]:
    """
    Auto-detect column x-ranges from word positions.

    Uses a 5px histogram of word left-edges. A "gap" is any contiguous run
    of buckets whose raw count is zero (no word starts there). Gaps narrower
    than MIN_GAP_PX are ignored to avoid splitting on incidental whitespace
    within a column. Resulting columns narrower than MIN_COL_WIDTH are merged
    into their neighbour.

    Returns a list of (x_start, x_end) tuples sorted left-to-right.
    Falls back to a single full-width column if no clear separators are found.
    """
    if not words:
        return [(0, page_width)]

    BUCKET = 5          # px per histogram bucket
    MIN_GAP_PX = 5      # minimum gap width to qualify as a column separator
    MIN_COL_WIDTH = 50  # minimum column width in px

    # Build histogram of word left-edges (raw, no smoothing)
    buckets: Dict[int, int] = {}
    for w in words:
        b = int(w["x0"] // BUCKET) * BUCKET
        buckets[b] = buckets.get(b, 0) + 1

    if not buckets:
        return [(0, page_width)]

    min_x = min(buckets.keys())
    max_x = int(max(w["x1"] for w in words) // BUCKET) * BUCKET

    # Find contiguous runs of empty buckets
    gaps: List[Tuple[int, int]] = []
    in_gap = False
    gap_start = 0

    for bx in range(min_x, max_x + BUCKET, BUCKET):
        empty = buckets.get(bx, 0) == 0
        if empty:
            if not in_gap:
                gap_start = bx
                in_gap = True
        else:
            if in_gap:
                gap_end = bx
                if gap_end - gap_start >= MIN_GAP_PX:
                    gaps.append((gap_start, gap_end))
                in_gap = False

    if in_gap:
        gap_end = max_x + BUCKET
        if gap_end - gap_start >= MIN_GAP_PX:
            gaps.append((gap_start, gap_end))

    if not gaps:
        return [(float(min_x), float(max_x + BUCKET))]

    # Build column ranges from gap boundaries
    cols: List[Tuple[float, float]] = []
    prev = float(min_x)
    for gs, ge in gaps:
        col_end = float(gs)
        if col_end - prev >= MIN_COL_WIDTH:
            cols.append((prev, col_end))
        prev = float(ge)

    final_end = float(max_x + BUCKET)
    if final_end - prev >= MIN_COL_WIDTH:
        cols.append((prev, final_end))

    if not cols:
        return [(float(min_x), float(max_x + BUCKET))]

    # Extend the first column's left edge and last column's right edge
    # to capture words that fall just outside the histogram bucket boundaries
    actual_min_x = float(min(w["x0"] for w in words))
    actual_max_x = float(max(w["x1"] for w in words))
    cols[0] = (min(cols[0][0], actual_min_x - 1), cols[0][1])
    cols[-1] = (cols[-1][0], max(cols[-1][1], actual_max_x + 1))

    return cols


def _words_to_text(words: List[dict], y_tolerance: float = 4.0) -> str:
    """
    Reconstruct text from a list of word dicts, grouping by y-position into lines.
    Words must already be filtered to a single column.
    """
    if not words:
        return ""

    # Sort by top (y), then x within the same line
    sorted_words = sorted(words, key=lambda w: (round(w["top"] / y_tolerance) * y_tolerance, w["x0"]))

    lines: List[str] = []
    current_line: List[str] = []
    current_top: Optional[float] = None

    for w in sorted_words:
        if current_top is None or abs(w["top"] - current_top) > y_tolerance:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [w["text"]]
            current_top = w["top"]
        else:
            current_line.append(w["text"])

    if current_line:
        lines.append(" ".join(current_line))

    return "\n".join(lines)


def _extract_header_info(all_words: List[dict], page_height: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract printed page number and chapter name from the true header strip.

    Strategy
    --------
    The header strip is the topmost LINE of text on the page — not a fixed
    pixel band. We find the minimum `top` value among all words, then collect
    only words whose `top` is within 8 pt of that minimum. This gives exactly
    the first line (or first two closely-spaced lines in some layouts).

    For car manuals this first line typically contains:
        "ENGINE   7-5"   or   "7-5   ENGINE"

    For regular documents (reports, forms, letters) the first line is usually
    a title or header paragraph — page numbers on those documents appear in the
    very top-right or bottom corner, handled separately below.

    Page-number detection rules (applied only to the first-line words):
      1. "N-NN" / "NN-NNN" format (chapter-page) — always accepted.
      2. Plain integer ≤ 3 digits AND ≤ 999 AND not a year (1800-2099).
         Rejects things like "120" that appear in body tables but happen to
         sit near the top of the page when the header band is too tall.

    Corner page-number fallback
    ---------------------------
    Many non-manual PDFs have a page number isolated in the top-right or
    bottom-right corner (outside the main text flow). We look for a lone
    integer in those positions only if nothing was found in the first line.

    Returns:
        (printed_page_number, chapter_name)  — either may be None.
    """
    if not all_words:
        return None, None

    # ── Step 1: Find the first (topmost) line ────────────────────────────────
    min_top = min(float(w["top"]) for w in all_words)
    # Collect words within 8 pt of the topmost word — this is the header line
    first_line_words = [w for w in all_words if float(w["top"]) <= min_top + 8]

    # Left-to-right order for display / chapter name reconstruction
    first_line_sorted = sorted(first_line_words, key=lambda w: w["x0"])
    tokens = [_substitute_cid(w["text"]).strip() for w in first_line_sorted]

    # chapter-page format e.g. "7-5", "1-21" — completely unambiguous
    _chpage_re = re.compile(r"^\d{1,3}-\d{1,4}$")
    # plain integer — accepted only if ≤ 3 digits and not a year
    _int_re = re.compile(r"^\d{1,3}$")

    def _is_year(s: str) -> bool:
        try:
            return 1800 <= int(s) <= 2099
        except ValueError:
            return False

    page_num: Optional[str] = None
    chapter_tokens: List[str] = []

    for t in tokens:
        if not t:
            continue
        if page_num is None:
            if _chpage_re.match(t):
                page_num = t
                continue
            if _int_re.match(t) and not _is_year(t):
                page_num = t
                continue
        chapter_tokens.append(t)

    chapter = " ".join(chapter_tokens).strip() or None

    # ── Step 2: Corner fallback (top-right or bottom-right lone integer) ─────
    # Only used when the first-line scan found nothing.
    if page_num is None:
        page_w = max((float(w["x1"]) for w in all_words), default=0)
        right_threshold = page_w * 0.75          # right 25 % of the page
        top_threshold   = page_height * 0.08     # top 8 % of the page
        bot_threshold   = page_height * 0.92     # bottom 8 % of the page

        for w in all_words:
            t = _substitute_cid(w["text"]).strip()
            if not _int_re.match(t) or _is_year(t):
                continue
            x0 = float(w["x0"])
            top = float(w["top"])
            # Must be in right margin AND in top or bottom strip
            if x0 >= right_threshold and (top <= top_threshold or top >= bot_threshold):
                page_num = t
                break

    return page_num, chapter


def _extract_page_text(page) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Extract clean text from a single pdfplumber page.

    Returns:
        (text, printed_page_number, chapter_name)

    Strategy:
      1. Extract all words with positions.
      2. Parse header band separately for page number + chapter name.
      3. Auto-detect columns in the body area.
      4. Reconstruct each column's text in reading order.
      5. Concatenate columns with a blank line separator.
      6. Clean up: CID substitution, hyphenation repair, footer removal.
    """
    page_w = float(page.width)
    page_h = float(page.height)

    # Extract all words, excluding the very bottom strip (ManualsLib footer)
    all_words = page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False)
    content_words = [w for w in all_words if float(w["top"]) < page_h - 12]

    if not content_words:
        return "", None, None

    # Extract page number and chapter from the header band
    printed_page_num, chapter_name = _extract_header_info(all_words, page_h)

    # Detect columns in the full content area
    columns = _detect_columns(content_words, page_w)

    # Extract text per column
    column_texts = []
    for col_start, col_end in columns:
        col_words = [w for w in content_words if w["x0"] >= col_start - 2 and w["x0"] < col_end + 2]
        col_text = _words_to_text(col_words)
        if col_text.strip():
            column_texts.append(col_text)

    # Join columns
    raw_text = "\n\n".join(column_texts)

    # Clean up
    raw_text = _substitute_cid(raw_text)
    raw_text = _repair_hyphenation(raw_text)

    # Remove footer lines
    lines = raw_text.split("\n")
    lines = _strip_footer_lines(lines)
    clean_text = "\n".join(lines).strip()

    return clean_text, printed_page_num, chapter_name


class PDFExtractor:
    """
    Extract text, metadata, and tables from PDF files.

    Designed for multi-column technical manuals (car manuals, service docs).
    Tracks both the PDF page index (1-based) and the printed page number
    embedded in the document header (e.g. "7-5", "1-21").
    """

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self._plumber_pdf = None
        self._pypdf2_pdf = None

    def _load(self):
        if self._plumber_pdf is None:
            self._plumber_pdf = pdfplumber.open(self.pdf_path)
        if self._pypdf2_pdf is None:
            self._pypdf2_pdf = PyPDF2.PdfReader(self.pdf_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_metadata(self) -> Dict[str, Any]:
        """
        Extract PDF document metadata (title, author, page count, etc.).
        """
        self._load()
        meta = {}

        if self._pypdf2_pdf.metadata:
            raw = self._pypdf2_pdf.metadata
            meta = {
                "title":             raw.get("/Title", "") or "",
                "author":            raw.get("/Author", "") or "",
                "subject":           raw.get("/Subject", "") or "",
                "creator":           raw.get("/Creator", "") or "",
                "producer":          raw.get("/Producer", "") or "",
                "creation_date":     str(raw.get("/CreationDate", "") or ""),
                "modification_date": str(raw.get("/ModDate", "") or ""),
            }

        meta["num_pages"]    = len(self._pypdf2_pdf.pages)
        meta["is_encrypted"] = self._pypdf2_pdf.is_encrypted

        return meta

    def extract_text(self, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Extract text from PDF pages with column-aware reconstruction.

        Args:
            pages: 0-indexed list of PDF page indices to extract.
                   None = all pages.

        Returns:
            Dict keyed by "page_N" (1-based PDF index), each value:
            {
                "pdf_page":          int,   # 1-based PDF page index
                "printed_page":      str,   # printed number in doc header e.g. "7-5" (or None)
                "chapter":           str,   # chapter/section name from header (or None)
                "text":              str,   # clean extracted text
                "char_count":        int,
            }
        """
        self._load()
        total = len(self._plumber_pdf.pages)

        if pages is None:
            pages = range(total)

        result = {}
        for page_idx in pages:
            if not (0 <= page_idx < total):
                continue

            page = self._plumber_pdf.pages[page_idx]
            text, printed_page, chapter = _extract_page_text(page)

            result[f"page_{page_idx + 1}"] = {
                "pdf_page":     page_idx + 1,
                "printed_page": printed_page,
                "chapter":      chapter,
                "text":         text,
                "char_count":   len(text),
            }

        return result

    def extract_tables(self, pages: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Extract tables from PDF pages.

        Args:
            pages: 0-indexed list of PDF page indices. None = all pages.

        Returns:
            Dict keyed by "page_N" (1-based), each value:
            {
                "pdf_page":     int,
                "printed_page": str or None,
                "num_tables":   int,
                "tables":       list of 2D lists,
            }
        """
        self._load()
        total = len(self._plumber_pdf.pages)

        if pages is None:
            pages = range(total)

        result = {}
        for page_idx in pages:
            if not (0 <= page_idx < total):
                continue

            page = self._plumber_pdf.pages[page_idx]
            tables = page.extract_tables()

            if tables:
                # Get printed page number for cross-reference
                _, printed_page, _ = _extract_page_text(page)

                result[f"page_{page_idx + 1}"] = {
                    "pdf_page":     page_idx + 1,
                    "printed_page": printed_page,
                    "num_tables":   len(tables),
                    "tables":       tables,
                }

        return result

    def extract_all(self, include_tables: bool = True) -> Dict[str, Any]:
        """
        Extract all data from the PDF in a single pass.

        Args:
            include_tables: Set False to skip table extraction (much faster for large docs).

        Returns:
            {
                "metadata": {...},
                "text":     {"page_N": {...}, ...},
                "tables":   {"page_N": {...}, ...},  # only if include_tables=True
            }
        """
        result: Dict[str, Any] = {
            "metadata": self.extract_metadata(),
            "text":     self.extract_text(),
        }
        if include_tables:
            result["tables"] = self.extract_tables()
        return result

    def close(self):
        if self._plumber_pdf:
            self._plumber_pdf.close()
        self._plumber_pdf = None
        self._pypdf2_pdf = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
