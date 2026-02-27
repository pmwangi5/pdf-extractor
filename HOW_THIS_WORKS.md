# How This Works

A detailed technical reference for the PDF Extractor service.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Request Lifecycle](#request-lifecycle)
4. [PDF Extraction Engine](#pdf-extraction-engine)
5. [Text Chunking](#text-chunking)
6. [Database Pipeline](#database-pipeline)
7. [OpenAI Embeddings](#openai-embeddings)
8. [Cloud Storage (DigitalOcean Spaces)](#cloud-storage)
9. [Concurrency Model](#concurrency-model)
10. [Job State & Progress Tracking](#job-state--progress-tracking)
11. [Failure Contract](#failure-contract)
12. [Environment Variables](#environment-variables)
13. [Database Schema](#database-schema)
14. [API Reference](#api-reference)

---

## Overview

This is a Python/Flask microservice that sits between your Next.js frontend and your Nhost/Hasura database. Its job is to:

1. Accept a PDF upload from a Next.js file uploader
2. Extract clean, structured text from every page — handling multi-column layouts, CID artifacts, and printed page numbers
3. Split the text into overlapping semantic chunks optimised for vector search
4. Generate OpenAI embeddings for every chunk **before** writing anything to the database
5. Upload the PDF and a preview JPG to DigitalOcean Spaces
6. Insert the document record and all chunks (with embeddings already set) into Nhost in a single pass
7. Return a `job_id` immediately so the client can poll for progress

Everything after step 1 runs in a background thread. The HTTP response returns in milliseconds.

---

## Architecture

```
Next.js (browser)
      │
      │  POST /extract/async
      │  FormData: file, userId, upload_device
      ▼
┌─────────────────────────────────────────────────────────┐
│                    Flask API (api.py)                    │
│                                                         │
│  ┌────────────────────────────────────────────────────┐ │
│  │              Background Thread                     │ │
│  │                                                    │ │
│  │  1. PDFExtractor (pdf_extractor.py)                │ │
│  │       pdfplumber  — text + tables                  │ │
│  │       PyPDF2      — metadata, validation           │ │
│  │                                                    │ │
│  │  2. _chunk_text_for_embeddings()                   │ │
│  │                                                    │ │
│  │  3. _send_to_db()                                  │ │
│  │       ├─ DigitalOcean Spaces  (PDF upload)         │ │
│  │       ├─ DigitalOcean Spaces  (JPG preview)        │ │
│  │       ├─ Nhost GraphQL        INSERT documents     │ │
│  │       ├─ OpenAI Embeddings API (batched, retried)  │ │
│  │       ├─ Nhost GraphQL        INSERT chunks+vecs   │ │
│  │       └─ Nhost GraphQL        UPDATE doc→embedded  │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  Redis (or in-memory)  ←→  job progress store           │
└─────────────────────────────────────────────────────────┘
      │
      │  GET /job/<job_id>   (client polls)
      ▼
Next.js (browser)
```

---

## Request Lifecycle

### 1. Client sends the file

```
POST /extract/async
Content-Type: multipart/form-data

file          = <PDF binary>
userId        = "uuid-of-the-logged-in-user"
upload_device = "web"          # or "mobile", "ios", etc.
send_to_nhost = "true"
send_webhook  = "true"
```

### 2. Concurrency gate

Before doing anything, the endpoint tries to acquire a semaphore slot (`MAX_CONCURRENT_JOBS`, default 10). If all slots are taken it returns immediately:

```json
HTTP 503
{ "success": false, "error": "Busy – try again later", "active_jobs": 10 }
```

### 3. File saved to disk, job ID created

The uploaded file is saved to the system temp directory as `<job_id>_<filename>.pdf`. The file object must be persisted to disk before the request ends because the background thread runs after the HTTP response is sent.

A UUID `job_id` is generated and the initial job state is written to Redis (or in-memory fallback).

### 4. HTTP response returned immediately

```json
HTTP 202
{
  "success": true,
  "job_id": "3f8a1c2d-...",
  "status": "processing",
  "message": "Extraction started. Use /job/<job_id> to check status."
}
```

### 5. Background thread runs the full pipeline

The thread holds the semaphore slot for its entire duration and releases it in a `finally` block — guaranteed even on crash or exception.

---

## PDF Extraction Engine

**File:** `pdf_extractor.py`

Two libraries are used together:

| Library | Purpose |
|---|---|
| `pdfplumber` | Word-level position data, text reconstruction, table extraction |
| `PyPDF2` | Document metadata (title, author, page count), encryption check |

### Security validation

Before extraction, `validate_pdf_structure()` checks:
- Magic bytes (`%PDF`) — rejects files with a `.pdf` extension but non-PDF content
- File size (min 100 bytes, max 200 MB)
- Page count (max 10,000 pages)
- Embedded scripts or embedded files (potential malware vectors)

### XSS / injection defence (two layers)

#### Layer 1 — Raw binary scan (`_scan_pdf_binary_for_xss`)

Runs **before** any text extraction. The entire PDF file is read as bytes and decoded as `latin-1` (lossless). The full byte stream is scanned against `_XSS_PATTERNS` — a list of 27 compiled regexes covering:

- HTML `<script>` tags (any variant, including entity-encoded)
- JavaScript event handlers (`onload=`, `onerror=`, etc.)
- `javascript:` / `vbscript:` / `data:` URI schemes
- `<iframe>`, `<object>`, `<embed>`, `<applet>` tags
- SVG with event handlers
- DOM manipulation (`document.cookie`, `innerHTML=`, `eval()`, etc.)
- `window.location` redirects
- Base64-encoded `javascript:`
- PDF-specific actions: `/JavaScript`, `/JS`, `/OpenAction`, `/AA`, `/Launch`, `/SubmitForm`, `/ImportData`, `/RichMedia`

If any pattern matches, the document is **rejected immediately** — the temp file is deleted, the job is marked `failed`, and `_flag_user_bad_actor()` is called.

#### Layer 2 — Extracted text scan (`detect_dangerous_content`)

During chunking, every page's sanitised text is scanned with the same `_XSS_PATTERNS` set. This catches payloads embedded in text streams that the binary scan might miss (e.g. obfuscated content in form field values). A match raises a `ValueError` which propagates to `_send_to_db`'s `except` block, which also calls `_flag_user_bad_actor()`.

#### User ban (`_flag_user_bad_actor`)

When XSS is detected, two independent Hasura mutations are executed (a failure in one does not block the other):

**Mutation 1 — `auth.users` (Nhost managed table):**
```graphql
mutation BanUser($id: uuid!) {
    updateUser(pk_columns: {id: $id}, _set: {
        disabled:    true,
        defaultRole: ""
    }) { id disabled defaultRole }
}
```

**Mutation 2 — `userProfiles` (app table):**
```graphql
mutation BanUserProfile($uid: uuid!, $meta: jsonb!) {
    update_userProfiles(
        where: {userID: {_eq: $uid}},
        _set: {
            canRunGFcrm:      false,
            mechanicGarageId: null,
            isAdmin:          0,
            userMetaData:     $meta
        }
    ) { affected_rows }
}
```

The `userMetaData` payload written is:
```json
{
  "BANNED": true,
  "reason": "XSS/injection detected in uploaded PDF: <pattern name>",
  "timestamp": "2026-02-27T12:34:56.789Z",
  "action": "account disabled, all roles stripped"
}
```

Both mutations use the Hasura admin secret so they can write to the `auth` schema and the app schema regardless of row-level security policies.

### Per-page extraction (`_extract_page_text`)

Each page goes through this pipeline:

#### Step 1 — Extract all words with positions

`pdfplumber.extract_words()` returns every word as a dict with `x0`, `x1`, `top`, `bottom`, `text`. This gives sub-pixel positioning for every token on the page.

#### Step 2 — Parse the header band

The top ~270 units of the page are treated as a header band. Words in this band are scanned for:

- **Printed page number** — a token matching `^\d+-\d+$` (chapter-page format like `7-5`) or `^\d+$` (plain integer). The first match wins.
- **Chapter name** — all other tokens in the header band concatenated left-to-right.

This is done *before* column detection so that header content is never mixed into body text.

#### Step 3 — Column detection (`_detect_columns`)

A 5 px histogram of word left-edges (`x0`) is built across the full page width. Any contiguous run of empty histogram buckets ≥ 5 px wide is treated as a column gap. This reliably separates 2- and 3-column layouts (common in car manuals) without any hard-coded column counts.

Rules:
- Gaps narrower than `MIN_GAP_PX` (5 px) are ignored — prevents splitting on incidental whitespace within a column
- Columns narrower than `MIN_COL_WIDTH` (50 px) are discarded
- The first column's left boundary and the last column's right boundary are extended to the actual word extents, preventing edge words from being missed

#### Step 4 — Per-column text reconstruction (`_words_to_text`)

Words are filtered to each column's x-range (with ±2 px tolerance for slight misalignment). Within a column, words are sorted by y-position (rounded to 4 px tolerance to group words on the same line), then by x-position within each line. Lines are joined with spaces; lines are joined with newlines.

#### Step 5 — Columns joined

Column texts are joined with a blank line (`\n\n`) separator, preserving natural left-to-right reading order.

#### Step 6 — Cleanup

- **CID substitution** — `(cid:NNN)` tokens are replaced with their Unicode equivalents (bullets, dashes, quotes, trademark symbols). Unknown CID tokens are stripped entirely.
- **Hyphenation repair** — words broken across lines (`assem-\nblies`) are rejoined (`assemblies`).
- **Footer removal** — ManualsLib watermark lines (`downloaded from www.manualslib.com...`) are stripped.

### Output per page

```python
{
  "pdf_page":     7,          # 1-based PDF page index
  "printed_page": "1-5",      # printed number from document header
  "chapter":      "ENGINE",   # chapter/section from header
  "text":         "...",      # clean body text
  "char_count":   1842,
}
```

---

## Text Chunking

**Function:** `_chunk_text_for_embeddings()` in `api.py`

Raw page text is too large and too coarse for vector search. The chunker produces overlapping segments that preserve enough context for a language model to answer questions accurately.

### Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `chunk_size` | 1500 chars | Target maximum characters per chunk |
| `overlap` | 400 chars | Characters carried over from the previous chunk |

### Algorithm

1. **Normalise** each page's text (collapse whitespace, fix encoding)
2. **Sanitise** (remove null bytes, control characters, overly long tokens)
3. **Split into semantic units** — paragraphs, then sentences, then words as fallback
4. **Combine units** into chunks up to `chunk_size`. When a chunk is full:
   - Save it
   - Start the next chunk with the last `overlap` characters of the previous chunk, trimmed to a sentence or paragraph boundary where possible
5. **Track metadata** per unit: `pdf_page` (int), `printed_page` (str), `chapter` (str)

### Chunk output

```python
{
  "chunk_index":    0,
  "text":           "...",
  "pages":          [7, 8],          # PDF page indices spanned
  "printed_pages":  ["1-5"],         # printed page numbers spanned
  "chapters":       ["ENGINE"],      # chapters spanned
  "char_count":     1487,
  "start_page":     7,
  "end_page":       8,
}
```

Chunks are capped at `MAX_CHUNKS_PER_PDF` (10,000) to prevent resource exhaustion on pathological inputs.

---

## Database Pipeline

**Function:** `_send_to_db()` in `api.py`

This is the core persistence function. It runs entirely inside the background thread and updates job progress at each stage.

### Critical design principle: embeddings are generated BEFORE any chunk rows are written

This guarantees there are no orphan chunk rows in the database without vectors. If OpenAI fails, nothing is written to `tt_ai.chunks`. The document row is marked `failed`. No partial states.

### Stage 1 — Spaces upload (progress 55%)

The local temp PDF is uploaded to DigitalOcean Spaces. On success:
- The CDN URL becomes `source` in `tt_ai.documents`
- The first page is rendered to a JPG (via `pdf2image`/`poppler`) and uploaded as `preview_url`

Spaces failure is non-fatal — the document is stored without a `source` URL if the upload fails.

### Stage 2 — INSERT `tt_ai.documents` (progress 62%)

A single GraphQL mutation inserts the document record with `status = 'processing'`:

```graphql
mutation InsertDocument($obj: tt_ai_documents_insert_input!) {
  insert_tt_ai_documents_one(object: $obj) { id }
}
```

Fields written: `job_id`, `title`, `filename`, `source`, `preview_url`, `num_pages`, `metadata`, `status`, `upload_device`, `userID`.

If this mutation returns GraphQL errors, the function returns `None` immediately (no document row exists to clean up). The caller marks the job `failed`.

### Stage 3 — Generate OpenAI embeddings (progress 68%)

See [OpenAI Embeddings](#openai-embeddings) section below.

`_generate_openai_embeddings()` **raises `RuntimeError`** on any failure — it never returns a partial list. The exception propagates to the outer `except` block which sets `document.status = 'failed'`.

After the call returns, an `assert len(embeddings) == len(chunks)` verifies completeness before proceeding.

### Stage 4 — Bulk INSERT `tt_ai.chunks` with embeddings (progress 85%)

Chunks are inserted in batches of 100, each with `embedding_chatgpt` and `chatgpt_model_name` already set. No separate UPDATE pass is needed.

```graphql
mutation InsertChunks($objects: [tt_ai_chunks_insert_input!]!) {
  insert_tt_ai_chunks(objects: $objects) {
    affected_rows
  }
}
```

Each batch returns `affected_rows`. After all batches, `total_inserted` is compared to `len(chunk_objects)`. Any mismatch raises immediately — the document is never marked `embedded`.

**Why batches of 100?**
An 800-page PDF produces ~800 chunks. Each chunk carries a 1536-dimension vector (~12 KB as a string). 800 × 12 KB ≈ 9.6 MB — near Hasura's default request body limit. Batching at 100 keeps each payload under 1.2 MB with headroom.

### Stage 5 — Mark document embedded (progress 95%)

Only reached after `total_inserted == len(chunk_objects)` is confirmed:

```graphql
mutation MarkEmbedded($id: uuid!, $source: String, $preview: String) {
  update_tt_ai_documents_by_pk(
    pk_columns: {id: $id},
    _set: {status: "embedded", source: $source, preview_url: $preview}
  ) { id status }
}
```

If this final mutation fails (rare — chunks are already stored correctly), it is logged as an error but does not abort. The document stays at `processing` in the DB but the chunks are intact.

### Stage 6 — Email notification

If `AWS_SES_*` is configured and a `userId` was provided, an email is sent via AWS SES confirming the document is ready.

---

## OpenAI Embeddings

**Function:** `_generate_openai_embeddings()` in `api.py`

| Setting | Default | Env var |
|---|---|---|
| Model | `text-embedding-3-small` | `OPENAI_EMBEDDING_MODEL` |
| Dimensions | 1536 | fixed by model |
| Batch size | 200 texts/call | `OPENAI_EMBED_BATCH_SIZE` |
| Max retries | 4 per batch | hardcoded |
| Backoff | 2s → 4s → 8s → 16s | hardcoded |

### Guarantees

- **Never returns a partial list.** Either returns a complete list of vector strings (one per input, no `None` holes) or raises `RuntimeError`.
- **Retries on transient failures.** Rate limits (429), server errors (500/503), and timeouts trigger exponential back-off up to 4 attempts per batch.
- **Raises on non-retryable errors.** Invalid API key, model not found, etc. raise immediately without retrying.
- **Final completeness check.** After all batches, every slot in the result list is verified non-None before returning.

### Vector format

Embeddings are returned as PostgreSQL vector literal strings: `"[0.123,0.456,...]"`. This is the format Hasura expects for `vector(1536)` columns — it exposes them as `String` scalars in GraphQL, not a native `vector` type.

### Why inline (same thread)?

- Chunks are already in memory — no DB round-trip to re-fetch them
- `text-embedding-3-small` processes 200 chunks in ~3–5 seconds
- The job progress bar reflects the embedding stage in real time
- A separate queue/worker would add infrastructure complexity for no throughput gain at this scale

### Adding Mistral embeddings later

The `tt_ai.chunks` table already has `embedding_mistral` and `mistral_model_name` columns. When ready, add a second pass in `_send_to_db` after the OpenAI pass, calling the Mistral API and inserting into those columns. The same insert-with-embedding pattern applies.

---

## Cloud Storage

**Provider:** DigitalOcean Spaces (S3-compatible)

**Function:** `upload_to_spaces()` in `api.py`

Files are stored under:
```
<DO_SPACES_BUCKET>/docs_pdf_embedding_sources/<job_id>/<filename>
```

The returned URL is the permanent CDN link (e.g. `https://<bucket>.nyc3.cdn.digitaloceanspaces.com/...`) stored as `source` in `tt_ai.documents`. This URL is directly linkable and downloadable.

Preview JPGs follow the same path with `_preview.jpg` appended to the stem.

### Required env vars

```
DO_SPACES_URL     = https://nyc3.digitaloceanspaces.com
DO_SPACES_ID      = <access key>
DO_SPACES_SECRET  = <secret key>
DO_SPACES_BUCKET  = <bucket name>
```

---

## Concurrency Model

The service uses a `threading.Semaphore` to cap simultaneous PDF jobs.

```
MAX_CONCURRENT_JOBS = 10  (configurable via env var)
```

- The semaphore is acquired **before** the background thread is started
- It is released in a `finally` block inside the thread — guaranteed even on exception
- A separate `_active_job_count` counter (protected by a `threading.Lock`) tracks the live count for the `/health` endpoint
- Requests that arrive when all slots are full get an immediate `HTTP 503` with `active_jobs` and `max_concurrent_jobs` in the body

For Railway's single-instance deployment this is sufficient. If you scale to multiple instances, replace the semaphore with a Redis-backed distributed lock.

---

## Job State & Progress Tracking

Job state is stored in Redis (if `REDIS_URL` is set) or in an in-memory dict (single-instance fallback).

### Progress stages

| Stage key | Progress % | Description |
|---|---|---|
| `file_received` | 0 | File saved to disk |
| `reading` | 5–30 | PDF being parsed |
| `reading_complete` | 50 | All pages extracted |
| `storing` | 52 | About to start DB pipeline |
| `spaces_upload` | 55 | Uploading PDF to Spaces |
| `insert_document` | 62 | Inserting document row |
| `embeddings` | 68 | Calling OpenAI API |
| `insert_chunks` | 85 | Writing chunks + vectors to DB |
| `finalise` | 95 | Marking document embedded |
| `done` | 100 | Complete |
| `failed` | — | Error — see `error` field |

### Polling

```
GET /job/<job_id>

→ { "status": "processing", "progress": 68, "stage": "embeddings", "message": "Generating ChatGPT embeddings…" }
→ { "status": "completed",  "progress": 100, "db_result": { "document_id": "...", "chunk_count": 312 } }
→ { "status": "failed",     "error": "..." }
```

Redis TTLs: completed jobs expire after 1 hour, failed jobs after 24 hours.

---

## Failure Contract

This is the most important section for understanding what state the system is in after any failure.

| Failure point | Document DB status | Chunks in DB | Job Redis status |
|---|---|---|---|
| Spaces upload fails | Never created | None | `failed` |
| Document INSERT fails (GraphQL error) | Never created | None | `failed` |
| OpenAI API fails after 4 retries | `failed` | None | `failed` |
| Chunk INSERT fails (GraphQL error) | `failed` | Partial (some batches may have succeeded) | `failed` |
| Chunk count mismatch | `failed` | Partial | `failed` |
| MarkEmbedded fails | `processing` (stuck) | All present with vectors | `completed` |

**The only ambiguous case** is the last row: if `MarkEmbedded` fails, the chunks are all correctly stored with vectors but the document status stays at `processing`. This is detectable — query `tt_ai.chunks` for `document_id` and check `count(*) > 0 AND embedding_chatgpt IS NOT NULL`.

**Document status values:**

| Status | Meaning |
|---|---|
| `processing` | Background thread still running (or MarkEmbedded failed — see above) |
| `embedded` | All chunks stored with vectors — fully ready for search |
| `failed` | Something went wrong — check service logs for the job_id |

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `NHOST_BACKEND_URL` | Yes | — | Nhost project URL (e.g. `https://xxx.nhost.run`) |
| `NHOST_ADMIN_SECRET` | Yes | — | Hasura admin secret |
| `NHOST_GRAPHQL_URL` | No | `<NHOST_BACKEND_URL>/v1/graphql` | Override GraphQL endpoint |
| `OPENAI_API_KEY` | Yes | — | OpenAI secret key (required for embeddings) |
| `OPENAI_EMBEDDING_MODEL` | No | `text-embedding-3-small` | Embedding model name |
| `OPENAI_EMBED_BATCH_SIZE` | No | `200` | Texts per OpenAI API call |
| `DO_SPACES_URL` | No | — | Spaces endpoint URL |
| `DO_SPACES_ID` | No | — | Spaces access key ID |
| `DO_SPACES_SECRET` | No | — | Spaces secret key |
| `DO_SPACES_BUCKET` | No | — | Spaces bucket name |
| `AWS_SES_REGION` | No | `eu-central-1` | SES region |
| `AWS_ACCESS_KEY_ID` | No | — | AWS access key (for SES) |
| `AWS_SECRET_ACCESS_KEY` | No | — | AWS secret key (for SES) |
| `AWS_SES_FROM_EMAIL` | No | — | Verified sender address |
| `AWS_SES_TO_EMAIL` | No | — | Notification recipient |
| `REDIS_URL` | No | — | Redis connection string (Railway auto-sets this) |
| `WEBHOOK_URL` | No | — | Next.js webhook endpoint for completion events |
| `MAX_CONCURRENT_JOBS` | No | `10` | Max simultaneous PDF jobs |
| `CORS_ORIGINS` | No | `*` | Allowed CORS origins (comma-separated) |

---

## Database Schema

Schema: `tt_ai`

### `tt_ai.documents`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | PK, `gen_random_uuid()` |
| `job_id` | text | Unique index, links to service job |
| `title` | text | From PDF metadata or inferred from first page |
| `filename` | text | Original upload filename |
| `source` | text | DigitalOcean Spaces CDN URL of the PDF |
| `preview_url` | text | CDN URL of the first-page JPG preview |
| `num_pages` | integer | Total pages in the PDF |
| `metadata` | jsonb | Raw PDF metadata (author, creator, dates, etc.) |
| `status` | text | `processing` → `embedded` (or `failed`) |
| `userID` | uuid | FK to auth user |
| `upload_device` | text | `web`, `mobile`, etc. |
| `vehicle_make` | text | Inferred vehicle make (future) |
| `vehicle_make_id` | uuid | FK to `mycar_vehicle_makes` (future) |
| `vehicle_model` | text | Inferred vehicle model (future) |
| `vehicle_model_id` | uuid | FK to `mycar_vehicle_models` (future) |
| `created_at` | timestamptz | `now()` |

### `tt_ai.chunks`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | PK, `gen_random_uuid()` |
| `document_id` | uuid | FK → `tt_ai.documents.id` (CASCADE DELETE) |
| `chunk_index` | integer | 0-based order within the document |
| `content` | text | The chunk text |
| `page` | integer | PDF page index (1-based) where chunk starts |
| `printed_page` | text | Printed page number from document header (e.g. `7-5`) |
| `chapter` | text | Chapter/section name from document header |
| `char_count` | integer | Character count of `content` |
| `embedding_chatgpt` | vector(1536) | OpenAI `text-embedding-3-small` vector — always set on insert |
| `chatgpt_model_name` | text | Model used for `embedding_chatgpt` |
| `embedding_mistral` | vector(1536) | Mistral embedding (future) |
| `mistral_model_name` | text | Model used for `embedding_mistral` (future) |
| `created_at` | timestamptz | `now()` |

---

## API Reference

### `POST /extract/async` — recommended

Accepts a PDF, starts background processing, returns a `job_id`.

**Form fields:**

| Field | Required | Description |
|---|---|---|
| `file` | Yes | PDF file (max 200 MB) |
| `userId` | No | UUID of the uploading user (camelCase; `user_id` also accepted) |
| `upload_device` | No | Device label, default `web` |
| `send_to_nhost` | No | `true`/`false`, default `true` |
| `send_webhook` | No | `true`/`false`, default `true` |
| `extract_type` | No | `all`/`text`/`metadata`/`tables`, default `all` |
| `include_tables` | No | `true`/`false`, default `true` |

**Response `202`:**
```json
{ "success": true, "job_id": "uuid", "status": "processing" }
```

**Response `503` (all slots busy):**
```json
{ "success": false, "error": "Busy – try again later", "active_jobs": 10 }
```

---

### `GET /job/<job_id>` — poll for status

```json
{ "status": "processing", "progress": 68, "stage": "embeddings", "message": "Generating ChatGPT embeddings…" }
{ "status": "completed",  "progress": 100, "db_result": { "document_id": "uuid", "chunk_count": 312 } }
{ "status": "failed",     "error": "Database storage or embedding generation failed. Check service logs." }
```

---

### `GET /health`

```json
{ "status": "healthy", "active_jobs": 2, "max_concurrent_jobs": 10, "slots_available": 8 }
```

---

### `GET /debug/nhost`

Returns current Nhost/OpenAI configuration state (no secrets, just whether they are set).

---

### `POST /extract` — synchronous (small PDFs only)

Blocks until complete. Not recommended for production. Accepts the same form fields as `/extract/async` plus `send_to_nhost=true` to trigger DB storage inline. No Spaces upload (no local file path available in the sync path).
