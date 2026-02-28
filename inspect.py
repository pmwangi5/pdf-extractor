#!/usr/bin/env python3
"""
inspect.py — dry-run extraction inspector

Shows exactly what the extractor produces for every page of a PDF,
and what the chunker will store in tt_ai.chunks — without touching
any database or calling any external API.

Usage:
    python inspect.py <file.pdf>
    python inspect.py <file.pdf> --pages 1 2 3
    python inspect.py <file.pdf> --no-chunks
    python inspect.py <file.pdf> --json          # machine-readable output
    python inspect.py <file.pdf> --chunk-size 1500 --overlap 400
"""

import sys
import json
import argparse
import textwrap
from pathlib import Path

# ── local imports ──────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from pdf_extractor import PDFExtractor


# ── helpers ────────────────────────────────────────────────────────────────────

def _hr(char="─", width=80):
    return char * width


def _wrap(text, width=78, indent=4):
    prefix = " " * indent
    return textwrap.fill(text, width=width, initial_indent=prefix,
                         subsequent_indent=prefix)


def _trunc(text, n=300):
    if len(text) <= n:
        return text
    return text[:n] + f"  … [{len(text) - n} more chars]"


# ── page-level report ──────────────────────────────────────────────────────────

def report_pages(text_by_page, show_full=False):
    pages = sorted(text_by_page.items(),
                   key=lambda x: x[1].get("pdf_page", 0))

    print(_hr("═"))
    print(f"  PAGE-BY-PAGE EXTRACTION  ({len(pages)} pages)")
    print(_hr("═"))

    for key, pd in pages:
        pdf_page     = pd.get("pdf_page", "?")
        printed_page = pd.get("printed_page")
        chapter      = pd.get("chapter")
        char_count   = pd.get("char_count", 0)
        text         = pd.get("text", "")

        print()
        print(_hr())
        print(f"  PDF page index : {pdf_page}")
        print(f"  printed_page   : {printed_page!r}   ← stored in tt_ai.chunks.printed_page")
        print(f"  chapter        : {chapter!r}   ← stored in tt_ai.chunks.chapter")
        print(f"  char_count     : {char_count}")
        print(_hr("-"))

        if not text.strip():
            print("    (no extractable text on this page)")
        else:
            display = text if show_full else _trunc(text, 400)
            for line in display.split("\n"):
                print(f"    {line}")

    print()
    print(_hr("═"))


# ── chunk-level report ─────────────────────────────────────────────────────────

def report_chunks(text_by_page, chunk_size, overlap, show_full=False):
    # Import the chunker from api.py without starting Flask
    import importlib.util, types

    # Minimal stubs so api.py imports cleanly without env vars
    import unittest.mock as mock
    import os

    # We only need _chunk_text_for_embeddings from api.py.
    # Load it by exec-ing the source with mocked heavy deps.
    api_path = Path(__file__).parent / "api.py"
    src = api_path.read_text()

    # Build a minimal module namespace
    ns = types.ModuleType("api")
    ns.__file__ = str(api_path)
    ns.__spec__ = None

    # Provide stubs for everything api.py imports at module level
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = lambda *a, **k: mock.MagicMock()
    fake_flask.request = mock.MagicMock()
    fake_flask.jsonify = mock.MagicMock()
    sys.modules.setdefault("flask", fake_flask)
    sys.modules.setdefault("flask_cors", mock.MagicMock())
    sys.modules.setdefault("werkzeug", mock.MagicMock())
    sys.modules.setdefault("werkzeug.utils", mock.MagicMock())
    sys.modules.setdefault("dotenv", mock.MagicMock())
    sys.modules.setdefault("requests", mock.MagicMock())
    sys.modules.setdefault("boto3", mock.MagicMock())
    sys.modules.setdefault("botocore", mock.MagicMock())
    sys.modules.setdefault("botocore.exceptions", mock.MagicMock())
    sys.modules.setdefault("openai", mock.MagicMock())
    sys.modules.setdefault("redis", mock.MagicMock())
    sys.modules.setdefault("pdf2image", mock.MagicMock())

    # Patch os.environ so app startup doesn't fail
    with mock.patch.dict(os.environ, {
        "NHOST_BACKEND_URL": "", "NHOST_ADMIN_SECRET": "",
        "OPENAI_API_KEY": "",    "REDIS_URL": "",
    }):
        exec(compile(src, str(api_path), "exec"), ns.__dict__)

    chunk_fn = ns.__dict__["_chunk_text_for_embeddings"]
    chunks = chunk_fn(text_by_page, chunk_size=chunk_size, overlap=overlap)

    print(_hr("═"))
    print(f"  CHUNKS  ({len(chunks)} total, chunk_size={chunk_size}, overlap={overlap})")
    print(_hr("═"))

    for ch in chunks:
        idx          = ch["chunk_index"]
        pages        = ch.get("pages", [])
        printed_pgs  = ch.get("printed_pages", [])
        chapters     = ch.get("chapters", [])
        char_count   = ch.get("char_count", 0)
        text         = ch.get("text", "")

        # What will actually land in the DB
        db_page          = pages[0]         if pages         else None
        db_printed_page  = printed_pgs[0]   if printed_pgs   else None
        db_chapter       = chapters[0]      if chapters      else None

        print()
        print(_hr())
        print(f"  chunk_index    : {idx}")
        print(f"  DB page        : {db_page!r}         (tt_ai.chunks.page  — PDF page index)")
        print(f"  DB printed_page: {db_printed_page!r}   (tt_ai.chunks.printed_page)")
        print(f"  DB chapter     : {db_chapter!r}")
        print(f"  char_count     : {char_count}  |  spans PDF pages: {pages}")
        print(_hr("-"))

        if not text.strip():
            print("    (empty chunk)")
        else:
            display = text if show_full else _trunc(text, 400)
            for line in display.split("\n"):
                print(f"    {line}")

    print()
    print(_hr("═"))
    print(f"  SUMMARY: {len(chunks)} chunks from {len(text_by_page)} pages")

    # Page number sanity check
    bad = []
    for ch in chunks:
        pages       = ch.get("pages", [])
        printed_pgs = ch.get("printed_pages", [])
        if not pages:
            continue
        pdf_pg = pages[0]
        for pp in printed_pgs:
            if pp is None:
                continue
            s = str(pp).strip()
            # Flag if plain integer is wildly out of range of PDF page index
            if s.isdigit():
                n = int(s)
                if 1800 <= n <= 2099:
                    bad.append((ch["chunk_index"], pdf_pg, pp, "looks like a year"))
                elif n > pdf_pg + 500:
                    bad.append((ch["chunk_index"], pdf_pg, pp,
                                f"printed_page {n} >> pdf_page {pdf_pg}"))

    if bad:
        print()
        print("  ⚠️  POSSIBLE BAD printed_page VALUES:")
        for chunk_idx, pdf_pg, pp, reason in bad:
            print(f"     chunk {chunk_idx}: pdf_page={pdf_pg}, printed_page={pp!r} → {reason}")
    else:
        print("  ✓  All printed_page values look plausible")

    print(_hr("═"))


# ── JSON output ────────────────────────────────────────────────────────────────

def report_json(text_by_page, chunks):
    out = {
        "pages": {
            k: {
                "pdf_page":     v.get("pdf_page"),
                "printed_page": v.get("printed_page"),
                "chapter":      v.get("chapter"),
                "char_count":   v.get("char_count"),
                "text_preview": v.get("text", "")[:200],
            }
            for k, v in text_by_page.items()
        },
        "chunks": [
            {
                "chunk_index":   ch["chunk_index"],
                "db_page":       ch.get("pages", [None])[0],
                "db_printed_page": ch.get("printed_pages", [None])[0],
                "db_chapter":    ch.get("chapters", [None])[0],
                "char_count":    ch.get("char_count"),
                "text_preview":  ch.get("text", "")[:200],
            }
            for ch in chunks
        ],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Dry-run extraction inspector — no DB, no API calls",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python inspect.py report.pdf
  python inspect.py manual.pdf --pages 1 5 10
  python inspect.py doc.pdf --no-chunks --full
  python inspect.py doc.pdf --json > out.json
  python inspect.py doc.pdf --chunk-size 1500 --overlap 400
        """,
    )
    parser.add_argument("pdf", help="PDF file to inspect")
    parser.add_argument("--pages", type=int, nargs="+",
                        help="1-indexed page numbers to inspect (default: all)")
    parser.add_argument("--no-chunks", action="store_true",
                        help="Skip chunk report, show pages only")
    parser.add_argument("--full", action="store_true",
                        help="Print full text instead of truncated preview")
    parser.add_argument("--json", action="store_true",
                        help="Output machine-readable JSON (ignores --full/--no-chunks)")
    parser.add_argument("--chunk-size", type=int, default=1500,
                        help="Target chars per chunk (default: 1500)")
    parser.add_argument("--overlap", type=int, default=400,
                        help="Overlap chars between chunks (default: 400)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: file not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    pages_0idx = [p - 1 for p in args.pages] if args.pages else None

    print(f"\nInspecting: {pdf_path.name}  ({pdf_path.stat().st_size // 1024} KB)\n")

    with PDFExtractor(str(pdf_path)) as ex:
        meta = ex.extract_metadata()
        text_by_page = ex.extract_text(pages_0idx)

    print(f"Metadata:")
    print(f"  title    : {meta.get('title') or '(none)'}")
    print(f"  author   : {meta.get('author') or '(none)'}")
    print(f"  num_pages: {meta.get('num_pages')}")
    print()

    if args.json:
        # Need chunks too for JSON mode — compute them
        import unittest.mock as mock
        import os
        import types
        api_path = Path(__file__).parent / "api.py"
        src = api_path.read_text()
        ns = types.ModuleType("api")
        for mod in ["flask", "flask_cors", "werkzeug", "werkzeug.utils",
                    "dotenv", "requests", "boto3", "botocore",
                    "botocore.exceptions", "openai", "redis", "pdf2image"]:
            sys.modules.setdefault(mod, mock.MagicMock())
        with mock.patch.dict(os.environ, {
            "NHOST_BACKEND_URL": "", "NHOST_ADMIN_SECRET": "",
            "OPENAI_API_KEY": "", "REDIS_URL": "",
        }):
            exec(compile(src, str(api_path), "exec"), ns.__dict__)
        chunk_fn = ns.__dict__["_chunk_text_for_embeddings"]
        chunks = chunk_fn(text_by_page,
                          chunk_size=args.chunk_size, overlap=args.overlap)
        report_json(text_by_page, chunks)
        return

    report_pages(text_by_page, show_full=args.full)

    if not args.no_chunks:
        print()
        report_chunks(text_by_page, args.chunk_size, args.overlap,
                      show_full=args.full)


if __name__ == "__main__":
    main()
